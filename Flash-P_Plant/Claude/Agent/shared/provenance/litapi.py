"""
Literature APIs for FLASH-P provenance — OpenAlex and Europe PMC.

Stdlib only (``urllib``), no API keys, and **no contact email**: both APIs serve the
anonymous pool without one, and FLASH-P must not leak a user's address into a public
request log.

Politeness is per-host: a minimum gap between requests to the same host, a hard
timeout, and retry-with-backoff on 429 and 5xx. AI-Writer's ``http.ts`` — the closest
prior art — has the queue but *no* retry, so a single 429 there silently becomes a
"paper not found". Here a rate-limit is retried, and only a real miss reports a miss.

What each source is for:
  * OpenAlex   — DOI -> metadata + abstract (rebuilt from ``abstract_inverted_index``),
                 and free-text ``search=`` when a DOI has to be re-found.
  * Europe PMC — a second opinion on DOI lookups (it often has an abstract OpenAlex
                 lacks), free-text search, and the **only** full-text source we use:
                 ``{pmcid}/fullTextXML`` for records it reports as open access.

Open-access provenance rule (borrowed from AI-Writer's ADR 0004): full text is fetched
**only** when the source's own OA fields say so — never from a free-text link, an
aggregator, or a repository scrape. A closed-access paper yields its abstract and
nothing else, and says so.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "PaperRecord", "HttpStats",
    "openalex_by_doi", "openalex_search",
    "epmc_by_doi", "epmc_by_dois", "epmc_search", "epmc_fulltext",
    "pubmed_search", "pubmed_by_pmids",
    "ncbi_taxonomy_species", "TAXONOMY_JUNK_EPITHETS",
    "reconstruct_abstract", "bare_doi", "doi_slug", "metered_out",
]

USER_AGENT = "FLASH-P/1.0 (provenance verification; https://github.com/flash-p)"
# Metadata and search responses are small and, measured, arrive in 1-3 s; 12 s is
# already far out in the tail, and waiting 20 s three times over for one wedged lookup
# just blocks a worker that could be verifying another claim.
TIMEOUT_S = 12.0
# Full text is a whole paper (measured mean 2.6 s, up to 17 MB across a run), so it gets
# a longer window — but one fewer attempt, because a paper we cannot fetch is a
# quarantine, not a crash, and the abstract has usually already been tried.
FULLTEXT_TIMEOUT_S = 25.0
FULLTEXT_RETRIES = 2
MAX_RETRIES = 3

OPENALEX = "https://api.openalex.org"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Minimum seconds between requests to the same host. OpenAlex and EBI both publish
# generous anonymous limits; these are deliberately conservative because a verification
# run issues a few hundred calls in a burst and a 429 costs more than the wait does.
_MIN_INTERVAL = {
    "api.openalex.org": 0.11,
    "www.ebi.ac.uk": 0.11,
    # NCBI allows 3 requests/second without an API key — stricter than the other two,
    # and we keep the no-key policy, so pace to match rather than earn a ban.
    "eutils.ncbi.nlm.nih.gov": 0.34,
}
_DEFAULT_INTERVAL = 0.2

_lock = threading.Lock()
_last_call: Dict[str, float] = {}

# Endpoints that have told us they are out of quota for the day (see ``_fetch``).
# Once an endpoint says that, every further call to it will say the same until the
# quota resets, so we stop calling it rather than retrying each one three times over.
_metered_out: Dict[str, str] = {}


def metered_out() -> Dict[str, str]:
    """Endpoints skipped this run because their quota was exhausted, and what they said."""
    with _lock:
        return dict(_metered_out)


class HttpStats:
    """Counters for one verification run — surfaced in the report so the cost is visible.

    Incremented from every worker thread in a concurrent run, so each update goes
    through a lock — cheap next to the network call it's counting.

    Beyond the totals there is a **per-endpoint breakdown**, because "600 calls took
    hours" does not say *which* call is expensive. Full-text XML is a whole paper and a
    search is a database query; charging them both to one counter hides the answer.
    Time spent asleep in ``_wait_turn`` is tracked separately so politeness can never be
    mistaken for server latency.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.retries = 0
        self.errors = 0
        self.bytes = 0
        self.paced = 0.0                       # seconds slept for per-host pacing
        self.by_ep: Dict[str, Dict[str, float]] = {}

    def add(self, *, calls: int = 0, retries: int = 0, errors: int = 0, bytes_: int = 0) -> None:
        with self._lock:
            self.calls += calls
            self.retries += retries
            self.errors += errors
            self.bytes += bytes_

    def paced_for(self, seconds: float) -> None:
        with self._lock:
            self.paced += seconds

    def timed(self, label: str, seconds: float, bytes_: int = 0) -> None:
        """Charge one completed request (including its retries) to its endpoint."""
        if not label:
            return
        with self._lock:
            e = self.by_ep.get(label)
            if e is None:
                e = {"calls": 0.0, "seconds": 0.0, "slowest": 0.0, "bytes": 0.0}
                self.by_ep[label] = e
            e["calls"] += 1
            e["seconds"] += seconds
            e["bytes"] += bytes_
            if seconds > e["slowest"]:
                e["slowest"] = seconds

    def breakdown(self) -> str:
        """Per-endpoint table, most expensive first."""
        with self._lock:
            rows = sorted(self.by_ep.items(), key=lambda kv: -kv[1]["seconds"])
            paced = self.paced
        if not rows:
            return "  (no HTTP calls)"
        out = [f"  {'endpoint':<18}{'calls':>7}{'total_s':>10}{'mean_s':>9}"
               f"{'slowest_s':>11}{'KB':>9}"]
        for label, e in rows:
            n = int(e["calls"])
            mean = e["seconds"] / n if n else 0.0
            out.append(f"  {label:<18}{n:>7}{e['seconds']:>10.1f}{mean:>9.2f}"
                       f"{e['slowest']:>11.2f}{int(e['bytes']) // 1024:>9}")
        out.append(f"  {'(pacing sleep)':<18}{'':>7}{paced:>10.1f}")
        return "\n".join(out)

    def __str__(self) -> str:
        return (f"{self.calls} HTTP calls, {self.retries} retries, "
                f"{self.errors} errors, {self.bytes // 1024} KB")


STATS = HttpStats()


# ---------------------------------------------------------------------------
# polite transport
# ---------------------------------------------------------------------------
def _wait_turn(host: str) -> None:
    """Sleep just long enough that this host has not been called too recently."""
    gap = _MIN_INTERVAL.get(host, _DEFAULT_INTERVAL)
    with _lock:
        prev = _last_call.get(host, 0.0)
        now = time.monotonic()
        delay = prev + gap - now
        if delay > 0:
            time.sleep(delay)
            STATS.paced_for(delay)
            now = time.monotonic()
        _last_call[host] = now


def _is_quota_exhausted(err: "urllib.error.HTTPError", label: str) -> bool:
    """Is this 429 "you are going too fast" or "your allowance is gone"?

    OpenAlex now meters its ``/works?`` list endpoint: once the free daily budget is
    spent it answers every request with 429 and *Insufficient budget … Resets at
    midnight UTC*. Waiting and retrying cannot help, so the two cases must be told
    apart — a burst limit is worth a backoff, an exhausted allowance is not.
    """
    try:
        body = err.read()[:400].decode("utf-8", "replace").lower()
    except Exception:
        return False
    if "budget" not in body and "quota" not in body:
        return False
    with _lock:
        _metered_out.setdefault(label or err.url, body.strip()[:200])
    return True


def _fetch(url: str, accept: str = "application/json", label: str = "",
           timeout: Optional[float] = None,
           retries: Optional[int] = None) -> Optional[bytes]:
    """GET with per-host pacing, timeout and backoff. Returns None on a real miss.

    A 404 is a miss and returns immediately — retrying it just wastes the caller's
    time. A 429/5xx is transient and is retried with an increasing delay.

    ``label`` names the endpoint for the per-request timing breakdown; the whole call
    is charged to it, retries and backoff sleeps included, because that is the time the
    caller actually waited.
    """
    with _lock:
        if label in _metered_out:
            return None               # quota gone; do not spend a round-trip finding out
    host = urllib.parse.urlparse(url).netloc
    tmo = TIMEOUT_S if timeout is None else timeout
    tries = MAX_RETRIES if retries is None else max(1, retries)
    delay = 1.0
    t0 = time.monotonic()
    for attempt in range(tries):
        _wait_turn(host)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            STATS.add(calls=1)
            with urllib.request.urlopen(req, timeout=tmo) as resp:
                body = resp.read()
                STATS.add(bytes_=len(body))
                STATS.timed(label, time.monotonic() - t0, len(body))
                return body
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                STATS.timed(label, time.monotonic() - t0)
                return None
            if e.code == 429 and _is_quota_exhausted(e, label):
                # Not a burst limit that waiting fixes — the daily allowance is spent.
                # Retrying costs 3 stalls per call and still fails, which is how a run
                # turns into hours. Give up on this endpoint for the rest of the run.
                STATS.add(errors=1)
                STATS.timed(label, time.monotonic() - t0)
                return None
            if e.code == 429 or 500 <= e.code < 600:
                STATS.add(retries=1)
                if attempt < tries - 1:
                    # Honour Retry-After when the server sends one.
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else delay
                    except ValueError:
                        wait = delay
                    time.sleep(min(wait, 30.0))
                    delay *= 2
                    continue
            STATS.add(errors=1)
            STATS.timed(label, time.monotonic() - t0)
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            STATS.add(retries=1)
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            STATS.add(errors=1)
            STATS.timed(label, time.monotonic() - t0)
            return None
    STATS.timed(label, time.monotonic() - t0)
    return None


def _fetch_json(url: str, label: str = "") -> Optional[Any]:
    raw = _fetch(url, label=label)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        STATS.add(errors=1)
        return None


# ---------------------------------------------------------------------------
# paper record
# ---------------------------------------------------------------------------
class PaperRecord(dict):
    """One paper. Keys mirror ``atlas.db``'s ``paper`` table so contributions merge.

    Extra keys beyond that table: ``pmcid`` (needed to fetch full text) and
    ``fulltext`` (populated lazily, only for open-access records).
    """

    FIELDS = ("doi", "title", "authors", "year", "journal", "licence", "oa_status",
              "abstract", "pmcid", "fulltext", "source")

    @classmethod
    def empty(cls, doi: str = "") -> "PaperRecord":
        r = cls({k: "" for k in cls.FIELDS})
        r["doi"] = doi
        r["year"] = None
        return r

    @property
    def is_oa(self) -> bool:
        return str(self.get("oa_status", "")).lower() in ("gold", "green", "hybrid", "bronze", "diamond")

    @property
    def has_fulltext(self) -> bool:
        return bool(self.get("fulltext"))

    def source_text(self) -> str:
        """Everything a quote may be grounded against: abstract, then full text."""
        return "\n".join(x for x in (self.get("abstract") or "", self.get("fulltext") or "") if x)


DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_ANYWHERE = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;]+")


def bare_doi(s: str) -> str:
    """Normalise anything DOI-ish to the bare ``10.x/y`` form, or '' if there is none."""
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = _DOI_ANYWHERE.search(s)
    if not m:
        return ""
    # Trailing punctuation is common when a DOI was copied out of prose.
    return m.group(0).rstrip(".,;)]}").lower()


def doi_slug(doi: str) -> str:
    """Filename-safe DOI, matching Flash-P_DataBase and the website's convention."""
    return re.sub(r"[^A-Za-z0-9]+", "_", doi or "").strip("_")


_TAG_RE = re.compile(r"<[^>]+>")


def clean_title(title: str) -> str:
    """Strip the inline markup both APIs leave in titles (``<i>``, ``<sub>``, …).

    Crossref-derived titles carry JATS markup verbatim; ``atlas.db`` stores it raw and
    the website has to render it as HTML. Cleaning here means every consumer gets
    plain text, and a title comparison is not thrown off by a stray tag.
    """
    if not title:
        return ""
    t = _TAG_RE.sub(" ", str(title))
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------
# Only the fields ``_oa_map``/``_authors_of`` actually read. Without this OpenAlex
# ships ``referenced_works``, ``related_works``, ``concepts``, ``topics`` and
# ``counts_by_year`` on every hit — a measured 233 KB per search response, almost none
# of it used. Trimming the payload is free speed on the search path especially.
_OA_SELECT = ("id,doi,display_name,title,publication_year,primary_location,"
              "open_access,best_oa_location,abstract_inverted_index,ids,authorships")


def reconstruct_abstract(idx: Optional[Dict[str, List[int]]]) -> str:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index``.

    OpenAlex stores abstracts as {word: [positions]} for licensing reasons; the text
    has to be reassembled position by position.
    """
    if not idx:
        return ""
    try:
        max_pos = max(p for positions in idx.values() for p in positions)
    except ValueError:
        return ""
    words = [""] * (max_pos + 1)
    for word, positions in idx.items():
        for p in positions:
            if 0 <= p <= max_pos:
                words[p] = word
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _oa_map(work: Dict[str, Any]) -> PaperRecord:
    rec = PaperRecord.empty()
    rec["doi"] = bare_doi(work.get("doi") or "")
    rec["title"] = clean_title(work.get("title") or work.get("display_name") or "")
    rec["year"] = work.get("publication_year")
    host = work.get("primary_location") or {}
    src = host.get("source") or {}
    rec["journal"] = (src.get("display_name") or "").strip()
    oa = work.get("open_access") or {}
    rec["oa_status"] = (oa.get("oa_status") or "").strip()
    best = work.get("best_oa_location") or {}
    rec["licence"] = (best.get("license") or "").strip()
    rec["abstract"] = reconstruct_abstract(work.get("abstract_inverted_index"))
    ids = work.get("ids") or {}
    pmcid = ids.get("pmcid") or ""
    if pmcid:
        # OpenAlex gives a full URL; Europe PMC wants the bare PMCnnnnnnn.
        m = re.search(r"(PMC\d+)", str(pmcid))
        rec["pmcid"] = m.group(1) if m else ""
    rec["source"] = "openalex"
    return rec


def _authors_of(work: Dict[str, Any]) -> str:
    names = []
    for a in (work.get("authorships") or [])[:20]:
        n = ((a.get("author") or {}).get("display_name") or "").strip()
        if n:
            names.append(n)
    return ", ".join(names)


def openalex_by_doi(doi: str) -> Optional[PaperRecord]:
    """Authoritative DOI lookup. None means OpenAlex does not know this DOI."""
    d = bare_doi(doi)
    if not d:
        return None
    work = _fetch_json(f"{OPENALEX}/works/doi:{urllib.parse.quote(d, safe='')}"
                       f"?select={_OA_SELECT}", label="openalex/by_doi")
    if not isinstance(work, dict) or not (work.get("id") or work.get("display_name")):
        return None
    rec = _oa_map(work)
    rec["authors"] = _authors_of(work)
    # Trust the DOI we asked for: OpenAlex occasionally echoes a merged record.
    rec["doi"] = d
    return rec


# NOTE — there is deliberately no ``openalex_by_dois``. ``filter=doi:A|B|C`` would fetch
# 50 DOIs in one call, but it goes through ``/works?``, which OpenAlex now **meters**:
# a free account gets a small daily budget and then every list request answers 429
# *"Insufficient budget … Resets at midnight UTC"*. The single-entity ``/works/doi:X``
# route used by ``openalex_by_doi`` is not metered. Bulk DOI lookups therefore go to
# Europe PMC (``epmc_by_dois``), which is free and takes 25 per call.
def openalex_search(query: str, per_page: int = 8,
                    sort: str = "relevance_score:desc") -> List[PaperRecord]:
    """Free-text search, used when a DOI has to be re-found.

    Retracted works and paratext (editorials, covers, indexes) are excluded at the
    source — they are never a valid citation and filtering here is cheaper than
    noticing later.
    """
    q = (query or "").strip()
    if not q:
        return []
    params = urllib.parse.urlencode({
        "search": q,
        "per_page": max(1, min(200, per_page)),
        "filter": "is_retracted:false,is_paratext:false",
        "sort": sort,
        "select": _OA_SELECT,
    })
    data = _fetch_json(f"{OPENALEX}/works?{params}", label="openalex/search")
    out: List[PaperRecord] = []
    for work in (data or {}).get("results", []) if isinstance(data, dict) else []:
        if not isinstance(work, dict):
            continue
        rec = _oa_map(work)
        rec["authors"] = _authors_of(work)
        if rec["doi"]:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------
def _epmc_map(r: Dict[str, Any]) -> PaperRecord:
    rec = PaperRecord.empty()
    rec["doi"] = bare_doi(r.get("doi") or "")
    # Europe PMC titles end in a full stop; strip it so exact title matching works.
    rec["title"] = clean_title(r.get("title") or "").rstrip(".")
    try:
        rec["year"] = int(r.get("pubYear")) if r.get("pubYear") else None
    except (TypeError, ValueError):
        rec["year"] = None
    rec["journal"] = ((r.get("journalInfo") or {}).get("journal") or {}).get("title", "") or ""
    rec["abstract"] = (r.get("abstractText") or "").strip()
    rec["authors"] = (r.get("authorString") or "").strip()
    rec["licence"] = (r.get("license") or "").strip()
    # Europe PMC's own OA flags — the only fields allowed to authorise a full-text fetch.
    is_oa = str(r.get("isOpenAccess", "")).upper() == "Y"
    in_epmc = str(r.get("inEPMC", "")).upper() == "Y"
    rec["pmcid"] = (r.get("pmcid") or "") if (is_oa and in_epmc) else ""
    rec["oa_status"] = "oa" if is_oa else ""
    rec["source"] = "europepmc"
    return rec


def _epmc_query(query: str, page_size: int = 8,
                label: str = "epmc/search") -> List[PaperRecord]:
    params = urllib.parse.urlencode({
        "query": query,
        "format": "json",
        "resultType": "core",      # 'core' is what carries abstractText
        "pageSize": max(1, min(100, page_size)),
    })
    data = _fetch_json(f"{EPMC}/search?{params}", label=label)
    results = (((data or {}).get("resultList") or {}).get("result") or []) \
        if isinstance(data, dict) else []
    return [_epmc_map(r) for r in results if isinstance(r, dict)]


def epmc_by_doi(doi: str) -> Optional[PaperRecord]:
    """Second-opinion DOI lookup; often has an abstract when OpenAlex does not."""
    d = bare_doi(doi)
    if not d:
        return None
    for rec in _epmc_query(f'DOI:"{d}"', page_size=3, label="epmc/by_doi"):
        if rec["doi"] == d:
            return rec
    return None


def epmc_by_dois(dois: List[str], chunk: int = 25) -> Dict[str, PaperRecord]:
    """Many DOIs in one request — ``DOI:"a" OR DOI:"b" …``. Returns ``{doi: record}``.

    Chunked at 25 to keep the query string a sane length; Europe PMC itself would take
    a much larger page. Measured: four DOIs in one 2.3 s call, against ~1.6 s each when
    fetched singly.
    """
    out: Dict[str, PaperRecord] = {}
    ds = [d for d in (bare_doi(x) for x in dois) if d]
    for i in range(0, len(ds), max(1, chunk)):
        part = ds[i:i + max(1, chunk)]
        q = " OR ".join(f'DOI:"{d}"' for d in part)
        for rec in _epmc_query(q, page_size=len(part), label="epmc/batch"):
            if rec["doi"]:
                out[rec["doi"]] = rec
    return out


def epmc_search(query: str, page_size: int = 8) -> List[PaperRecord]:
    q = (query or "").strip()
    return _epmc_query(q, page_size) if q else []


# ---------------------------------------------------------------------------
# PubMed (NCBI E-utilities)
# ---------------------------------------------------------------------------
# Two calls answer a whole search round: ``esearch`` returns PMIDs only (small and
# fast), then one ``efetch`` returns every one of those records *with its abstract* —
# up to 200 in a single request, against 8 per call from the other two. Coverage is not
# the point (Europe PMC already indexes PubMed); the request shape is. It also matters
# that this is genuinely free, which OpenAlex's list endpoint no longer is.
def _pm_text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _pubmed_map(art: ET.Element) -> PaperRecord:
    rec = PaperRecord.empty()
    article = art.find("MedlineCitation/Article")
    if article is None:
        return rec

    rec["title"] = clean_title(_pm_text(article.find("ArticleTitle"))).rstrip(".")
    journal = article.find("Journal")
    if journal is not None:
        rec["journal"] = _pm_text(journal.find("Title"))
        year = _pm_text(journal.find("JournalIssue/PubDate/Year"))
        if not year:
            # Older records carry a free-text date like "1998 Mar-Apr" instead.
            m = re.search(r"(\d{4})", _pm_text(journal.find("JournalIssue/PubDate/MedlineDate")))
            year = m.group(1) if m else ""
        try:
            rec["year"] = int(year) if year else None
        except ValueError:
            rec["year"] = None

    # Structured abstracts arrive as several <AbstractText Label="RESULTS"> chunks. The
    # labels are deliberately dropped: a supporting quote has to be a verbatim substring
    # of the paper's own text, and "RESULTS: " is our punctuation, not theirs.
    parts = [t for t in (_pm_text(x) for x in article.findall("Abstract/AbstractText")) if t]
    rec["abstract"] = " ".join(parts)

    names = []
    for a in article.findall("AuthorList/Author")[:20]:
        last, fore = _pm_text(a.find("LastName")), _pm_text(a.find("ForeName"))
        if last:
            names.append(f"{fore} {last}".strip())
    rec["authors"] = ", ".join(names)

    # Only ever the record's *own* id block. A PubmedArticle also contains the id of
    # every work in its reference list, and matching those returns another paper's DOI.
    doi = ""
    ids = art.find("PubmedData/ArticleIdList")
    if ids is not None:
        for aid in ids.findall("ArticleId"):
            if (aid.get("IdType") or "").lower() == "doi":
                doi = _pm_text(aid)
                break
    if not doi:
        for el in article.findall("ELocationID"):
            if (el.get("EIdType") or "").lower() == "doi":
                doi = _pm_text(el)
                break
    rec["doi"] = bare_doi(doi)

    # ``pmcid`` is left empty on purpose. PubMed reports that a record exists in PMC but
    # not that it is open access, and only an OA flag from the source may authorise a
    # full-text fetch (see the module docstring). PubMed hits are judged on abstracts;
    # if one becomes the chosen DOI, the ladder re-fetches it through Europe PMC, which
    # does carry the OA flags.
    rec["source"] = "pubmed"
    return rec


def pubmed_by_pmids(pmids: List[str], chunk: int = 200) -> List[PaperRecord]:
    """Full records, with abstracts, for up to 200 PMIDs per request."""
    out: List[PaperRecord] = []
    ids = [str(p).strip() for p in pmids if str(p).strip().isdigit()]
    for i in range(0, len(ids), max(1, chunk)):
        params = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(ids[i:i + max(1, chunk)]), "retmode": "xml"})
        raw = _fetch(f"{NCBI}/efetch.fcgi?{params}", accept="application/xml",
                     label="pubmed/efetch")
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            STATS.add(errors=1)
            continue
        for art in root.findall(".//PubmedArticle"):
            rec = _pubmed_map(art)
            if rec.get("doi"):
                out.append(rec)
    return out


def pubmed_search(query: str, per_page: int = 8) -> List[PaperRecord]:
    """Free-text search over PubMed: PMIDs from ``esearch``, records from one ``efetch``."""
    q = (query or "").strip()
    if not q:
        return []
    params = urllib.parse.urlencode({
        "db": "pubmed", "term": q, "retmode": "json",
        "retmax": max(1, min(200, per_page)), "sort": "relevance",
    })
    data = _fetch_json(f"{NCBI}/esearch.fcgi?{params}", label="pubmed/esearch")
    ids = (((data or {}).get("esearchresult") or {}).get("idlist") or []) \
        if isinstance(data, dict) else []
    return pubmed_by_pmids(ids)


# ---------------------------------------------------------------------------
# NCBI Taxonomy (species-name validation, on demand)
# ---------------------------------------------------------------------------
# NCBI Taxonomy tracks hybrid crosses and cultivar placeholders at "species" rank too —
# "Triticum hybrid cultivar", "Triticum aestivum x Triticosecale sp." — and a naive
# genus+second-word split reads their second token as if it were a real epithet. None of
# these words is a genuine species epithet in any genus, so they are dropped outright.
TAXONOMY_JUNK_EPITHETS = {
    "hybrid", "cultivar", "sp", "spp", "aff", "cf", "group", "complex",
    "unclassified", "uncultured", "environmental", "clade", "var", "forma", "form",
}


def ncbi_taxonomy_species(genus: str, retmax_page: int = 500, max_pages: int = 4) -> set:
    """Every species-rank epithet NCBI Taxonomy knows for one genus, lowercased.

    This is ``species.py``'s allowlist extended live: a network built for a crop the
    static seed list (``species_data.json``) never anticipated still needs its species
    recognised, not silently guessed at from whatever else the abstract mentions. Capped
    at ``max_pages`` x ``retmax_page`` taxon ids so one implausibly large genus cannot
    stall a verification run — a signaling-network paper's genus is never that big in
    practice, and the cap is generous enough (2,000 by default) that it will not bite.

    Returns an empty set on any failure, a misspelt genus, or a genus NCBI does not
    recognise — never raises, since a failed lookup must read as "unknown", exactly like
    every other miss in this module, not crash the claim that triggered it.
    """
    g = (genus or "").strip()
    if not g or not g[0].isalpha():
        return set()
    term = f"{g}[Subtree] AND species[Rank]"
    ids: List[str] = []
    retstart = 0
    for _ in range(max(1, max_pages)):
        params = urllib.parse.urlencode({
            "db": "taxonomy", "term": term, "retmode": "json",
            "retmax": retmax_page, "retstart": retstart,
        })
        data = _fetch_json(f"{NCBI}/esearch.fcgi?{params}", label="taxonomy/esearch")
        result = (data or {}).get("esearchresult") if isinstance(data, dict) else None
        batch = (result or {}).get("idlist") or []
        ids.extend(batch)
        if not batch:
            break
        try:
            count = int((result or {}).get("count", 0))
        except (TypeError, ValueError):
            count = 0
        retstart += len(batch)
        if retstart >= count:
            break
    if not ids:
        return set()

    epithets: set = set()
    low = g.lower()
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        params = urllib.parse.urlencode({
            "db": "taxonomy", "id": ",".join(chunk), "retmode": "json",
        })
        data = _fetch_json(f"{NCBI}/esummary.fcgi?{params}", label="taxonomy/esummary")
        result = (data or {}).get("result") if isinstance(data, dict) else None
        for uid in (result or {}).get("uids", []):
            name = ((result or {}).get(uid) or {}).get("scientificname", "")
            if not name or " x " in name:            # hybrid cross
                continue
            parts = name.split()
            if len(parts) < 2:
                continue
            gpart, sp = parts[0].lower(), parts[1].lower()
            if (gpart == low and len(sp) >= 3 and sp.isalpha()
                    and sp not in TAXONOMY_JUNK_EPITHETS):
                epithets.add(sp)
    return epithets


# ---------------------------------------------------------------------------
# Europe PMC full text (open access only)
# ---------------------------------------------------------------------------
_DROP_SECTIONS = re.compile(
    r"^(references?|bibliography|acknowledge?ments?|author contributions?|"
    r"competing interests?|conflicts? of interest|funding|supplementary)", re.I)


def _xml_text(el: ET.Element) -> str:
    """All text under an element, tags stripped, whitespace collapsed."""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _sec_to_lines(sec: ET.Element, out: List[str], depth: int = 0) -> None:
    title_el = sec.find("title")
    title = _xml_text(title_el) if title_el is not None else ""
    if title and _DROP_SECTIONS.match(title):
        return
    if title:
        out.append(f"## {title}")
    for child in sec:
        tag = child.tag.split("}")[-1]
        if tag == "title":
            continue
        if tag == "sec":
            _sec_to_lines(child, out, depth + 1)
        elif tag in ("p", "list", "disp-quote", "statement"):
            t = _xml_text(child)
            if t:
                out.append(t)


def _jats_to_text(xml_bytes: bytes) -> str:
    """JATS full-text XML -> plain text with ``## Section`` headings.

    The heading convention is deliberately identical to
    ``Flash-P_DataBase/extract/fetch_fulltext.py`` so the same text renders in the
    Studio drawer and on the website without a second parser.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""

    out: List[str] = []

    def find(tag: str):
        return root.find(f".//{tag}") if root.find(f".//{tag}") is not None else None

    title_el = find("article-title")
    if title_el is not None:
        t = _xml_text(title_el)
        if t:
            out += ["## Title", t]

    abs_el = find("abstract")
    if abs_el is not None:
        parts = [_xml_text(p) for p in abs_el.iter() if p.tag.split("}")[-1] == "p"]
        parts = [p for p in parts if p]
        if parts:
            out += ["## Abstract"] + parts

    body = find("body")
    if body is not None:
        for child in body:
            tag = child.tag.split("}")[-1]
            if tag == "sec":
                _sec_to_lines(child, out)
            elif tag == "p":
                t = _xml_text(child)
                if t:
                    out.append(t)

    return "\n".join(out).strip()


def epmc_fulltext(pmcid: str) -> str:
    """Open-access full text as sectioned plain text. '' when unavailable.

    Only ever called with a PMCID that Europe PMC itself flagged ``isOpenAccess=Y``
    and ``inEPMC=Y`` — see ``_epmc_map``. Closed-access papers never reach here.
    """
    pid = (pmcid or "").strip()
    if not pid.upper().startswith("PMC"):
        return ""
    raw = _fetch(f"{EPMC}/{pid}/fullTextXML", accept="application/xml",
                 label="epmc/fulltext", timeout=FULLTEXT_TIMEOUT_S,
                 retries=FULLTEXT_RETRIES)
    if not raw:
        return ""
    return _jats_to_text(raw)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
def merge_records(primary: Optional[PaperRecord],
                  secondary: Optional[PaperRecord]) -> Optional[PaperRecord]:
    """Fill gaps in ``primary`` from ``secondary`` without overwriting real values.

    OpenAlex and Europe PMC each know things the other does not: OpenAlex has better
    OA status and journal names, Europe PMC has abstracts for a lot of biology that
    OpenAlex leaves empty, and only Europe PMC gives a usable PMCID.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    merged = PaperRecord(primary)
    for k in ("title", "journal", "abstract", "pmcid", "licence", "oa_status", "authors"):
        if not merged.get(k) and secondary.get(k):
            merged[k] = secondary[k]
    if not merged.get("year") and secondary.get("year"):
        merged["year"] = secondary["year"]
    return merged


if __name__ == "__main__":
    # Live self-test — hits the network. A known open-access plant paper.
    doi = "10.1105/tpc.106.048934"
    print(f"openalex_by_doi({doi})")
    rec = openalex_by_doi(doi)
    if rec:
        print(f"  title    : {rec['title'][:70]}")
        print(f"  year     : {rec['year']}  journal: {rec['journal'][:40]}")
        print(f"  oa_status: {rec['oa_status']}  pmcid: {rec['pmcid']}")
        print(f"  abstract : {len(rec['abstract'])} chars")
    else:
        print("  MISS")

    print(f"\nepmc_by_doi({doi})")
    e = epmc_by_doi(doi)
    if e:
        print(f"  pmcid: {e['pmcid']}  abstract: {len(e['abstract'])} chars")
        if e["pmcid"]:
            ft = epmc_fulltext(e["pmcid"])
            print(f"  fulltext: {len(ft)} chars, "
                  f"{sum(1 for ln in ft.splitlines() if ln.startswith('## '))} sections")
    else:
        print("  MISS")

    print(f"\nsearch: {len(openalex_search('BRANCHED1 axillary bud outgrowth Arabidopsis', 5))} OpenAlex hits, "
          f"{len(epmc_search('BRANCHED1 axillary bud outgrowth', 5))} Europe PMC hits")
    print(f"\n{STATS}")

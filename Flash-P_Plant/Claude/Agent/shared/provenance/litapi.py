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
    "epmc_by_doi", "epmc_search", "epmc_fulltext",
    "reconstruct_abstract", "bare_doi", "doi_slug",
]

USER_AGENT = "FLASH-P/1.0 (provenance verification; https://github.com/flash-p)"
TIMEOUT_S = 20.0
MAX_RETRIES = 3

OPENALEX = "https://api.openalex.org"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# Minimum seconds between requests to the same host. OpenAlex and EBI both publish
# generous anonymous limits; these are deliberately conservative because a verification
# run issues a few hundred calls in a burst and a 429 costs more than the wait does.
_MIN_INTERVAL = {
    "api.openalex.org": 0.11,
    "www.ebi.ac.uk": 0.11,
}
_DEFAULT_INTERVAL = 0.2

_lock = threading.Lock()
_last_call: Dict[str, float] = {}


class HttpStats:
    """Counters for one verification run — surfaced in the report so the cost is visible.

    Incremented from every worker thread in a concurrent run, so each update goes
    through a lock — cheap next to the network call it's counting.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.retries = 0
        self.errors = 0
        self.bytes = 0

    def add(self, *, calls: int = 0, retries: int = 0, errors: int = 0, bytes_: int = 0) -> None:
        with self._lock:
            self.calls += calls
            self.retries += retries
            self.errors += errors
            self.bytes += bytes_

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
            now = time.monotonic()
        _last_call[host] = now


def _fetch(url: str, accept: str = "application/json") -> Optional[bytes]:
    """GET with per-host pacing, timeout and backoff. Returns None on a real miss.

    A 404 is a miss and returns immediately — retrying it just wastes the caller's
    time. A 429/5xx is transient and is retried with an increasing delay.
    """
    host = urllib.parse.urlparse(url).netloc
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        _wait_turn(host)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            STATS.add(calls=1)
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                body = resp.read()
                STATS.add(bytes_=len(body))
                return body
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None
            if e.code == 429 or 500 <= e.code < 600:
                STATS.add(retries=1)
                if attempt < MAX_RETRIES - 1:
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
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            STATS.add(retries=1)
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            STATS.add(errors=1)
            return None
    return None


def _fetch_json(url: str) -> Optional[Any]:
    raw = _fetch(url)
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
    work = _fetch_json(f"{OPENALEX}/works/doi:{urllib.parse.quote(d, safe='')}")
    if not isinstance(work, dict) or not (work.get("id") or work.get("display_name")):
        return None
    rec = _oa_map(work)
    rec["authors"] = _authors_of(work)
    # Trust the DOI we asked for: OpenAlex occasionally echoes a merged record.
    rec["doi"] = d
    return rec


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
        "per_page": max(1, min(25, per_page)),
        "filter": "is_retracted:false,is_paratext:false",
        "sort": sort,
    })
    data = _fetch_json(f"{OPENALEX}/works?{params}")
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


def _epmc_query(query: str, page_size: int = 8) -> List[PaperRecord]:
    params = urllib.parse.urlencode({
        "query": query,
        "format": "json",
        "resultType": "core",      # 'core' is what carries abstractText
        "pageSize": max(1, min(25, page_size)),
    })
    data = _fetch_json(f"{EPMC}/search?{params}")
    results = (((data or {}).get("resultList") or {}).get("result") or []) \
        if isinstance(data, dict) else []
    return [_epmc_map(r) for r in results if isinstance(r, dict)]


def epmc_by_doi(doi: str) -> Optional[PaperRecord]:
    """Second-opinion DOI lookup; often has an abstract when OpenAlex does not."""
    d = bare_doi(doi)
    if not d:
        return None
    for rec in _epmc_query(f'DOI:"{d}"', page_size=3):
        if rec["doi"] == d:
            return rec
    return None


def epmc_search(query: str, page_size: int = 8) -> List[PaperRecord]:
    q = (query or "").strip()
    return _epmc_query(q, page_size) if q else []


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
    raw = _fetch(f"{EPMC}/{pid}/fullTextXML", accept="application/xml")
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

#!/usr/bin/env python3
"""Shared helpers: locating the gene-id-resolver skill, and validating species strings.

Kept separate from the tools themselves so the skill location and the species vocabulary
are decided in exactly one place. Both change when this ships to someone who is not us.
"""
import functools
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Exit codes. These are the command file's contract with the pipeline, so they are
# defined once here rather than invented per script.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_A_NETWORK = 2    # no network/network.json and no flat network.json
EXIT_NO_EVIDENCE = 3      # pre-Step-1.6 network; backfill or pass --allow-no-evidence
EXIT_NO_GENE_NODES = 4    # nothing mappable in this network
EXIT_JUDGEMENT_REJECTED = 5   # a judgement broke an output rule; fix it, do not force it
EXIT_STALE_OUTPUT = 6         # <NET>/idmapping/ holds output from the superseded mapper


def _flashp_version():
    """The repo's git-tag-derived version. Never hand-typed -- house rule."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import flashp_version
        return flashp_version.get_version()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Two cache roots, and the split between them is deliberate.
#
# The READ root is the resolver library vendored beside this file: the 267-species name
# cache, the species manifest and the identifier patterns. It ships with the repo and is
# static -- a clone can map a network offline the moment it lands, which is the whole
# point of committing it.
#
# The WRITE root holds everything built on demand: NCBI description layers, PLAZA pair
# tables, Compara projections, taxonomy lineages. It is large, regenerable and
# machine-local, so it lives under .flashp_cache/ with the rest of the regenerable state
# and is never committed.
#
# Keeping them apart is what stops a first run on a new species from dirtying the working
# tree with a rebuilt cache file: readers fall back from write root to read root, writers
# only ever touch the write root.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))

_CANDIDATE_RESOLVER_DIRS = [
    os.environ.get("FLASHP_IDMAP_RESOLVER", ""),
    os.path.join(_HERE, "resolver"),
    os.environ.get("GENE_ID_RESOLVER_DIR", ""),          # legacy alias
    os.path.expanduser("~/.claude/skills/gene-id-resolver"),
]

# Agent/shared/idmap -> Agent/shared -> Agent -> <project root>/.flashp_cache/idmap
_DEFAULT_CACHE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(_HERE))), ".flashp_cache", "idmap")
CACHE_ROOT = os.environ.get("FLASHP_IDMAP_CACHE", _DEFAULT_CACHE_ROOT)

USER_AGENT = "flashp-idmap/%s (Flash-P gene identifier mapping)" % _flashp_version()


# ---------------------------------------------------------------------------
# Host rate limiting
#
# Ensembl publishes its budget in the response headers -- 55,000 requests an hour, about
# fifteen a second -- and a serial run of this tool spends roughly 3% of it. So the limit
# that bites first is not the hourly quota but the per-second burst, and NCBI's burst
# limit is five times tighter than Ensembl's. Holding the budget per host rather than as
# one global worker count is what makes that difference expressible: every thread draws
# from the same bucket, so no setting of the node-level worker count can breach a limit.
#
# Nothing here used to handle 429 at all, which meant a breach was invisible -- the reason
# the buckets are paired with Retry-After handling below rather than shipped alone.
# ---------------------------------------------------------------------------
HOST_RATE = {
    "rest.ensembl.org": 5.0,
    "data.gramene.org": 5.0,
    "eutils.ncbi.nlm.nih.gov": 3.0,   # E-utilities allows 3/s without an API key
    "rest.uniprot.org": 3.0,
    "www.ebi.ac.uk": 3.0,
    "ftp.ebi.ac.uk": 2.0,
}
DEFAULT_HOST_RATE = 2.0

# Start spreading requests out once a host says this little of its quota is left, rather
# than sprinting into the wall and taking the lockout.
QUOTA_FLOOR = 1000

_buckets = {}
_bucket_lock = threading.Lock()


def _host_of(url):
    try:
        return urllib.parse.urlsplit(str(url)).netloc.lower()
    except Exception:
        return ""


def _bucket(host):
    return _buckets.setdefault(host, {"next": 0.0, "until": 0.0,
                                      "interval": 1.0 / HOST_RATE.get(host,
                                                                      DEFAULT_HOST_RATE)})


def throttle(url):
    """Block until this host's bucket allows another request."""
    host = _host_of(url)
    with _bucket_lock:
        now = time.monotonic()
        b = _bucket(host)
        start = max(b["next"], b["until"], now)
        b["next"] = start + b["interval"]
        wait = start - now
    if wait > 0:
        time.sleep(wait)


def back_off(url, seconds):
    """Hold every thread off a host for a while, after a 429 or a spent quota."""
    host = _host_of(url)
    with _bucket_lock:
        b = _bucket(host)
        b["until"] = max(b["until"], time.monotonic() + max(0.0, float(seconds or 0)))


def note_headers(url, headers):
    """Read a published budget off the response and slow down before it runs out.

    Ensembl returns x-ratelimit-remaining and x-ratelimit-reset on every call. When the
    remainder gets low the right response is not to stop but to stretch: spread what is
    left over the seconds left in the window, so a long run degrades to a crawl instead of
    hitting a hard lockout partway through a network.
    """
    try:
        remaining = int(headers.get("x-ratelimit-remaining"))
    except (AttributeError, TypeError, ValueError):
        return
    try:
        reset = int(headers.get("x-ratelimit-reset") or 0)
    except (TypeError, ValueError):
        reset = 0
    if remaining <= 0:
        back_off(url, reset or 60)
        return
    if remaining < QUOTA_FLOOR and reset > 0:
        host = _host_of(url)
        with _bucket_lock:
            b = _bucket(host)
            b["interval"] = max(b["interval"], float(reset) / float(remaining))


def _retry_after(exc, default=5.0):
    hdrs = getattr(exc, "headers", None)
    raw = hdrs.get("Retry-After") if hdrs else None
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return default


# Transient on the server's side: worth another attempt, unlike a 400 or a 404 which are
# answers about the request itself.
_RETRY_CODES = (429, 500, 502, 503, 504)


def open_url(req, timeout=30, tries=4):
    """urlopen with the host budget respected and 429 handled.

    Returns the response object, so callers keep their own parsing and their own error
    semantics -- several of them distinguish 'the server answered and holds nothing' from
    'the server never answered', and collapsing those turns an outage into a confident
    statement about a gene.
    """
    url = req.full_url if hasattr(req, "full_url") else str(req)
    for t in range(tries):
        throttle(url)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_CODES and t < tries - 1:
                back_off(url, _retry_after(exc) if exc.code == 429 else 2.0 * (t + 1))
                continue
            raise
        except Exception:
            if t < tries - 1:
                time.sleep(2.0 * (t + 1))
                continue
            raise
        note_headers(url, resp.headers)
        return resp
    raise urllib.error.URLError(f"gave up on {url} after {tries} attempts")



def resolver_dir():
    """The vendored resolver library: modules plus the committed name cache."""
    for d in _CANDIDATE_RESOLVER_DIRS:
        if d and os.path.isdir(d) and os.path.isfile(os.path.join(d, "species_resolver.py")):
            return d
    sys.exit("cannot find the resolver library. Set FLASHP_IDMAP_RESOLVER to its directory.")


def resolver_script(name):
    return os.path.join(resolver_dir(), name)


def resolver_read(*parts):
    """Read a cache file, preferring a locally rebuilt copy over the shipped one.

    A layer rebuilt into the write root shadows the committed one, so refreshing a
    species never means editing a tracked file.
    """
    built = os.path.join(CACHE_ROOT, *parts)
    if os.path.exists(built):
        return built
    return os.path.join(resolver_dir(), "cache", *parts)


def resolver_write(*parts):
    """Where a rebuilt cache file goes. Always the write root, never the committed tree."""
    return cache_path(*parts)


@functools.lru_cache(maxsize=1)
def _resolver():
    sys.path.insert(0, resolver_dir())
    import species_resolver  # noqa: E402
    return species_resolver


# ---------------------------------------------------------------------------
# The ortholog tables on disk
#
# One species pair costs about 15 MB as plain JSON -- 7.5 MB each way for Arabidopsis and
# canola -- against 1.8 MB for the same rows as a compressed TSV. The verbosity is the
# price of the format being a direct memo lookup, one keyed record per ortholog, and that
# is worth keeping. Gzipping the file instead recovers almost all of it: the same pair
# stores in 1.6 MB, a tenfold saving, for a decompression cost paid once per process that
# touches the pair. At four pairs it would not matter; a user working across many species
# accumulates these tables one pair at a time and it starts to.
#
# Reads accept a legacy uncompressed file so nothing already cached is lost, and writes
# always produce the gzipped form, retiring the plain file as they go.
# ---------------------------------------------------------------------------
def ortholog_cache_path(src, tgt, legacy=False):
    return cache_path("orthologs", f"{src}__{tgt}" + (".json" if legacy else ".json.gz"))


def load_ortholog_cache(src, tgt):
    """A pair's table, from the gzipped file or from a legacy uncompressed one."""
    for legacy in (False, True):
        path = ortholog_cache_path(src, tgt, legacy)
        opener = open if legacy else gzip.open
        try:
            with opener(path, "rt") as fh:
                return json.load(fh)
        except FileNotFoundError:
            continue
        except Exception:
            # A truncated or half-written table is not an answer about any gene; treat it
            # as absent and let the routes fill it again.
            return {}
    return {}


def save_ortholog_cache(src, tgt, table):
    """Write a pair's table gzipped and atomically, retiring any legacy plain file."""
    path = ortholog_cache_path(src, tgt)
    tmp = f"{path}.tmp{os.getpid()}"
    with gzip.open(tmp, "wt", compresslevel=6) as fh:
        json.dump(table, fh)
    os.replace(tmp, path)
    legacy = ortholog_cache_path(src, tgt, legacy=True)
    if os.path.exists(legacy):
        os.remove(legacy)


def ortholog_pair_key(a, b):
    """Canonical name for a species pair.

    One scan of a Compara dump writes the ortholog tables for both directions, so a bulk
    fetch triggered as (A, B) and one triggered as (B, A) are the same job. Naming the
    marker and the lock by the unordered pair is what stops the second one repeating the
    first: keyed separately, a barley run downloaded the same dumps twice and added nothing
    the second time. The cached tables themselves stay directional -- each is keyed by the
    genes of its own source species -- and are not renamed by this.
    """
    return "__".join(sorted((a, b)))


def cache_path(*parts):
    p = os.path.join(CACHE_ROOT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def network_json(net_dir):
    """Path to a network's network.json, or None.

    Flash-P writes <NET>/network/network.json, but the older flat <NET>/network.json is
    still around and every sibling analysis command accepts both. Same here.
    """
    for rel in (("network", "network.json"), ("network.json",)):
        p = os.path.join(net_dir, *rel)
        if os.path.isfile(p):
            return p
    return None


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def write_json(path, obj):
    """Write via a temporary file so an interrupted write cannot truncate the dossier."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def same_species(a, b):
    """Whether two Ensembl species names refer to the same organism.

    Ensembl REST requires the GCA-suffixed form for some species, so the same organism
    appears as both `solanum_lycopersicum` and `solanum_lycopersicum_gca000188115v5cm`.
    """
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b + "_gca") or b.startswith(a + "_gca"))


def _disk_cache(name):
    path = cache_path("http", name)
    try:
        with open(path) as fh:
            return json.load(fh), path
    except Exception:
        return {}, path


def _save_cache(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=0, sort_keys=True)
    os.replace(tmp, path)


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with open_url(req, timeout=timeout) as fh:
        return json.load(fh)


_TAXON_CACHE = None
_TAXON_PATH = None


def ncbi_taxon_known(name):
    """Does NCBI Taxonomy recognise this string as an organism?

    This is the only reliable way to tell a real species from one of Flash-P's species
    detection artefacts. Flash-P reads the organism off the sentence and sometimes pairs a
    correctly-detected genus with the following word -- "Arabidopsis inflorescence",
    "Sorghum stay". Guessing from the shape of the epithet does not work: it would throw
    away "Hordeum spontaneum" and "Nicotiana benthamiana", which are real species that
    happen to have no Ensembl Plants assembly, and each of which is a whole network's
    declared species.

    A free-text taxonomy search is used rather than a [Scientific Name] search because
    NCBI files wild barley under Hordeum vulgare subsp. spontaneum, so the exact-name
    search misses it while the free-text search finds it.

    Returns the taxon id, or None. Results are cached to disk: one call per distinct
    string per installation.
    """
    global _TAXON_CACHE, _TAXON_PATH
    if _TAXON_CACHE is None:
        _TAXON_CACHE, _TAXON_PATH = _disk_cache("ncbi_taxonomy.json")
    key = (name or "").strip()
    if not key:
        return None
    if key in _TAXON_CACHE:
        return _TAXON_CACHE[key]

    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
           f"?db=taxonomy&term={urllib.parse.quote(key)}&retmode=json&retmax=1")
    taxid = None
    for attempt in range(3):
        try:
            ids = _get_json(url).get("esearchresult", {}).get("idlist", [])
            taxid = int(ids[0]) if ids else None
            break
        except Exception:
            if attempt == 2:
                # Do not cache a network failure as "not a species" -- that would silently
                # convert an outage into a permanent wrong answer.
                raise SystemExit(
                    "NCBI Taxonomy is unreachable. This tool needs network access; "
                    "re-run when the connection is available."
                )
            time.sleep(1.5 * (attempt + 1))
    _TAXON_CACHE[key] = taxid
    _save_cache(_TAXON_CACHE, _TAXON_PATH)
    return taxid


# Accepts hybrids: "Fragaria x ananassa" and "Fragaria × ananassa" are how strawberry and
# several other crop species are written.
BINOMIAL_RE = re.compile(r"^([A-Z][a-z]{2,})\s+(?:[x×]\s+)?([a-z][a-z\-]{2,})(?:\s+.*)?$")


@functools.lru_cache(maxsize=512)
def classify_species(token):
    """Judge a species string coming out of Flash-P evidence.

    status is one of:
      ensembl   resolved to an Ensembl Plants species; ensembl_name is usable directly
      outside   a real organism with no Ensembl Plants assembly; needs a proxy species
      suspect   not a known organism -- a Flash-P species-detection artefact; ignore it
      unusable  not shaped like a species name at all
    """
    token = (token or "").strip()
    if not token:
        return {"raw": token, "status": "unusable", "ensembl_name": None, "binomial": None}

    sr = _resolver()
    # strict=False returns None on a miss; strict=True raises SystemExit and prints to
    # stdout, which is the CLI's error path and not usable from inside another tool.
    sp = sr.resolve_species(token, strict=False)
    if sp:
        return {
            "raw": token,
            "status": "ensembl",
            "ensembl_name": sp.get("ensembl_name"),
            "binomial": sp.get("binomial") or token,
            "n_names": int(sp.get("n_names") or 0),
            "taxid": sp.get("taxid"),
        }

    if not BINOMIAL_RE.match(token):
        return {"raw": token, "status": "unusable", "ensembl_name": None, "binomial": None}

    taxid = ncbi_taxon_known(token)
    return {
        "raw": token,
        "status": "outside" if taxid else "suspect",
        "ensembl_name": None,
        "binomial": re.sub(r"\s+", " ", token).strip(),
        "n_names": 0,
        "taxid": taxid,
    }


def species_weight(status):
    """How much a species label counts when deciding where a gene name comes from."""
    return {"ensembl": 1.0, "outside": 1.0, "suspect": 0.0, "unusable": 0.0}.get(status, 0.0)


# Identifier systems that coexist with a species' current reference annotation. Papers and
# older databases keep using them for years after a genome is re-released, so they arrive
# through the literature and database routes and look exactly like answers. Labelling them
# `ensembl` -- which is what happened before this table existed -- makes them silently
# unjoinable: measured across the eleven mapped networks, sorghum emitted Phytozome v3.1 and
# Sbi1.4 accessions, wheat emitted v2.1 alongside v1.1, and maize emitted v3 and v4 against
# a v5 assembly, all labelled as the reference system.
#
# Checked before the reference pattern, because some are subsumed by it: wheat's
# TraesCS3A03G... matches the general TraesCS pattern while belonging to a later release.
ALT_ID_SYSTEMS = {
    "sorghum_bicolor": [("phytozome_v3", r"Sobic\.\d+G\d+$"),
                        ("sbi_v1_4", r"Sb\d{2}g\d{6}$")],
    "zea_mays": [("maize_v3", r"(?:GRMZM\d[GT]\d{6}|AC\d{6}\.\d+_FG\d+)$"),
                 ("maize_v4", r"Zm00001d\d{6}$")],
    "oryza_sativa": [("msu_v7", r"LOC_Os\d{2}g\d{5}$"),
                     ("rapdb", r"Os\d{2}[tg]\d{7}$")],
    "triticum_aestivum": [("iwgsc_v2_1", r"TraesCS\d+[ABDU]03G\d+$")],
    "hordeum_vulgare": [("morex_v2", r"HORVU\d[HU]\d+G\d+$|Horvu_MOREX_\w+$")],
}


# Identifiers that are Ensembl's but that a pattern derived from protein-coding genes will
# not match: non-coding RNA genes have their own prefix, and unplaced scaffolds substitute a
# 'U' for the chromosome. Both showed up as `unrecognised` on the first run -- correctly, in
# that nothing vouched for their shape, and wrongly, in that they are the reference system.
REFERENCE_EXTRA = {
    "*": [r"ENSRNA\d+$"],
    "triticum_aestivum": [r"TraesCSU\d+G\d+$"],
}


# ---------------------------------------------------------------------------
# Taxonomy: how closely two species are related, measured rather than assumed.
# ---------------------------------------------------------------------------
#
# Needed because "which well-annotated species should we borrow a gene symbol from" has no
# single answer. Arabidopsis is the usual one and is right for a great deal of plant
# nomenclature, but a sorghum photoperiod network is full of grass-specific names -- Ghd7,
# Ehd1, PRR37 -- coined in rice, and probing Arabidopsis for those either misses or, worse,
# lands on a homonym: EHD1 in Arabidopsis is an endocytosis protein with no connection to
# flowering, and it projects three sorghum orthologs with every support flag set.
#
# The ranking comes from NCBI's taxonomy rather than from a table of clades kept here, so a
# user working on a species none of us anticipated gets a sensible ordering from its own
# lineage. Fetched once for every species in the cache and stored; it never changes.

_LINEAGES = None


def _fetch_lineages(taxids, chunk=180):
    """{taxid: [clade, ...]} from NCBI Taxonomy."""
    import urllib.request
    import xml.etree.ElementTree as ET
    out = {}
    ids = [str(t) for t in taxids if t]
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=taxonomy&id="
               + ",".join(batch) + "&retmode=xml")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with open_url(req, timeout=90) as fh:
                root = ET.fromstring(fh.read())
        except Exception as exc:
            print(f"  taxonomy fetch failed for {len(batch)} species: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        # Only top-level <Taxon> children: each carries nested <Taxon> elements for its own
        # lineage, and reading those as separate records yields the same lineage many times.
        for t in root.findall("Taxon"):
            tid = t.findtext("TaxId")
            lin = [p.strip() for p in (t.findtext("Lineage") or "").split(";") if p.strip()]
            if tid and lin:
                out[tid] = lin + [t.findtext("ScientificName") or ""]
    return out


def _taxid_by_name(ens_name):
    """NCBI taxid for a species, looked up by its binomial. None when nothing matches."""
    import urllib.request
    import xml.etree.ElementTree as ET
    parts = re.split(r"_gca|_", ens_name or "")
    binomial = " ".join(p for p in parts[:2] if p and not p[0].isdigit())
    if len(binomial.split()) < 2:
        return None
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=taxonomy&term="
           + urllib.parse.quote(binomial) + "&retmax=1")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with open_url(req, timeout=30) as fh:
            root = ET.fromstring(fh.read())
        return (root.findtext(".//Id") or "").strip() or None
    except Exception:
        return None


def _lineage_path():
    return cache_path("taxonomy", "lineages.tsv")


def _load_lineages():
    global _LINEAGES
    if _LINEAGES is not None:
        return _LINEAGES
    table = {}
    path = _lineage_path()
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                sp, _, lin = line.rstrip("\n").partition("\t")
                if sp:
                    table[sp] = lin.split("|") if lin else []
    _LINEAGES = table
    return table


def ensure_lineages(ens_names):
    """Fetch and cache the lineages of any of these species not already known.

    Resolved from each species' own taxid rather than from a shipped list, so it covers
    every species the resolver knows and any that are added later. One batched request for
    whatever is missing; species that resolve to nothing are recorded as empty so a second
    run does not ask again.
    """
    table = _load_lineages()
    want = {n for n in ens_names if n and n not in table}
    if not want:
        return table
    by_tax, taxids = {}, {}
    for n in sorted(want):
        t = (classify_species(n) or {}).get("taxid")
        if not t:
            # The shipped taxid list covers only a handful of species. Everything else is
            # resolved from its own name, which is what makes this work for a species the
            # cache was never told about.
            t = _taxid_by_name(n)
        if t:
            taxids[str(t)] = n
    if taxids:
        print(f"fetching NCBI lineages for {len(taxids)} species...", file=sys.stderr)
        by_tax = _fetch_lineages(taxids.keys())
    for tid, n in taxids.items():
        table[n] = by_tax.get(tid, [])
    for n in want:
        table.setdefault(n, [])
    path = _lineage_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for sp in sorted(table):
            fh.write(sp + "\t" + "|".join(table[sp]) + "\n")
    os.replace(tmp, path)
    return table


def relatedness(a_ens, b_ens):
    """How much lineage two species share, as the length of their common prefix.

    Sorghum and maize share their lineage down to Andropogoneae, sorghum and rice only to
    Poaceae, sorghum and Arabidopsis only to the flowering plants -- so the numbers order
    grasses ahead of eudicots without anything here knowing what a grass is. Zero when
    either lineage is unknown, so an unresolvable species sorts last rather than first.
    """
    if not a_ens or not b_ens or a_ens == b_ens:
        return 0
    tbl = ensure_lineages([a_ens, b_ens])
    a, b = tbl.get(a_ens), tbl.get(b_ens)
    if not a or not b:
        return 0
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def strip_species_prefix(name):
    """A symbol without its leading two-letter organism tag, or None if it has none.

    SbGHD7 -> GHD7, OsPRR37 -> PRR37, AtPIF4 -> PIF4. Narrow on purpose: the prefix must be
    an uppercase letter followed by a lowercase one, and what remains must still start with
    an uppercase letter or a digit and be at least two characters, so STH1 is left alone.

    Shared so that the probe and the annotation test agree about what a symbol is. They did
    not: the probe searched for GHD7 while the annotation test searched for sbghd7, so rice
    Ghd7 -- annotated "transcription factor GHD7-like" -- was scored as contradicting a node
    called SbGHD7.
    """
    m = re.match(r"^[A-Z][a-z]([A-Z0-9][A-Za-z0-9._-]+)$", (name or "").strip())
    return m.group(1) if m else None


_ALPHA_RUN = re.compile(r"[A-Za-z]+")


def _pattern_tokens(pat):
    """What letters a pattern requires, run by run, and how each is cased.

    Two kinds of token. A literal run -- `TraesCS`, `HORVU`, `Os` -- fixes both the letters
    and their case. A single-letter character class -- `[GT]` in maize's GRMZM\\d[GT]\\d{6},
    `[ABDU]` in wheat's subgenome field -- fixes only the case, because which letter appears
    is a property of the gene, not of the convention.

    Everything else (escapes, quantifiers, group openers, digits, underscores) ends the run
    it interrupts, which is what a letter run means in an identifier too.
    """
    toks, buf, i = [], "", 0
    def flush():
        nonlocal buf
        if buf:
            toks.append(("lit", buf))
            buf = ""
    while i < len(pat):
        c = pat[i]
        if c == "\\":
            flush(); i += 2; continue
        if c == "[":
            j = pat.find("]", i)
            if j < 0:
                return None
            flush()
            letters = [ch for ch in pat[i + 1:j] if ch.isalpha()]
            if letters:
                case = ("upper" if all(ch.isupper() for ch in letters)
                        else "lower" if all(ch.islower() for ch in letters) else "mixed")
                toks.append(("cls", case))
            i = j + 1; continue
        if c == "{":
            j = pat.find("}", i)
            i = j + 1 if j >= 0 else i + 1; continue
        if c == "(":
            flush()
            m = re.match(r"\(\?[a-zA-Z]*:?", pat[i:])
            i += m.end() if m else 1; continue
        if c.isalpha():
            buf += c; i += 1; continue
        flush(); i += 1
    flush()
    return toks


def _alternatives(pat):
    """Top-level alternatives of a derived pattern, outermost group unwrapped.

    The end anchor comes off first: without that, `(?:A|B)$` unwraps to nothing and splits
    into two fragments that are not valid regexes on their own.
    """
    body = pat.strip()
    if body.endswith("$"):
        body = body[:-1]
    m = re.fullmatch(r"\(\?:(.*)\)", body, re.S)
    if m:
        body = m.group(1)
    if "|" not in body:
        return [body]
    parts = body.split("|")
    for part in parts:
        try:
            re.compile(part)
        except re.error:
            return [body]        # a bar inside a group; not ours to split
    return parts


def canonical_case(gene_id, ens_name):
    """A gene identifier respelled the way its own species writes it.

    Papers print AGI codes as `At4g31800` at least as often as `AT4G31800`, and Ensembl's
    REST endpoints are case-sensitive: projecting the lower-case spelling returns zero
    orthologs rather than an error, so a correct identifier is silently discarded.

    The casing is taken from the species' own identifier pattern, which is derived from that
    species' gene list -- not from a table of species. `AT\\d+G\\d+` says the letters are
    upper case, `Os\\d+g\\d+` and `Solyc\\d+g\\d+` say the second run is lower, and
    `HORVU\\.MOREX\\.r\\d+` says all three. Whatever a user's species turns out to be, the
    convention comes from its annotation rather than from anything hard-coded here.

    This can only change letter case. Runs that do not match the pattern's letters
    case-insensitively are left exactly as they are, and an identifier whose shape does not
    line up with the pattern is returned untouched -- so a mis-recognised identifier stays
    wrong rather than being quietly rewritten into something else.
    """
    gid = (gene_id or "").strip()
    if not gid or not ens_name:
        return gid

    pats = [p for _label, p in ALT_ID_SYSTEMS.get(ens_name, [])]
    pats += REFERENCE_EXTRA.get("*", []) + REFERENCE_EXTRA.get(ens_name, [])
    try:
        ref, _cov = _resolver().derived_pattern(ens_name)
    except Exception:
        ref = None
    if ref:
        pats.append(ref)

    for pat in pats:
        for alt in _alternatives(pat):
            body = alt.rstrip("$")
            try:
                if not re.match(body + "$", gid, re.I):
                    continue
            except re.error:
                continue
            toks = _pattern_tokens(body)
            runs = _ALPHA_RUN.findall(gid)
            if toks is None or len(runs) != len(toks):
                continue
            fixed, ok = [], True
            for run, (kind, val) in zip(runs, toks):
                if kind == "lit":
                    if run.lower() != val.lower():
                        ok = False
                        break
                    fixed.append(val)
                elif val == "upper":
                    fixed.append(run.upper())
                elif val == "lower":
                    fixed.append(run.lower())
                else:
                    fixed.append(run)
            if not ok:
                continue
            it = iter(fixed)
            out = _ALPHA_RUN.sub(lambda m: next(it), gid)
            # Never anything but a case change.
            return out if out.lower() == gid.lower() else gid
    return gid


def classify_id_system(gene_id, ens_name):
    """Which identifier system a gene identifier belongs to, for a given species.

    Returns 'ensembl' when it matches that species' current reference pattern, the name of
    a known coexisting system when it matches one of those, and 'unrecognised' otherwise.
    'unrecognised' is a useful answer: it means nothing vouches for the identifier's shape.
    """
    gid = (gene_id or "").strip()
    if not gid or not ens_name:
        return "unrecognised"
    for label, pat in ALT_ID_SYSTEMS.get(ens_name, []):
        if re.match(pat, gid, re.I):
            return label
    for pat in REFERENCE_EXTRA.get("*", []) + REFERENCE_EXTRA.get(ens_name, []):
        if re.match(pat, gid, re.I):
            return "ensembl"
    try:
        ref, _cov = _resolver().derived_pattern(ens_name)
    except Exception:
        ref = None
    # Case-insensitive: "At4g37790" is how a great many papers spell AT4G37790, and it is
    # the same identifier, not an unknown one.
    if ref and re.match(ref + "$", gid, re.I):
        return "ensembl"
    return "unrecognised"

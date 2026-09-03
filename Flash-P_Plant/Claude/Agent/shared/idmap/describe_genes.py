#!/usr/bin/env python3
"""Functional descriptions for gene identifiers, and a family-level shortlist search.

Two modes, serving the same purpose from opposite ends.

  --annotate   descriptions for identifiers you already have. This is the cheap and
               important one: attaching a description to each ortholog candidate is what
               lets a judgement be made between them. Three sorghum candidates for PIF4
               all sit in PANTHER family PTHR45855, but only one is annotated
               "PHYTOCHROME INTERACTING FACTOR-LIKE 13", and that is decisive in a way
               sequence identity alone is not.

  --search     identifiers whose description matches a set of functional terms, for names
               that produced no candidates any other way.

The second mode must be read carefully. These descriptions are PANTHER family assignments,
so they identify a family and never a gene: searching sorghum for "MATE efflux" returns
seven transporters and cannot tell you which one is SbMATE. A search result is a shortlist,
and it only becomes an answer when it agrees with an independent route.

Description sources, in order: the local annotation layer built from Phytozome, then
Ensembl REST. Ensembl carries no description at all for several major crops -- sorghum
included -- which is why the local layer exists.
"""
import argparse
import collections
import gzip
import json
import math
import os
import re
import subprocess
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

STOP = {"the", "and", "for", "with", "protein", "gene", "family", "domain", "containing",
        "putative", "probable", "like", "related", "type", "subunit", "isoform", "factor"}


def _layer_paths(ens_name):
    """Every description layer available for this species, with what produced it.

    Both are kept rather than the first one winning. They are genuinely independent
    annotations -- Phytozome assigns a PANTHER family, NCBI curates a gene-level name --
    and where they agree that is worth more than either alone. Where they disagree that is
    worth knowing: sorghum SORBI_3002G199200 is "NAC domain-containing protein 100" to
    Phytozome and "NAC domain-containing protein 92" to NCBI, and the ORE1 node turns on
    which is right. A route that quietly picked one source would have hidden that.
    """
    return [
        (common.resolver_read("phytozome", f"{ens_name}.tsv.gz"), "panther"),
        (common.cache_path("descriptions", f"{ens_name}.tsv.gz"), "ncbi"),
    ]


_LAYERS = {}


def load_layer(ens_name, build_if_missing=False):
    """{gene_id: description} for a species, or {} if no layer is available.

    Memoised by species: this table has tens of thousands of rows and is consulted once
    per node.
    """
    if ens_name in _LAYERS:
        return _LAYERS[ens_name]
    _LAYERS[ens_name] = _load_layer_uncached(ens_name, build_if_missing)
    return _LAYERS[ens_name]


def _read_one(path):
    out = {}
    with gzip.open(path, "rt") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        try:
            gi, di = header.index("gene_id_canon"), header.index("description")
        except ValueError:
            gi, di = 0, len(header) - 1
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) > max(gi, di) and f[di]:
                out[f[gi]] = f[di]
    return out


def _load_layer_uncached(ens_name, build_if_missing=False):
    """{gene_id: {"description": text, "sources": {src: text}}} across every layer present."""
    merged = {}
    for path, src in _layer_paths(ens_name):
        if not os.path.exists(path):
            continue
        for gid, desc in _read_one(path).items():
            rec = merged.setdefault(gid, {"sources": {}})
            rec["sources"][src] = desc
    if not merged and build_if_missing:
        built = build_layer(ens_name)
        for gid, desc in (built or {}).items():
            merged[gid] = {"sources": {"panther": desc}}
    for rec in merged.values():
        # The searchable text is every source's wording, so a term that only one of them
        # uses still retrieves the gene. Curated gene-level naming leads.
        parts = [rec["sources"][s] for s in ("ncbi", "panther") if s in rec["sources"]]
        rec["description"] = " | ".join(parts)
    return merged


def build_layer(ens_name):
    """Fetch a description layer for a species we have not seen before.

    Delegates to the resolver library's builder, which joins Phytozome descriptions onto
    Ensembl identifiers and refuses when the two identifier systems do not correspond
    well enough to be joined safely.
    """
    script = common.resolver_script("build_phytozome_layer.py")
    print(f"building description layer for {ens_name} (first use; this is slow)...",
          file=sys.stderr)
    env = dict(os.environ)
    try:
        subprocess.run([sys.executable, script, "--species", ens_name],
                       check=True, env=env, timeout=1800)
    except subprocess.CalledProcessError as e:
        print(f"no description layer available for {ens_name}: {e}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"description layer build failed for {ens_name}: {e}", file=sys.stderr)
        return {}
    return load_layer(ens_name)


# Identifiers already looked up this run, including the ones Ensembl had nothing for. Two
# call sites ask one gene at a time from inside per-candidate loops, so without this the
# same miss is re-requested for every candidate that mentions it.
_ENS_CACHE = {}

# Consecutive batch failures before this run stops asking. Ensembl returning 500s is not a
# fact about any particular gene, and forty batches each waiting out a timeout turns a
# four-node run into six minutes. Two failures in a row is enough to conclude the service is
# unavailable; the run then continues on the offline layers, which is what they are for.
ENS_FAILURE_LIMIT = 2
_ens_failures = [0]

# Nodes are gathered concurrently and every one of them may ask for descriptions. The memo
# and the consecutive-failure counter are shared, so both move under one lock; the counter
# in particular is the thing that stops a run asking forty more times after Ensembl has
# started refusing, and it only works if the threads can see each other's failures.
_ENS_LOCK = threading.Lock()


# Twenty seconds, not sixty. A POST for forty identifiers answers in about two when the
# service is healthy, so a longer ceiling buys nothing and only makes an outage more
# expensive to discover.
def ensembl_descriptions(ids, chunk=40, timeout=20, quiet=False):
    """Descriptions from Ensembl REST. Empty for several major crops, sorghum included.

    Failures are reported rather than swallowed. They used to be silent, which made a
    service outage indistinguishable from a species Ensembl has no descriptions for: the
    REST endpoint returning 500 cost 60 seconds per batch and produced no message, so a
    four-node run took eight minutes with nothing in the log to say why.
    """
    out = {}
    failed = 0
    ids = [i for i in ids if i]
    with _ENS_LOCK:
        for i in ids:
            if i in _ENS_CACHE and _ENS_CACHE[i]:
                out[i] = _ENS_CACHE[i]
        ids = [i for i in ids if i not in _ENS_CACHE]
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        with _ENS_LOCK:
            spent = _ens_failures[0] >= ENS_FAILURE_LIMIT
        if spent:
            failed += len(batch)
            continue
        try:
            req = urllib.request.Request(
                "https://rest.ensembl.org/lookup/id",
                data=json.dumps({"ids": batch}).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": common.USER_AGENT})
            with common.open_url(req, timeout=timeout) as fh:
                payload = json.load(fh) or {}
            with _ENS_LOCK:
                _ens_failures[0] = 0
                for k in batch:
                    _ENS_CACHE[k] = ""
                for k, v in payload.items():
                    d = (v or {}).get("description")
                    if d:
                        out[k] = _ENS_CACHE[k] = d
        except Exception as exc:
            failed += len(batch)
            with _ENS_LOCK:
                _ens_failures[0] += 1
                hit_limit = _ens_failures[0] == ENS_FAILURE_LIMIT
            if hit_limit and not quiet:
                print(f"  Ensembl has failed {ENS_FAILURE_LIMIT} times in a row; no further "
                      f"description lookups will be attempted this run", file=sys.stderr)
            if not quiet:
                print(f"  Ensembl description lookup failed for {len(batch)} identifier(s): "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
    if failed and not quiet:
        print(f"  {failed} identifier(s) have no description because Ensembl could not be "
              f"reached; this is a service problem, not an answer about those genes",
              file=sys.stderr)
    return out


# The PANTHER layer prefixes its text with the family accession -- "(1 of 2) PTHR31744:SF104
# - NAC DOMAIN-CONTAINING PROTEIN 100" -- and those digits are not part of the description.
PANTHER_PREFIX = re.compile(r"^\s*(?:\(\d+ of \d+\)\s*)?(?:[A-Z0-9]+:?[A-Z0-9]*\s*-\s*)?")


# Accession noise that a merged description carries but that says nothing about the gene:
# the PANTHER family and subfamily numbers, and the "(1 of 2)" assignment count. Left in,
# their digits are read as member numbers -- "transcription factor PIF5 | (1 of 1)
# PTHR45855:SF17" then looks like a gene numbered 1, 45855 and 17 rather than 5.
ACCESSION_NOISE = re.compile(r"\(\d+ of \d+\)|\b[A-Z]{2,}\d+(?::[A-Z]{2,}\d+)?\b")


def _describe_only(text):
    """A description with accession identifiers and assignment counts removed, lowercased."""
    return ACCESSION_NOISE.sub(" ", PANTHER_PREFIX.sub("", text or "")).lower()


def member_conflict(symbol, description):
    """True when the annotation names a different member of the family than the node does.

    Descriptions identify families, so "auxin response factor 4" corroborates an ARF node
    on every word it contains -- and the one thing it actually settles is that this gene is
    not ARF2. The member number is the only part of a family annotation that discriminates,
    so when the node's symbol carries one and the description carries a different one, that
    is evidence against the candidate rather than for it.

    Silent when either side has no number: PIF5 against "transcription factor PIF5" agrees,
    and ARF2 against "auxin response factor" neither agrees nor conflicts.
    """
    sym = re.sub(r"^[A-Z][a-z]([A-Z])", r"\1", symbol or "")
    stem = re.match(r"^([A-Za-z]+)", sym)
    sym_n = re.findall(r"\d+", sym)
    if not sym_n or not stem:
        return False
    clean = _describe_only(description)
    # Digits glued to a word count: "PIF5" and "hexokinase-6" carry their member number
    # that way, and requiring a word boundary before the digit misses every one of them.
    desc_n = re.findall(r"\d+", clean)
    if not desc_n:
        return False

    # The numbers are only comparable when both sides are numbering the same family. ORE1
    # is ORESARA 1, and its annotation "NAC domain-containing protein 92" numbers the NAC
    # family -- 1 and 92 are counts of different things, and ANAC092 is in fact ORE1's own
    # alias. So the stem must name the family too: literally ("wrky" in "WRKY factor 58"),
    # or as the initials the symbol was abbreviated from (ARF <- auxin response factor).
    st = stem.group(1).lower()
    words = re.findall(r"[a-z]+", clean)
    if st in words:
        same_family = True
    else:
        initials = "".join(w[0] for w in words)
        same_family = st in initials
    if not same_family:
        return False
    return not (set(sym_n) & set(desc_n))


def _sources_agree(texts):
    """Whether two independent annotations of one gene actually say the same thing.

    Matching on shared words is not enough, and gets this exactly backwards: "NAC
    domain-containing protein 100" and "NAC domain-containing protein 83" share every word
    but the one that matters. Family membership is what these sources always agree on and
    is never what distinguishes a gene, so the member number decides -- when both sources
    give one and they differ, they disagree, whatever else they have in common.
    """
    clean = [PANTHER_PREFIX.sub("", t).lower() for t in texts]
    nums = [set(re.findall(r"\b\d+\b", c)) for c in clean]
    if all(nums) and not set.intersection(*nums):
        return False
    # Purely alphabetic, so "hexokinase-6" contributes "hexokinase" and its member number
    # is left to the numeric test above rather than welded onto the word.
    words = [set(re.findall(r"[a-z]{3,}", c)) - STOP for c in clean]
    return bool(set.intersection(*words))


def annotate(ids, ens_name, build_if_missing=False):
    layer = load_layer(ens_name, build_if_missing=build_if_missing)
    rows = []
    missing = [i for i in ids if i not in layer]
    ens = ensembl_descriptions(missing) if missing else {}
    for i in ids:
        rec = layer.get(i) or {}
        desc = rec.get("description") or ens.get(i) or ""
        srcs = rec.get("sources") or {}
        row = {
            "gene_id": i,
            "description": desc,
            "source": ("annotation_layer" if rec
                       else "ensembl" if ens.get(i) else "none"),
            "n_annotation_sources": len(srcs),
        }
        if srcs:
            row["by_source"] = srcs
            # Two independent annotations of the same gene that do not say the same thing.
            # This is not noise to smooth over: it is the annotation route telling you it
            # cannot settle the node on its own.
            if len(srcs) > 1:
                row["sources_agree"] = _sources_agree(list(srcs.values()))
        rows.append(row)
    return rows


_IDF = {}


def _term_weights(ens_name, layer, terms):
    """Weight each query term by how rare it is in this species' annotation.

    Weighting by term length instead -- which is the obvious thing to do and is wrong --
    makes "4-COUMARATE:CoA LIGASE" match on "ligase", which appears in hundreds of
    descriptions, and buries the handful of genes annotated "coumarate". Rarity is what
    carries the information: a term matching five genes says far more than one matching
    five hundred.
    """
    df = _IDF.get(ens_name)
    if df is None:
        df = collections.Counter()
        for rec in layer.values():
            for w in set(re.findall(r"[a-z][a-z0-9-]{2,}", rec["description"].lower())):
                df[w] += 1
        _IDF[ens_name] = df
    n = max(1, len(layer))
    out = {}
    for t in terms:
        # Terms absent from the annotation are given the weight of a very rare term: they
        # are informative when they do match, and cost nothing when they do not.
        out[t] = math.log(n / (1 + df.get(t, 0)))
    return out


def search(terms, ens_name, limit=40, build_if_missing=False):
    """Genes whose description matches the given functional terms, best match first.

    A shortlist, never an answer: these are protein-family assignments, so the result
    identifies a family. It becomes useful when it agrees with an independent route.
    """
    layer = load_layer(ens_name, build_if_missing=build_if_missing)
    if not layer:
        return []
    terms = [t.lower() for t in terms if len(t) >= 4 and t.lower() not in STOP]
    if not terms:
        return []
    weights = _term_weights(ens_name, layer, terms)
    total = sum(weights.values()) or 1.0

    # Whole-word matching. A substring test makes "mate" match inside "ferric hydroxamate
    # transporter", which is not a MATE transporter at all.
    matchers = [(t, re.compile(r"\b" + re.escape(t) + r"[a-z]{0,3}\b")) for t in terms]
    scored = []
    for gid, rec in layer.items():
        desc = rec["description"]
        d = desc.lower()
        hits = [t for t, rx in matchers if rx.search(d)]
        if not hits:
            continue
        score = sum(weights[t] for t in hits) / total
        scored.append({"gene_id": gid, "description": desc,
                       "matched_terms": hits, "score": round(score, 3)})
    scored.sort(key=lambda r: (-r["score"], r["gene_id"]))
    return scored[:limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--species", required=True)
    ap.add_argument("--annotate", help="comma-separated gene identifiers to describe")
    ap.add_argument("--search", help="comma-separated functional terms")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--build", action="store_true",
                    help="build the description layer if this species has none yet")
    args = ap.parse_args()

    info = common.classify_species(args.species)
    ens = info.get("ensembl_name")
    if not ens:
        print(json.dumps({"species": args.species, "status": info.get("status"),
                          "error": "no Ensembl Plants assembly; descriptions are only "
                                   "available for a proxy species"}))
        return

    if args.annotate:
        ids = [i.strip() for i in args.annotate.split(",") if i.strip()]
        rows = annotate(ids, ens, build_if_missing=args.build)
    elif args.search:
        terms = [t.strip() for t in re.split(r"[,\s]+", args.search) if t.strip()]
        rows = search(terms, ens, limit=args.limit, build_if_missing=args.build)
        if not rows:
            print(json.dumps({"species": ens, "results": [],
                              "note": "no description layer for this species, or no term matched. "
                                      "Run with --build to fetch one."}))
            return
    else:
        sys.exit("give --annotate or --search")

    print(json.dumps({"species": ens,
                      "shortlist_only": bool(args.search),
                      "results": rows}, indent=1))


if __name__ == "__main__":
    main()

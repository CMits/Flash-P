#!/usr/bin/env python3
"""Project a gene onto another species, with a confidence tier and independent corroboration.

Ortholog projection is the route that carries this whole tool: across the Flash-P corpus
43% of gene nodes are named after a gene in a different species, and for those the only
way to a target identifier is to resolve the name where it is known and project.

Aggregate projection accuracy of ~64% hides two very different populations. Compara's own
`type` label separates them: a top candidate labelled ortholog_one2one is right 95% of the
time, many2many only 37%. That label is therefore reported, never discarded.

Two further signals are gathered because the one2one label is not available often enough:

  reciprocal   project the candidate back and see whether the original gene returns. A
               reciprocal best hit is a much stronger statement than a one-way many2many.
  gramene      Gramene runs its own compara build on a different Ensembl release, so
               agreement between it and Ensembl is independent corroboration rather than
               the same computation asked twice.
  plaza        PLAZA infers orthology from gene family trees, best-hit families and
               genome collinearity -- a genuinely different method from Compara's gene
               trees, and the strongest corroboration available. It also grades its own
               belief across four evidence types instead of one label.

Results are cached to disk, so a network re-run costs nothing and the whole corpus can be
projected once.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import plaza_orthologs as plaza  # noqa: E402

ENSEMBL = "https://rest.ensembl.org"
GRAMENE = "https://data.gramene.org/v69/genes"

# Measured precision of the top candidate, by the label Compara puts on it. From a
# 200-gene rice benchmark with a non-circular up-leg.
# PLAZA's four evidence types are not four independent opinions. TROG (tree-based
# orthologous groups), BHIF (best-hits-and-inparalogs) and ORTHO (orthologous gene family)
# all read sequence similarity; anchor_point reads genome collinearity. So the three
# sequence types are counted once as a group, and synteny is scored on its own.
SIM_EVIDENCE = ("TROG", "BHIF", "ORTHO")
SIM_STEP = 0.04          # per sequence-similarity evidence type
SYNTENY_BONUS = 0.16     # collinearity anchor: independent of sequence


def _sim_support(evidence):
    return sum(1 for e in evidence if e in SIM_EVIDENCE)


TIER = {
    "ortholog_one2one":   ("high",   0.95),
    "ortholog_one2many":  ("medium", 0.58),
    "ortholog_many2many": ("low",    0.37),
}

def _get(url, params=None, tries=4, timeout=90):
    """A JSON GET. None means the server answered with nothing, or could not be reached.

    Pacing and 429 handling live in common.open_url, which holds one budget per host that
    every thread draws from. A single-process pacer was enough while this ran one node at
    a time; it is not once several nodes are in flight, and it could not express the fact
    that NCBI's limit is five times tighter than Ensembl's.
    """
    q = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(q, headers={"User-Agent": common.USER_AGENT,
                                             "Accept": "application/json"})
    try:
        with common.open_url(req, timeout=timeout, tries=tries) as r:
            return json.load(r)
    except Exception as e:
        if getattr(e, "code", None) in (400, 404):
            return None
        return None


def canon(i):
    """Strip transcript versions and gene: prefixes so identifiers compare cleanly."""
    return re.sub(r"\.\d+$", "", re.sub(r"^gene[-:]", "", str(i).strip()))


_CACHE = {}
_CACHE_DIRTY = set()

# Nodes are gathered concurrently, and they share this memo. Its contents do not depend on
# the order they arrive in -- every entry is keyed by gene and species pair -- so a lock
# around the read-modify-write is the whole of what concurrency costs here.
_CACHE_LOCK = threading.RLock()


def _cache_load(src, tgt):
    key = (src, tgt)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = common.load_ortholog_cache(src, tgt)
        return _CACHE[key]


def flush_cache():
    """Write the memo back, merged with whatever is on disk now.

    Merged rather than overwritten because this process is no longer the only writer: the
    bulk prefetch fills the same files from the Compara dumps in the background, and a
    plain overwrite at the end of a run would throw away a table of twenty thousand genes
    in favour of the seventy this run happened to ask about. On-disk entries win for genes
    this run never touched; this run's entries win for the genes it did, so a fresh answer
    is never replaced by a stale one.
    """
    with _CACHE_LOCK:
        for (src, tgt) in list(_CACHE_DIRTY):
            merged = common.load_ortholog_cache(src, tgt)
            merged.update(_CACHE[(src, tgt)])
            _CACHE[(src, tgt)] = merged
            common.save_ortholog_cache(src, tgt, merged)
        _CACHE_DIRTY.clear()


# ---------------------------------------------------------------------------
# Bulk fill of a species pair
#
# The first network on a new species pair pays one REST call per gene, and that is what
# makes a first run slow: 47s a node against 8.4s once the pair is cached. The whole
# pairwise table is a downloadable file, so the first miss starts fetching it in the
# background while this run carries on over REST. Nothing waits for it -- the download is
# minutes and putting it in the startup path would make the first run slower, not faster.
# It is the run after this one that gets the benefit, and every run after that.
#
# Set FLASHP_IDMAP_NO_PREFETCH=1 to keep a run purely on REST.
# ---------------------------------------------------------------------------
_PREFETCH_STARTED = set()


def _prefetch_marker(src, tgt):
    return common.cache_path("orthologs",
                             f"{common.ortholog_pair_key(src, tgt)}.bulk.json")


def prefetch_pair_async(src, tgt):
    """Start filling this species pair from the Compara dump. Returns immediately."""
    if os.environ.get("FLASHP_IDMAP_NO_PREFETCH") or src == tgt:
        return
    # Keyed on the unordered pair. compara() is called in both directions -- once to
    # project and again for the reciprocal check -- so an ordered key let the same dumps be
    # fetched twice, the second time adding nothing.
    key = common.ortholog_pair_key(src, tgt)
    with _CACHE_LOCK:
        if key in _PREFETCH_STARTED:
            return
        _PREFETCH_STARTED.add(key)
    # Already fetched, or already found to be absent from both dumps.
    if os.path.exists(_prefetch_marker(src, tgt)):
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prefetch_compara.py")
    if not os.path.exists(script):
        return
    try:
        log = open(common.cache_path("orthologs", f"{key}.prefetch.log"), "ab")
        subprocess.Popen([sys.executable, script, "--src", src, "--tgt", tgt],
                         stdout=log, stderr=log, start_new_session=True)
    except Exception:
        # A prefetch that cannot start is a slow run, not a wrong one.
        pass


def compara(gene, src, tgt):
    """Forward projection from Ensembl Compara, keeping the homology type."""
    store = _cache_load(src, tgt)
    with _CACHE_LOCK:
        if gene in store:
            return store[gene]
    # Nothing local for this pair yet: fill it from the Compara dump in the background,
    # for this run's later genes and for every run after it. This call goes out regardless.
    prefetch_pair_async(src, tgt)
    d = _get(f"{ENSEMBL}/homology/id/{src}/{gene}",
             {"target_species": tgt, "type": "orthologues",
              "content-type": "application/json"})
    out = []
    if d and d.get("data"):
        for h in d["data"][0].get("homologies", []):
            t = h.get("target", {}) or {}
            if not t.get("id"):
                continue
            out.append({
                "gene_id": canon(t["id"]),
                "type": h.get("type", ""),
                "perc_id": t.get("perc_id"),
                "perc_pos": t.get("perc_pos"),
            })
    out.sort(key=lambda c: -(c.get("perc_id") or 0))
    with _CACHE_LOCK:
        store[gene] = out
        _CACHE_DIRTY.add((src, tgt))
    return out


def gramene_orthologs(gene, tgt_pattern):
    """Independent ortholog set from Gramene's own compara build."""
    store = _cache_load("gramene", "any")
    with _CACHE_LOCK:
        rows = store.get(gene)
    if rows is None:
        d = _get(GRAMENE, {"idList": gene, "fl": "homology"})
        rows = []
        if isinstance(d, list) and d:
            hg = (d[0].get("homology") or {}).get("homologous_genes") or {}
            for rel, ids in hg.items():
                if not rel.startswith("ortholog"):
                    continue
                for i in ids or []:
                    rows.append({"gene_id": canon(i), "type": rel})
        with _CACHE_LOCK:
            store[gene] = rows
            _CACHE_DIRTY.add(("gramene", "any"))
    if not tgt_pattern:
        return []
    rx = re.compile(tgt_pattern)
    return [r for r in rows if rx.fullmatch(r["gene_id"]) or rx.match(r["gene_id"])]


def target_pattern(tgt):
    sr = common._resolver()
    try:
        pat, cov = sr.derived_pattern(tgt)
        return pat if cov >= 0.5 else None
    except Exception:
        return None


# Where a target genome carries several subgenomes, a gene's counterpart is a set, not a
# gene, and collinearity is what identifies the set. Measured on the rice-wheat PLAZA
# table: of the 14,819 rice genes with syntenic wheat candidates, 59% have exactly three,
# and 80% of the syntenic sets span all three subgenomes. Reading those three as three
# competing answers is simply wrong -- they are the homoeolog triad.
#
# The subgenome letter is positional in the identifier (TraesCS3A02G077900 -> A), so it is
# read from the pattern rather than looked up. Species not listed here have one subgenome
# and take the ordinary path.
# Each pattern captures the chromosome group first and the subgenome letter second, because
# both are needed: homoeologs sit on *homoeologous chromosomes*, so 1A, 1B and 1D are a
# triad while 3A, 1B and 1D are two different loci wearing one label.
SUBGENOME_PATTERNS = {
    "triticum_aestivum": (re.compile(r"^TraesCS(\d+)([ABD])\d+G\d+$"), ("A", "B", "D")),
    "triticum_dicoccoides": (re.compile(r"^TRIDC(\d+)([AB])\d+G\d+$"), ("A", "B")),
}


def subgenome_of(gene_id, tgt):
    """(chromosome group, subgenome) for an identifier, or None for a diploid target."""
    entry = SUBGENOME_PATTERNS.get(tgt)
    if not entry:
        return None
    m = entry[0].match(gene_id or "")
    return (m.group(1), m.group(2)) if m else None


def homoeolog_set(cands, tgt):
    """The syntenic candidates that together form a homoeolog set, or [] if they do not.

    Requires collinearity, because sequence similarity cannot tell a homoeolog from any
    other close paralogue, and requires the members to sit in *different* subgenomes --
    three syntenic hits in the A subgenome are a tandem array, which is a different thing
    and must not be reported as a triad.
    """
    if tgt not in SUBGENOME_PATTERNS:
        return []
    syn = [c for c in cands if "syntenic" in c.get("support", [])]

    # Group by chromosome first. Wheat GAMYB projected from rice returned syntenic hits on
    # 3A, 1B and 1D; taking one per subgenome made a "complete ABD triad" out of two
    # unrelated loci. Homoeologs share the chromosome group by definition.
    groups = {}
    for c in syn:
        loc = subgenome_of(c["gene_id"], tgt)
        if not loc:
            continue
        grp, sub = loc
        groups.setdefault(grp, {}).setdefault(sub, c)

    if not groups:
        return []
    # The best-supported chromosome group: most subgenomes represented, then highest score.
    best = max(groups.values(),
               key=lambda g: (len(g), max(c["score"] for c in g.values())))
    return [best[k] for k in sorted(best)] if len(best) > 1 else []


def project(gene, src, tgt, reciprocal=True, use_gramene=True, use_plaza=True,
            max_reciprocal=2):
    # Case matters to the REST endpoints and not to the paper that printed the identifier.
    # Normalised here as well as at mining time, because project() is called directly by
    # the adjudication step with whatever spelling the mapper passed on the command line.
    gene = common.canonical_case(canon(gene), src)
    fwd = compara(gene, src, tgt)

    gram = []
    if use_gramene:
        try:
            gram = gramene_orthologs(gene, target_pattern(tgt))
        except Exception:
            gram = []
    gram_ids = {g["gene_id"] for g in gram}

    # PLAZA, from its bulk download. The first call for a species pair streams and distils
    # one large file; every call after that reads a cached table of a megabyte or so.
    plz = []
    plz_id_system = "ensembl"
    if use_plaza:
        try:
            plz, _meta = plaza.orthologs(gene, src, tgt)
            plz_id_system = (_meta or {}).get("tgt_id_system", "ensembl")
        except Exception:
            plz = []
    plz_support = {p["gene_id"]: p for p in plz}

    # Reciprocal check on the leading candidates only: it costs one call each, and a
    # candidate that never places in the top few is not going to be the answer.
    recip = {}
    if reciprocal:
        for c in fwd[:max_reciprocal]:
            back = compara(c["gene_id"], tgt, src)
            ids = [b["gene_id"] for b in back]
            recip[c["gene_id"]] = {
                "returns_source": gene in ids,
                "is_best": bool(ids) and ids[0] == gene,
                "n_back": len(ids),
            }

    # Sequence identity separates candidates that the homology label cannot. Among a set of
    # many2many orthologs one candidate is often far closer than the rest, and that is
    # informative -- but only when the margin is real, so the bonus requires clear
    # separation from the runner-up rather than merely being first.
    ids_pc = sorted((c.get("perc_id") or 0) for c in fwd)
    best_pc = ids_pc[-1] if ids_pc else 0
    second_pc = ids_pc[-2] if len(ids_pc) > 1 else 0
    dominant_pc = best_pc if (best_pc >= 25 and best_pc >= 1.3 * second_pc) else None

    cands = []
    for c in fwd:
        label, base = TIER.get(c["type"], ("low", 0.30))
        r = recip.get(c["gene_id"], {})
        support = []
        score = base
        if dominant_pc and (c.get("perc_id") or 0) == dominant_pc:
            support.append("dominant_identity")
            score = min(0.95, score + 0.12)
        if r.get("is_best"):
            support.append("reciprocal_best")
            score = min(0.97, score + 0.22)
        elif r.get("returns_source"):
            support.append("reciprocal_hit")
            score = min(0.95, score + 0.10)
        if c["gene_id"] in gram_ids:
            support.append("gramene_agrees")
            score = min(0.97, score + 0.12)
        pz = plz_support.get(c["gene_id"])
        if pz:
            # PLAZA grades its own belief, but not all four of its evidence types are worth
            # the same. TROG, BHIF and ORTHO are three readings of sequence similarity --
            # ORTHO alone accounts for 166,655 of the Arabidopsis-sorghum pairs -- so a
            # candidate carrying all three is corroborated by one kind of evidence three
            # times. anchor_point is collinearity: independent of sequence, and unaffected
            # by the saturation that makes deep comparisons unreliable.
            #
            # Measured on the Arabidopsis-sorghum table: of the 16,038 source genes with
            # more than one candidate, synteny is available for 9% -- and where it is
            # available it takes the median candidate set from 5 to 1, resolving 1,119 of
            # 1,394 outright. Scoring it level with a family assignment wasted that.
            support.append(f"plaza_agrees({'+'.join(pz['evidence']) or 'weak'})")
            score = min(0.97, score + 0.06 + SIM_STEP * _sim_support(pz["evidence"]))
            if "anchor_point" in pz["evidence"]:
                support.append("syntenic")
                score = min(0.97, score + SYNTENY_BONUS)
        cands.append({**c, "support": support, "score": round(score, 3), "tier": label})

    # Candidates PLAZA found that Compara did not. These matter: Compara's gene trees miss
    # pairs that PLAZA's collinearity evidence recovers, and a pair with three or four
    # evidence types behind it deserves to be seen even with no Ensembl support.
    known = {c["gene_id"] for c in cands}
    for p in plz:
        if p["gene_id"] in known:
            continue
        syn = "anchor_point" in p["evidence"]
        base = 0.20 + 0.07 * _sim_support(p["evidence"]) + (SYNTENY_BONUS if syn else 0.0)
        sup = [f"plaza_only({'+'.join(p['evidence']) or 'weak'})"]
        if syn:
            sup.append("syntenic")
        cands.append({"gene_id": p["gene_id"], "type": "plaza_ortholog", "perc_id": None,
                      "perc_pos": None,
                      "support": sup,
                      "score": round(base, 3),
                      "id_system": plz_id_system,
                      # A collinearity anchor Compara missed is a real finding, not a
                      # leftover: synteny is how a pair survives sequence divergence.
                      "tier": "medium" if (syn or p["n_support"] >= 3) else "low"})

    # Gramene-only candidates are kept and clearly marked. Gramene publishes its own
    # homology type, so a one2one it found alone still deserves more weight than a
    # many2many -- but everything here is discounted for having no Ensembl support.
    for g in gram:
        if g["gene_id"] not in {c["gene_id"] for c in cands}:
            g_label, g_base = TIER.get(g["type"], ("low", 0.30))
            cands.append({"gene_id": g["gene_id"], "type": g["type"], "perc_id": None,
                          "perc_pos": None, "support": ["gramene_only"],
                          "score": round(max(0.25, g_base - 0.33), 3),
                          "tier": "low" if g_base - 0.33 < 0.5 else "medium"})

    cands.sort(key=lambda c: (-c["score"], -(c.get("perc_id") or 0)))

    # A homoeolog set is one answer with several members, not several competing answers.
    # It is reported alongside the ranked candidates rather than replacing them, because
    # whether this node means the triad or one particular copy is the mapper's call.
    homoeologs = homoeolog_set(cands, tgt)
    for c in homoeologs:
        loc = subgenome_of(c["gene_id"], tgt)
        c["chromosome_group"], c["subgenome"] = loc if loc else ("", "")

    top = cands[0] if cands else None
    return {
        "gene": gene,
        "source_species": src,
        "target_species": tgt,
        "resolved": bool(cands),
        "n_candidates": len(cands),
        "candidates": cands[:12],
        "top": top,
        "relation": (top or {}).get("type", ""),
        "confidence": (top or {}).get("tier", "none"),
        "score": (top or {}).get("score", 0.0),
        "target_id_system": plz_id_system,
        "homoeolog_set": [{"gene_id": c["gene_id"], "subgenome": c["subgenome"],
                           "score": c["score"]} for c in homoeologs],
        "subgenomes_covered": "".join(c["subgenome"] for c in homoeologs),
        "subgenomes_expected": "".join(SUBGENOME_PATTERNS[tgt][1] or ())
                               if tgt in SUBGENOME_PATTERNS and SUBGENOME_PATTERNS[tgt][1] else "",
        "n_sources": len({s.split("(")[0] for c in cands for s in c["support"]
                          if s.startswith(("plaza", "gramene"))}) + (1 if fwd else 0),
        "note": "" if cands else "no orthologs returned by Ensembl Compara, Gramene or PLAZA",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gene", help="comma-separated gene identifiers")
    ap.add_argument("--genes-file", help="file with one gene identifier per line")
    ap.add_argument("--from", dest="src", required=True, help="source species")
    ap.add_argument("--to", dest="tgt", required=True, help="target species")
    ap.add_argument("--no-reciprocal", action="store_true")
    ap.add_argument("--no-gramene", action="store_true")
    ap.add_argument("--no-plaza", action="store_true",
                    help="skip PLAZA; avoids a large one-off download for a new species pair")
    args = ap.parse_args()

    genes = []
    if args.gene:
        genes += [g.strip() for g in args.gene.split(",") if g.strip()]
    if args.genes_file:
        with open(args.genes_file) as fh:
            genes += [ln.strip() for ln in fh if ln.strip()]
    if not genes:
        sys.exit("give --gene or --genes-file")

    src = common.classify_species(args.src).get("ensembl_name") or args.src
    tgt = common.classify_species(args.tgt).get("ensembl_name") or args.tgt

    try:
        for g in genes:
            print(json.dumps(project(g, src, tgt,
                                     reciprocal=not args.no_reciprocal,
                                     use_gramene=not args.no_gramene,
                                     use_plaza=not args.no_plaza)))
    finally:
        flush_cache()


if __name__ == "__main__":
    main()

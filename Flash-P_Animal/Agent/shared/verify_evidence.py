"""
Verify a FLASH-P network's provenance — every DOI resolved, every claim grounded.

Reads a network directory, checks each edge and each perturbation test against the
literature, and writes ``data/evidence.json``: the paper behind every claim, the exact
sentence that supports it, and where that sentence lives. Open-access full texts land
in ``data/fulltext/``.

    python Agent/shared/verify_evidence.py <NET>
    python Agent/shared/verify_evidence.py <NET> --apply        # write repaired DOIs back
    python Agent/shared/verify_evidence.py <NET> --offline      # cache only, no network

Three outcomes per claim, using Flash-P_DataBase's vocabulary so a verified network can
be contributed to the atlas unchanged:

    verified    the DOI on record resolves and the paper co-mentions both entities
    repaired    it did not, and a paper that does was found — old DOI kept on the record
    quarantine  nothing found within the budget; every attempt is recorded

Cost: HTTP only. No model calls, no API keys, no contact email. Papers are cached under
``.flashp_cache/`` so re-running a network is free.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import light_io                                        # noqa: E402
from provenance import (                               # noqa: E402
    QUARANTINE, REPAIRED, VERIFIED, Claim, Config, Store,
    aliases_for, bare_doi, doi_slug, resolve_claim,
)
from provenance import litapi                          # noqa: E402
from provenance.store import default_path              # noqa: E402

EVIDENCE_FILE = "evidence.json"
FULLTEXT_DIR = "fulltext"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _read(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        return light_io.load(path)
    except Exception as e:
        print(f"  ! could not read {os.path.basename(path)}: {e}", file=sys.stderr)
        return None


def build_alias_index(net: str, curated: Optional[Dict[str, Any]]) -> Dict[str, set]:
    """node id -> every name a paper might use for it.

    ``node_annotations.json`` is the good source because it carries ``fn`` (the full
    name, often with the abbreviation in parentheses). When the network has not been
    built yet — verification runs before BUILDER — fall back to the node ids in
    ``curated_edges.json``, which still gives underscore and species-prefix variants.

    Keyed by upper case on purpose: the BUILDER rewrites ``ZmEPF2`` as ``ZMEPF2``, so
    an exact-match index attaches the full names to none of the gene nodes and every
    gene claim loses its aliases. Aliases are still generated from the *claim's*
    spelling, which keeps the species prefix strippable.
    """
    index: Dict[str, set] = {}
    ann = _read(os.path.join(net, "network", "node_annotations.json"))
    for a in (ann or {}).get("annotations", []):
        node = a.get("node") or a.get("n") or ""
        fn = a.get("full_name") or a.get("fn") or ""
        if node:
            index.setdefault(node.upper(), set()).update(aliases_for(node, fn))
    for node in ((curated or {}).get("nodes") or {}):
        index.setdefault(node.upper(), set()).update(aliases_for(node))
    return index


def _alias(index: Dict[str, set], name: str) -> set:
    """Aliases for a node: whatever the index knows, plus the claim's own spelling."""
    return (index.get((name or "").upper()) or set()) | aliases_for(name)


def collect_claims(net: str) -> Tuple[List[dict], List[dict], Dict[str, Any]]:
    """Edge and perturbation claims for a network, plus its metadata."""
    curated = _read(os.path.join(net, "data", "curated_edges.json"))
    perts = _read(os.path.join(net, "data", "perturbation_dataset.json"))
    if curated is None and perts is None:
        raise SystemExit(f"no curated_edges.json or perturbation_dataset.json under {net}/data")

    meta = (curated or perts or {}).get("metadata", {}) or {}
    phenotype = meta.get("phenotype", "") or ""
    species = meta.get("species", "") or ""
    index = build_alias_index(net, curated)

    # The phenotype node is the one typed P; fall back to the metadata string.
    pheno_node = next((n for n, t in ((curated or {}).get("nodes") or {}).items()
                       if str(t).upper() in ("P", "PHENOTYPE")), "")
    pheno_name = pheno_node or phenotype.replace(" ", "_")

    edges = []
    for e in (curated or {}).get("edges", []):
        s, t = e.get("source") or e.get("s"), e.get("target") or e.get("t")
        if not s or not t:
            continue
        edges.append({
            "eid": e.get("edge_id") or e.get("eid") or "",
            "s": s, "t": t, "x": int(e.get("sign", e.get("x", 0)) or 0),
            "doi": bare_doi(e.get("doi") or e.get("d") or ""),
            "claim": Claim(entity_a=s, entity_b=t,
                           sign=int(e.get("sign", e.get("x", 0)) or 0),
                           species=species,
                           aliases_a=_alias(index, s), aliases_b=_alias(index, t),
                           label=f"{s} {'->' if int(e.get('sign', e.get('x', 0)) or 0) >= 0 else '-|'} {t}"),
        })

    tests = []
    for p in (perts or {}).get("perturbations", []):
        gene = p.get("gene") or p.get("g") or ""
        if not gene:
            continue
        ed = str(p.get("expected_direction") or p.get("ed") or "")
        # The paper reports what was *observed* in the experiment, so the cue we
        # expect in its sentence is the observed direction, not the gene's sign.
        obs = 1 if ed in ("up", "increased") else (-1 if ed in ("dn", "decreased") else 0)
        sp = p.get("species") or p.get("sp") or species
        tests.append({
            "id": p.get("test_id") or p.get("id") or "",
            "g": gene,
            "pt": p.get("perturbation_type") or p.get("pt") or "",
            "ed": ed,
            "sp": sp,
            "doi": bare_doi(p.get("doi") or p.get("d") or ""),
            "claim": Claim(entity_a=gene, entity_b=pheno_name, sign=obs, species=sp,
                           aliases_a=_alias(index, gene),
                           aliases_b=_alias(index, pheno_name) | aliases_for(pheno_name, phenotype),
                           label=f"{gene} {p.get('perturbation_type') or p.get('pt') or ''} -> {ed}"),
        })

    return edges, tests, meta


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify_records(records: List[dict], store: Store, cfg: Config,
                   kind: str, quiet: bool = False) -> List[dict]:
    """Resolve every record, reporting progress as it goes."""
    out = []
    total = len(records)
    for i, rec in enumerate(records, 1):
        claim: Claim = rec["claim"]
        res = resolve_claim(claim, rec["doi"], store, cfg)

        row = {k: v for k, v in rec.items() if k != "claim"}
        row.update({
            "doi": res.doi,
            "evidence": res.support.quote if res.support else "",
            "source_locator": res.support.locator if res.support else "",
            "confidence": res.support.confidence if res.support else 0.0,
            "verification": res.status,
            "verification_reason": res.reason,
            "previous_doi": res.previous_doi,
            "tried": [a.as_dict() for a in res.tried],
        })
        out.append(row)

        if not quiet:
            mark = {VERIFIED: "ok ", REPAIRED: "fix", QUARANTINE: "QRN"}[res.status]
            extra = f" -> {res.doi}" if res.status == REPAIRED else ""
            print(f"  [{i:>3}/{total}] {mark} {kind} {claim.label[:48]:<48}{extra}")
    return out


def write_fulltexts(net: str, dois: List[str], store: Store) -> Dict[str, str]:
    """Write open-access full texts next to the evidence; return doi -> relative path.

    Kept as separate ``.txt`` files rather than inlined in the JSON: they are 30-60 KB
    each, and the ``## Section`` layout is the same one Flash-P_DataBase and the
    website already parse.
    """
    written: Dict[str, str] = {}
    out_dir = os.path.join(net, "data", FULLTEXT_DIR)
    for doi in dois:
        text = store.get_fulltext(doi)
        if not text:
            continue
        os.makedirs(out_dir, exist_ok=True)
        name = f"{doi_slug(doi)}.txt"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
        written[doi] = f"{FULLTEXT_DIR}/{name}"
    return written


def build_papers(dois: List[str], store: Store, fulltexts: Dict[str, str]) -> Dict[str, dict]:
    papers: Dict[str, dict] = {}
    for doi in sorted(set(d for d in dois if d)):
        rec = store.get(doi)
        if rec is None:
            continue
        papers[doi] = {
            "title": rec.get("title", ""),
            "authors": rec.get("authors", ""),
            "year": rec.get("year"),
            "journal": rec.get("journal", ""),
            "abstract": rec.get("abstract", ""),
            "oa_status": rec.get("oa_status", ""),
            "has_fulltext": doi in fulltexts,
            "fulltext_file": fulltexts.get(doi, ""),
        }
    return papers


def apply_repairs(net: str, edges: List[dict], tests: List[dict]) -> int:
    """Write repaired DOIs back into the Step 1 files, preserving the Light shape.

    Only DOIs change. Nothing is added, removed or reordered, so the BUILDER sees the
    same repository it would have seen — with references that now point at the papers
    the claims actually came from.
    """
    changed = 0

    cur_path = os.path.join(net, "data", "curated_edges.json")
    if os.path.isfile(cur_path):
        data = light_io.load(cur_path)
        fixed = {(r["s"], r["t"], r["x"]): r["doi"]
                 for r in edges if r["verification"] == REPAIRED and r["doi"]}
        for e in data.get("edges", []):
            key = (e.get("source"), e.get("target"), int(e.get("sign", 0) or 0))
            if key in fixed and e.get("doi") != fixed[key]:
                e["doi"] = fixed[key]
                e["evidence"] = [{"doi": fixed[key]}]
                changed += 1
        if fixed:
            light_io.dump_slim(cur_path, data, "curated_edges")

    pert_path = os.path.join(net, "data", "perturbation_dataset.json")
    if os.path.isfile(pert_path):
        data = light_io.load(pert_path)
        fixed = {r["id"]: r["doi"] for r in tests
                 if r["verification"] == REPAIRED and r["doi"]}
        for p in data.get("perturbations", []):
            tid = p.get("test_id")
            if tid in fixed and p.get("doi") != fixed[tid]:
                p["doi"] = fixed[tid]
                p["evidence"] = [{"doi": fixed[tid]}]
                changed += 1
        if fixed:
            light_io.dump_slim(pert_path, data, "perturbation_dataset")

    return changed


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def summarise(rows: List[dict]) -> Dict[str, int]:
    c = {VERIFIED: 0, REPAIRED: 0, QUARANTINE: 0}
    for r in rows:
        c[r["verification"]] = c.get(r["verification"], 0) + 1
    return c


def print_report(name: str, edges: List[dict], tests: List[dict],
                 papers: Dict[str, dict], elapsed: float) -> None:
    e, t = summarise(edges), summarise(tests)
    n_e, n_t = len(edges), len(tests)
    oa = sum(1 for p in papers.values() if p["has_fulltext"])
    abs_ok = sum(1 for p in papers.values() if p["abstract"])

    print(f"\n{'=' * 68}\n{name}\n{'=' * 68}")
    print(f"{'':14}{'verified':>10}{'repaired':>10}{'quarantine':>12}{'total':>8}")
    print(f"{'edges':14}{e[VERIFIED]:>10}{e[REPAIRED]:>10}{e[QUARANTINE]:>12}{n_e:>8}")
    print(f"{'perturbations':14}{t[VERIFIED]:>10}{t[REPAIRED]:>10}{t[QUARANTINE]:>12}{n_t:>8}")
    total = n_e + n_t
    good = e[VERIFIED] + e[REPAIRED] + t[VERIFIED] + t[REPAIRED]
    if total:
        print(f"\ngrounded: {good}/{total} ({100.0 * good / total:.0f}%)   "
              f"papers: {len(papers)} ({abs_ok} with abstract, {oa} with open-access full text)")
    print(f"time: {elapsed:.0f}s   {litapi.STATS}")

    bad = [r for r in edges + tests if r["verification"] == QUARANTINE]
    if bad:
        print(f"\nquarantined ({len(bad)}) — claim could not be grounded in any paper found:")
        for r in bad[:12]:
            label = (f"{r['s']} -> {r['t']}" if "s" in r
                     else f"{r['g']} {r['pt']} -> {r['ed']}")
            print(f"  - {label:<42} {r['doi'] or '(no DOI)'}")
        if len(bad) > 12:
            print(f"  … and {len(bad) - 12} more (see {EVIDENCE_FILE})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def verify_network(net: str, cfg: Config, apply: bool = False,
                   quiet: bool = False, cache: Optional[str] = None) -> dict:
    started = time.time()
    name = os.path.basename(os.path.abspath(net.rstrip("/\\")))
    edges, tests, meta = collect_claims(net)
    if not quiet:
        print(f"\n{name}: {len(edges)} edges, {len(tests)} perturbation tests")

    with Store(cache or default_path()) as store:
        e_rows = verify_records(edges, store, cfg, "edge", quiet)
        t_rows = verify_records(tests, store, cfg, "test", quiet)

        dois = [r["doi"] for r in e_rows + t_rows if r["doi"]]
        fulltexts = write_fulltexts(net, dois, store)
        papers = build_papers(dois, store, fulltexts)

    evidence = {
        "metadata": {
            "flash_p_version": meta.get("flash_p_version", ""),
            "phenotype": meta.get("phenotype", ""),
            "species": meta.get("species", ""),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sources": ["openalex", "europepmc"],
            "grounding": "strict verbatim substring of abstract or open-access full text",
        },
        "summary": {
            "edges": summarise(e_rows), "perturbations": summarise(t_rows),
            "papers": len(papers),
            "papers_with_fulltext": sum(1 for p in papers.values() if p["has_fulltext"]),
        },
        "papers": papers,
        "edges": e_rows,
        "perturbations": t_rows,
    }

    os.makedirs(os.path.join(net, "data"), exist_ok=True)
    out_path = os.path.join(net, "data", EVIDENCE_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)

    if apply:
        n = apply_repairs(net, e_rows, t_rows)
        if not quiet:
            print(f"\napplied {n} repaired DOI(s) back to data/")

    if not quiet:
        print_report(name, e_rows, t_rows, papers, time.time() - started)
        print(f"\nwrote {out_path}")
    return evidence


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify DOIs and ground evidence for a FLASH-P network.")
    ap.add_argument("network", nargs="+", help="network directory (containing data/)")
    ap.add_argument("--apply", action="store_true",
                    help="write repaired DOIs back into data/ (default: report only)")
    ap.add_argument("--offline", action="store_true",
                    help="use the cache only; make no network requests")
    ap.add_argument("--no-fulltext", action="store_true",
                    help="skip open-access full-text retrieval (abstracts only)")
    ap.add_argument("--max-rounds", type=int, default=3,
                    help="repair search rounds per claim (default 3)")
    ap.add_argument("--candidates", type=int, default=8,
                    help="candidate papers examined per round (default 8)")
    ap.add_argument("--cache", default=None, help="path to the paper cache DB")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args(argv)

    cfg = Config(max_rounds=args.max_rounds, candidates_per_round=args.candidates,
                 fulltext=not args.no_fulltext, offline=args.offline)

    worst = 0
    for net in args.network:
        if not os.path.isdir(net):
            print(f"not a directory: {net}", file=sys.stderr)
            worst = 2
            continue
        ev = verify_network(net, cfg, apply=args.apply, quiet=args.quiet, cache=args.cache)
        q = ev["summary"]["edges"].get(QUARANTINE, 0) + \
            ev["summary"]["perturbations"].get(QUARANTINE, 0)
        worst = max(worst, 1 if q else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main())

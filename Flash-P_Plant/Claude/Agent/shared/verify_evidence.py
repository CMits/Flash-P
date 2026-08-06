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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import light_io                                        # noqa: E402
from provenance import (                               # noqa: E402
    QUARANTINE, REPAIRED, VERIFIED, Claim, Config, Store,
    aliases_for, bare_doi, doi_slug, resolve_claim,
)
from provenance import litapi, species                 # noqa: E402
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
def prefetch_papers(records: List[dict], store: Store, cfg: Config,
                    quiet: bool = False) -> int:
    """Warm the cache with every DOI in the network, in a handful of batched calls.

    Both catalogues take a batch of DOIs per request, so a whole network's on-record
    DOIs cost a few calls instead of one or two per claim. Three things fall out of
    doing it once, up front:

      * claims that share a paper (73 distinct DOIs behind 122 claims in the salinity
        network) stop re-fetching it;
      * no two workers can race to fetch the same DOI, which the per-claim path could
        not prevent — the cache only helps once the first fetch has *returned*;
      * the per-claim ladder then usually starts from a cache hit.

    Only metadata and abstracts are prefetched. Full text stays lazy — it is the
    expensive call and most claims never need it.
    """
    if cfg.offline:
        return 0
    want = []
    for rec in records:
        d = bare_doi(rec.get("doi", ""))
        if d and d not in want:
            want.append(d)
    # Anything already cached with an abstract needs nothing; known-bad DOIs are skipped
    # so a batch is not spent re-asking about them.
    todo = [d for d in want
            if not (store.get(d) or {}).get("abstract") and not store.is_miss(d)]
    if not todo:
        return 0

    # Europe PMC only: it is free and takes 25 DOIs per call. OpenAlex's equivalent
    # (``filter=doi:A|B|C``) goes through its metered ``/works?`` endpoint, so bulk
    # lookups there buy a daily-budget 429 instead of speed. Whatever Europe PMC cannot
    # answer is picked up per claim by the ladder in ``resolve.py``, on OpenAlex's free
    # single-entity route and in parallel across the worker pool.
    ep = litapi.epmc_by_dois(todo)

    stored = 0
    for d, rec in ep.items():
        rec["doi"] = d
        store.put(rec)
        stored += 1
    if not quiet:
        print(f"  prefetched {stored}/{len(todo)} papers "
              f"({len(want) - len(todo)} already cached)")
    return stored


def _resolve_one(rec: dict, store: Store, cfg: Config) -> dict:
    """Resolve a single record. Runs in a worker thread — no shared mutable state
    besides ``store`` (thread-safe, see provenance/store.py) and ``litapi.STATS``
    (thread-safe counters)."""
    claim: Claim = rec["claim"]
    res = resolve_claim(claim, rec["doi"], store, cfg)
    row = {k: v for k, v in rec.items() if k != "claim"}

    # Which organism the evidence is actually about. Perturbation tests carry a curated
    # species; edges do not, so it is read back out of the paper — supporting sentence
    # first, then title, then abstract. Kept separate from the network's own species so
    # the Studio can say "this came from somewhere else" rather than implying the claim
    # was made in the modelled organism.
    curated = (rec.get("sp") or "").strip()
    paper = res.paper or {}
    detected = "" if curated else species.detect_species(
        res.support.quote if res.support else "",
        paper.get("title", ""), paper.get("abstract", ""))
    row.update({
        "species": curated or detected,
        "species_source": "curated" if curated else ("paper" if detected else ""),
        "doi": res.doi,
        "evidence": res.support.quote if res.support else "",
        "source_locator": res.support.locator if res.support else "",
        "confidence": res.support.confidence if res.support else 0.0,
        "verification": res.status,
        "verification_reason": res.reason,
        "previous_doi": res.previous_doi,
        "tried": [a.as_dict() for a in res.tried],
    })
    return row


def verify_records(records: List[dict], store: Store, cfg: Config,
                   kind: str, quiet: bool = False, workers: int = 1) -> List[dict]:
    """Resolve every record, reporting progress as it completes.

    With ``workers > 1`` records are resolved concurrently in a thread pool: each
    record is mostly waiting on network I/O (DOI lookup, repair search, full-text
    fetch), so overlapping records overlaps that wait instead of paying it serially.
    The per-host request pacing in ``litapi._wait_turn`` still applies across all
    threads, so this does not increase the request rate any single host sees — it
    just stops one slow/retrying record from blocking every other record behind it.
    Output order always matches input order regardless of completion order.
    """
    total = len(records)
    out: List[Optional[dict]] = [None] * total
    workers = max(1, min(workers, total) or 1)

    if workers == 1:
        for i, rec in enumerate(records):
            out[i] = _resolve_one(rec, store, cfg)
            if not quiet:
                _print_progress(kind, i + 1, total, records[i]["claim"], out[i])
        return out  # type: ignore[return-value]

    done = 0
    print_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_resolve_one, rec, store, cfg): i
                   for i, rec in enumerate(records)}
        for fut in as_completed(futures):
            i = futures[fut]
            row = fut.result()
            out[i] = row
            if not quiet:
                with print_lock:
                    done += 1
                    _print_progress(kind, done, total, records[i]["claim"], row)
    return out  # type: ignore[return-value]


def fill_species_by_paper(*row_sets: List[dict]) -> int:
    """Give every claim the organism already established for its own paper.

    A paper is about one organism, so a species found for any claim on a DOI holds for
    every other claim on it. Detection reads the supporting sentence first, and plenty
    of sentences state a mechanism without naming the species — "The SOS2-SOS3 kinase
    complex up-regulates SOS1" — while a sibling claim on the same paper hit a sentence
    that did. On the salinity network this fills 14 of 31 blanks for free.

    A curated species wins over a detected one for the same DOI: it was recorded by hand
    from the experiment, not inferred from prose.
    """
    known: Dict[str, Tuple[str, str]] = {}
    for rows in row_sets:
        for r in rows:
            doi, sp = r.get("doi"), r.get("species")
            if not doi or not sp:
                continue
            src = r.get("species_source") or "paper"
            if doi not in known or (src == "curated" and known[doi][1] != "curated"):
                known[doi] = (sp, src)

    filled = 0
    for rows in row_sets:
        for r in rows:
            if r.get("species") or not r.get("doi"):
                continue
            hit = known.get(r["doi"])
            if hit:
                r["species"], r["species_source"] = hit[0], "paper"
                filled += 1
    return filled


def _retry_config(cfg: Config) -> Config:
    """A deeper budget for the second pass.

    Only the claims the first pass could not ground reach it — a fraction of the
    network — so it can afford several times the effort per claim and still cost
    seconds. The grounding rule itself is untouched: this buys more places to look, not
    a lower bar for what counts as support.
    """
    return Config(
        max_rounds=4,                    # including the citation-sorted OpenAlex round
        candidates_per_round=16,
        min_candidates_before_pick=3,    # look at real alternatives before choosing
        fulltext=cfg.fulltext,
        fulltext_candidates=4,
        offline=cfg.offline,
        descriptive_query=True,          # the actual fix — see Claim.query
    )


def second_pass(records: List[dict], rows: List[dict], store: Store, cfg: Config,
                kind: str, quiet: bool = False, workers: int = 1) -> Tuple[List[dict], int, int]:
    """Try again, harder, on just the claims the first pass could not ground.

    Returns ``(rows, recovered, attempted)``. The retry's row always replaces the first
    one — even when it fails again — with both attempt trails concatenated, so
    ``evidence.json`` shows everything that was tried rather than only the last thing.
    """
    idx = [i for i, r in enumerate(rows) if r["verification"] == QUARANTINE]
    if not idx or cfg.offline:
        return rows, 0, len(idx)

    if not quiet:
        print(f"  second pass: {len(idx)} ungrounded {kind} claim(s), "
              f"searching by full name")
    again = verify_records([records[i] for i in idx], store, _retry_config(cfg),
                           kind, quiet, workers)

    recovered = 0
    for slot, row in zip(idx, again):
        row["tried"] = (rows[slot]["tried"]
                        + [{"source": "-", "action": "search", "outcome": "retry",
                            "note": "second pass, searching by full name"}]
                        + row["tried"])
        if row["verification"] != QUARANTINE:
            recovered += 1
        rows[slot] = row
    return rows, recovered, len(idx)


def _print_progress(kind: str, i: int, total: int, claim: Claim, row: dict) -> None:
    mark = {VERIFIED: "ok ", REPAIRED: "fix", QUARANTINE: "QRN"}[row["verification"]]
    extra = f" -> {row['doi']}" if row["verification"] == REPAIRED else ""
    print(f"  [{i:>3}/{total}] {mark} {kind} {claim.label[:48]:<48}{extra}")


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


def apply_repairs(net: str, edges: List[dict], tests: List[dict]) -> Tuple[int, int]:
    """Write repaired DOIs and the ungrounded flag back into the Step 1 files.

    Two things change, and nothing else: a repaired claim gets the DOI of the paper that
    actually states it, and a claim that could not be grounded gets ``verification: "q"``
    so the BUILDER can see it. Nothing is added, removed or reordered — the ungroundable
    claims stay in the repository, because most of them are process links the cascade
    needs, and deleting them would quietly cost real biology (CLAUDE.md Rule 12).

    The flag is cleared when a claim is grounded, so a later run that finds the paper
    leaves no stale warning behind. Returns ``(dois_changed, flags_set)``.
    """
    changed = flagged = 0

    def stamp(rec: dict, status: str) -> int:
        """Set or clear the ungrounded marker. Returns 1 if it was newly set."""
        if status == QUARANTINE:
            was = rec.get("verification") == "q"
            rec["verification"] = "q"
            return 0 if was else 1
        rec.pop("verification", None)
        return 0

    cur_path = os.path.join(net, "data", "curated_edges.json")
    if os.path.isfile(cur_path):
        data = light_io.load(cur_path)
        by_key = {(r["s"], r["t"], r["x"]): r for r in edges}
        for e in data.get("edges", []):
            row = by_key.get((e.get("source"), e.get("target"),
                              int(e.get("sign", 0) or 0)))
            if row is None:
                continue
            if row["verification"] == REPAIRED and row["doi"] and e.get("doi") != row["doi"]:
                e["doi"] = row["doi"]
                e["evidence"] = [{"doi": row["doi"]}]
                changed += 1
            flagged += stamp(e, row["verification"])
        if by_key:
            light_io.dump_slim(cur_path, data, "curated_edges")

    pert_path = os.path.join(net, "data", "perturbation_dataset.json")
    if os.path.isfile(pert_path):
        data = light_io.load(pert_path)
        by_id = {r["id"]: r for r in tests}
        for p in data.get("perturbations", []):
            row = by_id.get(p.get("test_id"))
            if row is None:
                continue
            if row["verification"] == REPAIRED and row["doi"] and p.get("doi") != row["doi"]:
                p["doi"] = row["doi"]
                p["evidence"] = [{"doi": row["doi"]}]
                changed += 1
            flagged += stamp(p, row["verification"])
        if by_id:
            light_io.dump_slim(pert_path, data, "perturbation_dataset")

    return changed, flagged


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def summarise(rows: List[dict]) -> Dict[str, int]:
    c = {VERIFIED: 0, REPAIRED: 0, QUARANTINE: 0}
    for r in rows:
        c[r["verification"]] = c.get(r["verification"], 0) + 1
    return c


def print_report(name: str, edges: List[dict], tests: List[dict],
                 papers: Dict[str, dict], elapsed: float,
                 retry: Tuple[int, int] = (0, 0)) -> None:
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
    if retry[1]:
        print(f"second pass: recovered {retry[0]} of {retry[1]} that the first pass "
              f"could not ground")
    print(f"time: {elapsed:.0f}s   {litapi.STATS}")
    # Which endpoint the wall-clock actually went to — the totals alone can't tell a
    # whole-paper full-text download apart from a metadata lookup.
    print(litapi.STATS.breakdown())

    # A spent daily allowance quietly removes a search backend, which shows up as more
    # quarantines rather than as an error. Say so plainly instead.
    for label, why in litapi.metered_out().items():
        print(f"\n  ! {label} was skipped for the rest of this run — quota exhausted.\n"
              f"    {why[:160]}")

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
                   quiet: bool = False, cache: Optional[str] = None,
                   workers: int = 1) -> dict:
    started = time.time()
    name = os.path.basename(os.path.abspath(net.rstrip("/\\")))
    edges, tests, meta = collect_claims(net)
    if not quiet:
        tag = f" ({workers} workers)" if workers > 1 else ""
        print(f"\n{name}: {len(edges)} edges, {len(tests)} perturbation tests{tag}")

    with Store(cache or default_path()) as store:
        prefetch_papers(edges + tests, store, cfg, quiet)
        e_rows = verify_records(edges, store, cfg, "edge", quiet, workers)
        t_rows = verify_records(tests, store, cfg, "test", quiet, workers)

        e_rows, e_got, e_try = second_pass(edges, e_rows, store, cfg, "edge", quiet, workers)
        t_rows, t_got, t_try = second_pass(tests, t_rows, store, cfg, "test", quiet, workers)
        retry = (e_got + t_got, e_try + t_try)

        # Needs every claim resolved first — it borrows across claims that share a paper.
        fill_species_by_paper(e_rows, t_rows)

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
        n, flags = apply_repairs(net, e_rows, t_rows)
        if not quiet:
            print(f"\napplied {n} repaired DOI(s) back to data/"
                  + (f"; flagged {flags} claim(s) as ungrounded (v: q)" if flags else ""))

    if not quiet:
        print_report(name, e_rows, t_rows, papers, time.time() - started, retry)
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
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent records to resolve at once (default 8; "
                         "1 = old sequential behaviour). Verification is network-"
                         "latency-bound, not CPU-bound, so this is the main lever "
                         "on wall-clock time; per-host request pacing is unchanged.")
    args = ap.parse_args(argv)

    cfg = Config(max_rounds=args.max_rounds, candidates_per_round=args.candidates,
                 fulltext=not args.no_fulltext, offline=args.offline)

    worst = 0
    for net in args.network:
        if not os.path.isdir(net):
            print(f"not a directory: {net}", file=sys.stderr)
            worst = 2
            continue
        ev = verify_network(net, cfg, apply=args.apply, quiet=args.quiet,
                            cache=args.cache, workers=args.workers)
        q = ev["summary"]["edges"].get(QUARANTINE, 0) + \
            ev["summary"]["perturbations"].get(QUARANTINE, 0)
        worst = max(worst, 1 if q else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main())

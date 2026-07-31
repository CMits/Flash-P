#!/usr/bin/env python3
"""
FLASH-P v1.0 - Network Structure QA Check

Deterministic structural validator for network.json. Runs six checks that
can be verified without biological judgment, reports violations, and
optionally auto-repairs the safely-fixable ones.

The 6 checks:
  1. Connectivity: every node must reach the PHENOTYPE via a directed path.
  2. DOI presence and shape: every edge must carry a well-formed 10.xxxx/yyy DOI.
  3. Node naming conventions: regex per node type.
  4. is_source flag correctness: is_source=true iff node has no incoming edges.
  5. Phenotype node sanity: exactly one PHENOTYPE-typed node matching metadata.
  6. Evidence: every edge is grounded in a paper we actually retrieved and read,
     per data/evidence.json (written by verify_evidence.py, pipeline Step 1.6).

Checks 2 and 6 are deliberately separate. A DOI can be perfectly formed, resolve
to a real paper, and still be the wrong citation — that is in fact the common
failure, and no amount of string checking finds it. Check 2 catches typos;
check 6 reports what happened when the paper was actually read.

Usage:
    python check_network_structure.py <network_dir> [--dry-run] [--fix] [--backup]

Exit codes:
    0 = all checks pass (or --fix resolved all auto-fixable issues)
    1 = one or more checks failed

Auto-fixable: checks 1 (connectivity) and 4 (is_source).
Report-only: checks 2 (DOI), 3 (naming), 5 (phenotype sanity), 6 (evidence).

The script is non-blocking: it is NOT registered as a settings.json hook.
Run it manually or from the BUILDER agent's post-build self-check.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


# ---------------------------------------------------------------------------
# Naming conventions per node type
# ---------------------------------------------------------------------------

# Pragmatic regexes - permissive enough for valid abbreviations (GA, T6P, ABA)
# but strict enough to catch obvious case-inconsistency typos.
NAMING_REGEX = {
    "GENE":             re.compile(r"^[A-Z][A-Z0-9_]*$"),       # BRC1, MAX2, D14
    "PROTEIN_COMPLEX":  re.compile(r"^[A-Z][A-Z0-9_]*$"),       # SMXL678, DELLA
    "HORMONE":          re.compile(r"^[A-Z][A-Za-z0-9_]*$"),    # Strigolactone, GA, T6P
    "METABOLITE":       re.compile(r"^[A-Z][A-Za-z0-9_]*$"),    # Sucrose, T6P
    "ENVIRONMENT":      re.compile(r"^[A-Z][A-Za-z0-9_]*$"),    # Low_R_FR, Photoperiod
    "PHENOTYPE":        re.compile(r"^[A-Z][A-Za-z0-9_]*$"),    # Shoot_Branching
    "REGULATORY_RNA":   re.compile(r"^[a-z]+[0-9A-Z]"),         # miR156, lncRNA42
}


# ---------------------------------------------------------------------------
# Evidence / DOI extraction
# ---------------------------------------------------------------------------

def extract_doi(edge: Dict) -> str:
    """Extract DOI from an edge's evidence block.

    Handles both flat (v1.0) and nested (v1.0) evidence shapes:
      v1.0: edge.evidence = [{"doi": "...", ...}, ...]
      v1.0: edge.evidence = [{"source": {"doi": "..."}, ...}, ...]
    Also handles evidence as a dict (single-item shorthand).
    """
    ev = edge.get("evidence")
    if isinstance(ev, dict):
        ev_list = [ev]
    elif isinstance(ev, list):
        ev_list = ev
    else:
        return ""
    for item in ev_list:
        if not isinstance(item, dict):
            continue
        doi = item.get("doi", "")
        if not doi and isinstance(item.get("source"), dict):
            doi = item["source"].get("doi", "")
        if doi:
            return doi
    return ""


# The bare form only: no 'doi:' prefix, no https://doi.org/ URL. Those resolve in a
# browser but break every string comparison and every API lookup downstream.
DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")


# ---------------------------------------------------------------------------
# Check 6: Evidence grounding (reads data/evidence.json)
# ---------------------------------------------------------------------------

def check_evidence(network_path: Path, edges: List[Dict]) -> Dict[str, Any]:
    """Summarise data/evidence.json against the edges actually in the network.

    Reports rather than judges. An unverified edge is not automatically wrong — the
    paper may be paywalled, or the claim may be spread across two sentences — so this
    surfaces the counts and lets a human decide. What it will not do is let a network
    quietly claim provenance it does not have.

    The join is on (source, target, sign): the BUILDER renumbers edge ids between
    curated_edges.json and network.json, so ids cannot be matched across the two.
    """
    ev_file = network_path.parent.parent / "data" / "evidence.json"
    if not ev_file.exists():
        return {
            "present": False,
            "passed": False,
            "message": (f"no {ev_file.name} — this network's citations have never been "
                        f"checked against the papers. Run: "
                        f"python Agent/shared/verify_evidence.py <NET>"),
        }
    try:
        ev = json.loads(ev_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"present": True, "passed": False, "message": f"unreadable evidence.json: {e}"}

    def key(s, t, x):
        return (str(s).upper(), str(t).upper(), 1 if int(x or 0) >= 0 else -1)

    by_edge = {key(r.get("s"), r.get("t"), r.get("x")): r for r in ev.get("edges", [])}

    counts = {"verified": 0, "repaired": 0, "quarantine": 0}
    ungrounded: List[Dict] = []
    unchecked: List[Dict] = []
    for e in edges:
        rec = by_edge.get(key(e.get("source"), e.get("target"), e.get("sign")))
        if rec is None:
            unchecked.append({"source": e.get("source"), "target": e.get("target"),
                              "sign": e.get("sign")})
            continue
        st = rec.get("verification", "quarantine")
        counts[st] = counts.get(st, 0) + 1
        if st == "quarantine":
            ungrounded.append({"source": e.get("source"), "target": e.get("target"),
                               "doi": rec.get("doi", ""),
                               "reason": rec.get("verification_reason", "")})

    total = len(edges)
    grounded = counts["verified"] + counts["repaired"]
    summary = ev.get("summary", {})
    return {
        "present": True,
        "counts": counts,
        "grounded": grounded,
        "total_edges": total,
        "grounded_pct": round(100.0 * grounded / total, 1) if total else 0.0,
        "ungrounded": ungrounded,
        # Edges added after verification ran — the network changed and nobody re-checked.
        "unchecked": unchecked,
        "papers": summary.get("papers", 0),
        "papers_with_fulltext": summary.get("papers_with_fulltext", 0),
        "passed": not ungrounded and not unchecked,
    }


# ---------------------------------------------------------------------------
# Check 1: Connectivity (backward BFS from phenotype)
# ---------------------------------------------------------------------------

def check_connectivity(
    nodes: List[Dict], edges: List[Dict], phenotype_id: str,
) -> Tuple[Set[str], List[Dict]]:
    """Return (reachable_set, floating_edges).

    reachable_set = nodes that can reach the phenotype via a directed path.
    floating_edges = edges whose source is not in the reachable set.
    """
    # Build reverse adjacency: target -> [sources]
    reverse_adj: Dict[str, List[str]] = {}
    for e in edges:
        reverse_adj.setdefault(e["target"], []).append(e["source"])

    # BFS from phenotype along reverse edges
    reachable: Set[str] = {phenotype_id}
    queue: deque = deque([phenotype_id])
    while queue:
        node = queue.popleft()
        for src in reverse_adj.get(node, []):
            if src not in reachable:
                reachable.add(src)
                queue.append(src)

    # Floating edges: src not in reachable OR target not in reachable
    floating_edges = [
        e for e in edges
        if e["source"] not in reachable or e["target"] not in reachable
    ]

    return reachable, floating_edges


# ---------------------------------------------------------------------------
# Check 4: is_source flag correctness
# ---------------------------------------------------------------------------

def check_is_source(nodes: List[Dict], edges: List[Dict]) -> List[Dict]:
    """Return nodes with mismatched is_source flag.

    A node should have is_source=true iff it has no incoming edges.
    Each returned dict has: node_id, has_incoming, declared_flag, should_be.
    """
    incoming_count: Dict[str, int] = {}
    for e in edges:
        incoming_count[e["target"]] = incoming_count.get(e["target"], 0) + 1

    mismatches = []
    for n in nodes:
        nid = n["id"]
        has_incoming = incoming_count.get(nid, 0) > 0
        declared = bool(n.get("is_source", False))
        should_be = not has_incoming
        if declared != should_be:
            mismatches.append({
                "node_id": nid,
                "type": n.get("type", ""),
                "has_incoming": has_incoming,
                "declared_flag": declared,
                "should_be": should_be,
            })
    return mismatches


# ---------------------------------------------------------------------------
# Audit runner
# ---------------------------------------------------------------------------

def audit_network(network_path: Path) -> Dict[str, Any]:
    """Run all five checks and return a structured report."""
    import light_io  # Light: read short-key/TOON network, expand to rich (incl. evidence from doi)
    net = light_io.load(network_path)

    nodes = net.get("nodes", [])
    edges = net.get("edges", [])
    metadata = net.get("metadata", {})

    # Phenotype identification
    phenotype_nodes = [n for n in nodes if n.get("type") == "PHENOTYPE"]
    phenotype_id_from_meta = metadata.get("phenotype_node")

    # Check 5: phenotype sanity
    phenotype_issues: List[str] = []
    phenotype_id = None
    if len(phenotype_nodes) == 0:
        phenotype_issues.append("No PHENOTYPE-typed node found")
    elif len(phenotype_nodes) > 1:
        phenotype_issues.append(
            f"Multiple PHENOTYPE-typed nodes: "
            f"{[n['id'] for n in phenotype_nodes]}"
        )
    else:
        phenotype_id = phenotype_nodes[0]["id"]
        if phenotype_id_from_meta and phenotype_id_from_meta != phenotype_id:
            phenotype_issues.append(
                f"metadata.phenotype_node ({phenotype_id_from_meta!r}) "
                f"does not match PHENOTYPE-typed node ({phenotype_id!r})"
            )

    # Check 1: connectivity (requires phenotype)
    reachable: Set[str] = set()
    floating_edges: List[Dict] = []
    floating_nodes: List[Dict] = []
    if phenotype_id:
        reachable, floating_edges = check_connectivity(nodes, edges, phenotype_id)
        floating_nodes = [n for n in nodes if n["id"] not in reachable]

    # Check 2: DOI presence AND shape.
    # Presence alone caught nothing: every DOI in every built network is non-empty,
    # so this check passed on all of them, wrong ones included. Shape catches typos;
    # whether the paper actually says what the edge claims is check 6's job, because
    # only reading the paper can answer that.
    edges_missing_doi = [e for e in edges if not extract_doi(e)]
    edges_malformed_doi = [e for e in edges
                           if extract_doi(e) and not DOI_SHAPE.match(extract_doi(e))]

    # Check 6: evidence — is every claim grounded in a paper we actually read?
    evidence_report = check_evidence(network_path, edges)

    # Check 3: naming conventions
    naming_violations = []
    for n in nodes:
        ntype = n.get("type", "")
        rx = NAMING_REGEX.get(ntype)
        if rx and not rx.match(n["id"]):
            naming_violations.append({
                "node_id": n["id"],
                "type": ntype,
                "expected_pattern": rx.pattern,
            })

    # Check 4: is_source correctness
    source_mismatches = check_is_source(nodes, edges)

    return {
        "network_path": str(network_path),
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "phenotype_id": phenotype_id,
        "check_1_connectivity": {
            "reachable_count": len(reachable),
            "floating_nodes": floating_nodes,
            "floating_edges": floating_edges,
            "passed": len(floating_nodes) == 0,
        },
        "check_2_doi": {
            "missing_count": len(edges_missing_doi),
            "malformed_count": len(edges_malformed_doi),
            "edges": edges_missing_doi,
            "malformed": edges_malformed_doi,
            "passed": len(edges_missing_doi) == 0 and len(edges_malformed_doi) == 0,
        },
        "check_6_evidence": evidence_report,
        "check_3_naming": {
            "violation_count": len(naming_violations),
            "violations": naming_violations,
            "passed": len(naming_violations) == 0,
        },
        "check_4_is_source": {
            "mismatch_count": len(source_mismatches),
            "mismatches": source_mismatches,
            "passed": len(source_mismatches) == 0,
        },
        "check_5_phenotype": {
            "issue_count": len(phenotype_issues),
            "issues": phenotype_issues,
            "phenotype_count": len(phenotype_nodes),
            "passed": len(phenotype_issues) == 0,
        },
    }


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------

def fix_network(network_path: Path, report: Dict, backup: bool) -> List[str]:
    """Apply safe fixes (connectivity, is_source) to network.json.

    Returns list of fix descriptions. Does NOT touch DOI, naming, or
    phenotype issues (those need human judgment).
    """
    import light_io  # Light: read short-key/TOON network, expand to rich (incl. evidence from doi)
    net = light_io.load(network_path)

    fixes_applied: List[str] = []

    # Fix 1: remove floating nodes and their edges
    floating = report["check_1_connectivity"]["floating_nodes"]
    if floating:
        floating_ids = {n["id"] for n in floating}
        original_node_count = len(net["nodes"])
        original_edge_count = len(net["edges"])

        net["nodes"] = [n for n in net["nodes"] if n["id"] not in floating_ids]
        net["edges"] = [
            e for e in net["edges"]
            if e["source"] not in floating_ids and e["target"] not in floating_ids
        ]

        removed_nodes = original_node_count - len(net["nodes"])
        removed_edges = original_edge_count - len(net["edges"])
        fixes_applied.append(
            f"Removed {removed_nodes} floating nodes: {sorted(floating_ids)} "
            f"(and {removed_edges} incident edges)"
        )

    # Fix 4: correct is_source flags
    mismatches = report["check_4_is_source"]["mismatches"]
    if mismatches:
        by_id = {m["node_id"]: m for m in mismatches}
        for n in net["nodes"]:
            if n["id"] in by_id:
                n["is_source"] = by_id[n["id"]]["should_be"]
        fixes_applied.append(
            f"Corrected is_source on {len(mismatches)} nodes: "
            f"{[m['node_id'] for m in mismatches]}"
        )

    # Update metadata totals
    if fixes_applied and "metadata" in net:
        net["metadata"]["total_nodes"] = len(net["nodes"])
        net["metadata"]["total_edges"] = len(net["edges"])
        sources = sum(1 for n in net["nodes"] if n.get("is_source"))
        net["metadata"]["source_nodes"] = sources
        if len(net["nodes"]) > 0:
            net["metadata"]["source_percentage"] = round(
                sources / len(net["nodes"]) * 100, 1
            )

    # Backup + write
    if fixes_applied:
        if backup:
            backup_path = network_path.with_suffix(".json.before_filter")
            shutil.copy2(network_path, backup_path)
            fixes_applied.append(f"Backup saved: {backup_path.name}")
        with open(network_path, "w", encoding="utf-8") as f:
            json.dump(net, f, indent=2, ensure_ascii=False)

    return fixes_applied


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_report(report: Dict, fix_mode: bool = False) -> str:
    lines: List[str] = []
    name = Path(report["network_path"]).parent.parent.name
    lines.append(f"=== Network Structure Audit: {name} ===")
    lines.append(
        f"Total nodes: {report['total_nodes']}  |  "
        f"Total edges: {report['total_edges']}  |  "
        f"Phenotype: {report['phenotype_id']}"
    )
    lines.append("")

    # Check 1
    c1 = report["check_1_connectivity"]
    lines.append("[1/6] Connectivity (BFS from phenotype)")
    if c1["passed"]:
        lines.append(f"  PASS - all {c1['reachable_count']} nodes reach the phenotype")
    else:
        lines.append(
            f"  FLOATING: {len(c1['floating_nodes'])} nodes, "
            f"{len(c1['floating_edges'])} edges"
        )
        for n in c1["floating_nodes"]:
            lines.append(f"    - {n['id']:30s} ({n.get('type', '')})")
        for e in c1["floating_edges"]:
            lines.append(f"      edge: {e['source']} -> {e['target']} "
                         f"(sign={e.get('sign', '?')})")
        if fix_mode:
            lines.append("  -> --fix removed these")
        else:
            lines.append("  -> --fix would remove these")

    # Check 2
    c2 = report["check_2_doi"]
    lines.append("")
    lines.append("[2/6] DOI presence and shape")
    if c2["passed"]:
        lines.append(f"  PASS - all {report['total_edges']} edges carry a well-formed DOI")
    else:
        if c2["missing_count"]:
            lines.append(f"  MISSING DOIs: {c2['missing_count']} edges")
            for e in c2["edges"][:10]:
                lines.append(f"    - {e.get('source')} -> {e.get('target')} "
                             f"(edge_id={e.get('edge_id', '?')})")
            if c2["missing_count"] > 10:
                lines.append(f"    ... and {c2['missing_count'] - 10} more")
        if c2.get("malformed_count"):
            lines.append(f"  MALFORMED DOIs: {c2['malformed_count']} edges "
                         f"(expected the bare form 10.xxxx/suffix)")
            for e in c2["malformed"][:10]:
                lines.append(f"    - {e.get('source')} -> {e.get('target')}: "
                             f"{extract_doi(e)[:60]!r}")
        lines.append("  -> NOT auto-fixable (needs re-curation)")
        lines.append("  Note: a well-formed DOI can still be the wrong paper — see check 6.")

    # Check 3
    c3 = report["check_3_naming"]
    lines.append("")
    lines.append("[3/6] Node naming conventions")
    if c3["passed"]:
        lines.append(f"  PASS - all {report['total_nodes']} node names "
                     f"match type-specific regex")
    else:
        lines.append(f"  VIOLATIONS: {c3['violation_count']}")
        for v in c3["violations"]:
            lines.append(
                f"    - {v['node_id']:30s} ({v['type']:15s}) "
                f"expected pattern: {v['expected_pattern']}"
            )
        lines.append("  -> NOT auto-fixable (renaming needs human judgment)")

    # Check 4
    c4 = report["check_4_is_source"]
    lines.append("")
    lines.append("[4/6] is_source flag correctness")
    if c4["passed"]:
        lines.append("  PASS - all is_source flags consistent with edge structure")
    else:
        lines.append(f"  MISMATCHES: {c4['mismatch_count']}")
        for m in c4["mismatches"]:
            lines.append(
                f"    - {m['node_id']:30s} ({m['type']:15s}) "
                f"declared={m['declared_flag']}, should_be={m['should_be']} "
                f"(has_incoming={m['has_incoming']})"
            )
        if fix_mode:
            lines.append("  -> --fix corrected these")
        else:
            lines.append("  -> --fix would correct these")

    # Check 5
    c5 = report["check_5_phenotype"]
    lines.append("")
    lines.append("[5/6] Phenotype node sanity")
    if c5["passed"]:
        lines.append(
            f"  PASS - exactly 1 PHENOTYPE node "
            f"matching metadata.phenotype_node"
        )
    else:
        for issue in c5["issues"]:
            lines.append(f"  ISSUE: {issue}")
        lines.append("  -> NOT auto-fixable (needs manual metadata correction)")

    # Check 6
    c6 = report["check_6_evidence"]
    lines.append("")
    lines.append("[6/6] Evidence grounding")
    if not c6.get("present"):
        lines.append(f"  NOT CHECKED - {c6.get('message', '')}")
    elif c6["passed"]:
        lines.append(f"  PASS - all {c6['total_edges']} edges grounded in a retrieved paper "
                     f"({c6['counts']['verified']} verified, {c6['counts']['repaired']} repaired)")
        lines.append(f"  {c6['papers']} papers cited, {c6['papers_with_fulltext']} with "
                     f"open-access full text")
    else:
        lines.append(f"  GROUNDED: {c6['grounded']}/{c6['total_edges']} ({c6['grounded_pct']}%)  "
                     f"[verified {c6['counts']['verified']}, repaired {c6['counts']['repaired']}, "
                     f"unverified {c6['counts']['quarantine']}]")
        for e in c6["ungrounded"][:8]:
            lines.append(f"    - {e['source']} -> {e['target']}: {e['reason'][:74]}")
        if len(c6["ungrounded"]) > 8:
            lines.append(f"    ... and {len(c6['ungrounded']) - 8} more")
        if c6["unchecked"]:
            lines.append(f"  NOT IN evidence.json: {len(c6['unchecked'])} edges — the network "
                         f"changed after verification ran; re-run verify_evidence.py")
        lines.append("  -> NOT auto-fixable (an unverified claim needs a human, or a better paper)")

    # Summary
    passed = sum(1 for k in ("check_1_connectivity", "check_2_doi",
                              "check_3_naming", "check_4_is_source",
                              "check_5_phenotype", "check_6_evidence") if report[k]["passed"])
    failed = 6 - passed
    lines.append("")
    if failed == 0:
        lines.append("RESULT: ALL 6 CHECKS PASSED  -  Exit code: 0")
    else:
        lines.append(f"RESULT: {failed} CHECK(S) FAILED  -  Exit code: 1")
        if not fix_mode:
            lines.append("Hint: run with --fix to auto-repair safely-fixable checks "
                         "(connectivity, is_source).")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="FLASH-P network structure QA check"
    )
    parser.add_argument("network_dir", type=str,
                        help="Path to a phenotype network directory "
                             "(expects network/network.json inside)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only, no writes (default)")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-repair safely-fixable checks "
                             "(connectivity, is_source)")
    parser.add_argument("--backup", action="store_true",
                        help="If --fix, save original as "
                             "network.json.before_filter before rewriting")
    args = parser.parse_args()

    network_dir = Path(args.network_dir)
    if not network_dir.is_absolute():
        network_dir = Path.cwd() / network_dir
    network_path = network_dir / "network" / "network.json"
    if not network_path.exists():
        print(f"Error: {network_path} not found", file=sys.stderr)
        return 2

    report = audit_network(network_path)

    # Apply fixes if requested
    fix_mode = args.fix and not args.dry_run if args.fix else False
    # Resolve flag conflict: if --fix is passed, override --dry-run default
    if args.fix:
        fix_mode = True
        # Only fix if there is something auto-fixable
        auto_fixable = (
            not report["check_1_connectivity"]["passed"]
            or not report["check_4_is_source"]["passed"]
        )
        if auto_fixable:
            fixes = fix_network(network_path, report, backup=args.backup)
            # Re-run audit after fix
            report = audit_network(network_path)
            print(render_report(report, fix_mode=True))
            print()
            print("--- Fixes applied ---")
            for f in fixes:
                print(f"  {f}")
        else:
            print(render_report(report, fix_mode=False))
            print()
            print("No auto-fixable issues found; --fix had nothing to do.")
    else:
        print(render_report(report, fix_mode=False))

    # Exit code: 0 if all pass (after any fixes applied), 1 otherwise
    all_pass = all(
        report[k]["passed"]
        for k in ("check_1_connectivity", "check_2_doi",
                  "check_3_naming", "check_4_is_source",
                  "check_5_phenotype", "check_6_evidence")
    )
    return 0 if all_pass else 1


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Run synthetic-network tests for all 6 checks."""
    import tempfile

    def make_net(nodes: List[Dict], edges: List[Dict],
                 phenotype_in_meta: str | None = None) -> Dict:
        meta = {"phenotype": "test", "species": "synthetic"}
        if phenotype_in_meta:
            meta["phenotype_node"] = phenotype_in_meta
        return {"metadata": meta, "nodes": nodes, "edges": edges}

    def run_audit(net: Dict, evidence: Dict | None = None) -> Dict:
        # Mirror the real layout (<NET>/network/network.json + <NET>/data/), because
        # check 6 resolves evidence.json relative to the network file.
        with tempfile.TemporaryDirectory() as tmp:
            netdir = Path(tmp) / "Trait"
            (netdir / "network").mkdir(parents=True)
            (netdir / "data").mkdir()
            path = netdir / "network" / "network.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(net, f)
            if evidence is not None:
                with open(netdir / "data" / "evidence.json", "w", encoding="utf-8") as f:
                    json.dump(evidence, f)
            return audit_network(path)

    def evidence_for(edges: List[Dict], status: str = "verified") -> Dict:
        return {"summary": {"papers": len(edges), "papers_with_fulltext": 0},
                "papers": {}, "perturbations": [],
                "edges": [{"s": e["source"], "t": e["target"], "x": e["sign"],
                           "doi": extract_doi(e), "evidence": "a real sentence from the paper",
                           "source_locator": "abstract", "verification": status,
                           "verification_reason": "" if status != "quarantine" else "not co-mentioned"}
                          for e in edges]}

    # --- Test 1: connectivity ---
    # A -> B -> Phenotype, plus isolated C -> D
    net = make_net(
        nodes=[
            {"id": "A", "type": "GENE"},
            {"id": "B", "type": "GENE"},
            {"id": "C", "type": "GENE"},
            {"id": "D", "type": "GENE"},
            {"id": "Phenotype", "type": "PHENOTYPE"},
        ],
        edges=[
            {"source": "A", "target": "B", "sign": 1,
             "evidence": [{"doi": "10.1234/x"}]},
            {"source": "B", "target": "Phenotype", "sign": 1,
             "evidence": [{"doi": "10.1234/y"}]},
            {"source": "C", "target": "D", "sign": 1,
             "evidence": [{"doi": "10.1234/z"}]},
        ],
    )
    r = run_audit(net)
    floating_ids = {n["id"] for n in r["check_1_connectivity"]["floating_nodes"]}
    assert floating_ids == {"C", "D"}, f"Expected {{C, D}} floating, got {floating_ids}"
    assert not r["check_1_connectivity"]["passed"]
    print("[1/6] connectivity: detected C, D floating  - OK")

    # --- Test 2: DOI ---
    net_no_doi = make_net(
        nodes=[
            {"id": "A", "type": "GENE"},
            {"id": "Phenotype", "type": "PHENOTYPE"},
        ],
        edges=[{"source": "A", "target": "Phenotype", "sign": 1,
                 "evidence": [{"title": "no doi"}]}],
    )
    r = run_audit(net_no_doi)
    assert not r["check_2_doi"]["passed"]
    assert r["check_2_doi"]["missing_count"] == 1
    print("[2/6] DOI presence: detected missing DOI  - OK")

    # --- Test 3: naming ---
    net_bad_name = make_net(
        nodes=[
            {"id": "brc1", "type": "GENE"},  # wrong: should be ALL_CAPS
            {"id": "Phenotype", "type": "PHENOTYPE"},
        ],
        edges=[{"source": "brc1", "target": "Phenotype", "sign": 1,
                 "evidence": [{"doi": "10.1234/x"}]}],
    )
    r = run_audit(net_bad_name)
    assert not r["check_3_naming"]["passed"]
    assert r["check_3_naming"]["violations"][0]["node_id"] == "brc1"
    print("[3/6] naming: detected lowercase GENE  - OK")

    # --- Test 4: is_source ---
    # Node A has no incoming; is_source=false -> mismatch
    net_bad_source = make_net(
        nodes=[
            {"id": "A", "type": "GENE", "is_source": False},  # should be true
            {"id": "B", "type": "GENE", "is_source": True},   # has incoming, wrong
            {"id": "Phenotype", "type": "PHENOTYPE"},
        ],
        edges=[
            {"source": "A", "target": "B", "sign": 1,
             "evidence": [{"doi": "10.1234/x"}]},
            {"source": "B", "target": "Phenotype", "sign": 1,
             "evidence": [{"doi": "10.1234/y"}]},
        ],
    )
    r = run_audit(net_bad_source)
    assert not r["check_4_is_source"]["passed"]
    mismatch_ids = {m["node_id"] for m in r["check_4_is_source"]["mismatches"]}
    assert mismatch_ids == {"A", "B"}
    print("[4/6] is_source: detected mismatches on A, B  - OK")

    # --- Test 5: phenotype ---
    net_two_phen = make_net(
        nodes=[
            {"id": "A", "type": "GENE"},
            {"id": "P1", "type": "PHENOTYPE"},
            {"id": "P2", "type": "PHENOTYPE"},
        ],
        edges=[{"source": "A", "target": "P1", "sign": 1,
                 "evidence": [{"doi": "10.1234/x"}]}],
    )
    r = run_audit(net_two_phen)
    assert not r["check_5_phenotype"]["passed"]
    assert "Multiple" in r["check_5_phenotype"]["issues"][0]
    print("[5/6] phenotype: detected two PHENOTYPE nodes  - OK")

    # --- Test 6: evidence grounding ---
    clean_edges = [
        {"source": "A", "target": "B", "sign": 1, "evidence": [{"doi": "10.1234/aaa"}]},
        {"source": "B", "target": "Phenotype", "sign": 1, "evidence": [{"doi": "10.1234/bbb"}]},
    ]
    clean_nodes = [
        {"id": "A", "type": "GENE", "is_source": True},
        {"id": "B", "type": "GENE", "is_source": False},
        {"id": "Phenotype", "type": "PHENOTYPE", "is_source": False},
    ]
    net_clean = make_net(nodes=clean_nodes, edges=clean_edges)

    r = run_audit(net_clean)                       # no evidence.json at all
    assert not r["check_6_evidence"]["passed"] and not r["check_6_evidence"]["present"]
    print("[6/6] evidence: unverified network reported as unchecked  - OK")

    r = run_audit(net_clean, evidence_for(clean_edges, "quarantine"))
    assert not r["check_6_evidence"]["passed"]
    assert len(r["check_6_evidence"]["ungrounded"]) == 2
    print("[6/6] evidence: quarantined edges surfaced  - OK")

    # An edge added after verification must not silently count as grounded.
    r = run_audit(make_net(nodes=clean_nodes + [{"id": "C", "type": "GENE"}],
                           edges=clean_edges + [{"source": "C", "target": "B", "sign": 1,
                                                 "evidence": [{"doi": "10.1234/ccc"}]}]),
                  evidence_for(clean_edges))
    assert len(r["check_6_evidence"]["unchecked"]) == 1
    print("[6/6] evidence: edge added after verification flagged  - OK")

    # Malformed DOI must fail check 2 (it passed the old truthiness test).
    r = run_audit(make_net(nodes=clean_nodes,
                           edges=[{"source": "A", "target": "Phenotype", "sign": 1,
                                   "evidence": [{"doi": "doi:10.1234/x"}]}]))
    assert r["check_2_doi"]["malformed_count"] == 1 and not r["check_2_doi"]["passed"]
    print("[2/6] doi: malformed 'doi:' prefix rejected  - OK")

    # --- Clean network: all pass ---
    r = run_audit(net_clean, evidence_for(clean_edges))
    for k in ("check_1_connectivity", "check_2_doi", "check_3_naming",
              "check_4_is_source", "check_5_phenotype", "check_6_evidence"):
        assert r[k]["passed"], f"{k} should pass on clean network: {r[k]}"
    print("[All 6] clean network: all checks pass  - OK")

    print("\nAll self-tests passed.")


if __name__ == "__main__":
    # If invoked as `python check_network_structure.py --self-test`
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        _self_test()
        sys.exit(0)
    sys.exit(main())

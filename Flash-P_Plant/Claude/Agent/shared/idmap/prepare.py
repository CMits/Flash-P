#!/usr/bin/env python3
"""Run every mechanical step of gene identifier mapping for one network.

Builds the node dossiers from the network and its evidence, mines identifiers out of the
cited papers, plans the routes, and gathers candidates. What comes out is a dossier file
in which each gene node carries its evidence, its candidates and the routes that produced
them -- the material the flashp-gene-id-mapper subagent then judges. Nothing here decides
which identifier is right; that is the judgement this deliberately stops short of.

Usage:
    python Agent/shared/idmap/prepare.py <NET>
    python Agent/shared/idmap/prepare.py <NET> --offline --no-plaza

The gathering step makes network calls and takes a few minutes for a whole network.

Exit codes:
    0  ready to judge
    1  unexpected error
    2  not a Flash-P network directory
    3  no data/evidence.json (pre-Step-1.6 network) -- see --allow-no-evidence
    4  no mappable gene nodes
    6  <NET>/idmapping/ holds output from the superseded mapper -- see --force
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402


def step(name, argv):
    """Run one stage. Its exit code is the pipeline's exit code -- the codes above are
    raised by the stage that detects the condition, and propagating them unchanged is what
    lets the command file tell the user which of them happened."""
    print(f"\n[{name}]", file=sys.stderr)
    r = subprocess.run([sys.executable] + argv)
    if r.returncode != 0:
        print(f"{name} failed with exit code {r.returncode}", file=sys.stderr)
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("net_dir", help="a built Flash-P network directory, e.g. networks/Grain_Yield")
    ap.add_argument("--out", help="default: <NET>/idmapping")
    ap.add_argument("--offline", action="store_true",
                    help="cache and descriptions only; skip every route needing network access")
    ap.add_argument("--no-fulltext", action="store_true",
                    help="mine identifiers from evidence sentences only, not cached full texts")
    ap.add_argument("--no-plaza", action="store_true",
                    help="skip PLAZA; avoids a large one-off download for a new species pair")
    ap.add_argument("--no-db-prefetch", action="store_true",
                    help="query the live databases per node rather than in batches")
    ap.add_argument("--no-propagate", action="store_true",
                    help="do not offer one node's appositive pairings to other nodes")
    ap.add_argument("--allow-no-evidence", action="store_true",
                    help="map a pre-Step-1.6 network from network.json alone; every "
                         "literature-derived route is then inert and confidence drops accordingly")
    ap.add_argument("--anchors",
                    help="comma-separated species to probe, overriding the relatedness ranking. "
                         "Re-run routing with this after adjudicating an anchor.")
    ap.add_argument("--species", help="comma-separated extra species for the identifier panel")
    ap.add_argument("--workers", type=int, help="nodes gathered concurrently (default 3)")
    ap.add_argument("--limit", type=int, help="only this many nodes, for a quick look")
    ap.add_argument("--force", action="store_true",
                    help="move a superseded mapper's output aside to <outdir>_v1 and continue")
    args = ap.parse_args()

    net = os.path.abspath(args.net_dir)
    if not common.network_json(net):
        print(f"{args.net_dir} is not a FLASH-P network directory "
              f"(no network/network.json and no network.json)", file=sys.stderr)
        return common.EXIT_NOT_A_NETWORK

    outdir = args.out or os.path.join(net, "idmapping")

    # The superseded map_gene_ids.py wrote to this same directory, and 24 networks still
    # carry its output. Its network.idmapped.json would be overwritten while candidates.tsv
    # and report.md survived beside the new files, leaving a directory that is half one
    # mapper and half the other. Move the old run aside rather than blending the two.
    stale = [f for f in ("candidates.tsv", "report.md", "corroboration_packet.txt")
             if os.path.isfile(os.path.join(outdir, f))]
    if stale:
        if not args.force:
            print(f"{os.path.relpath(outdir)} holds output from the superseded mapper "
                  f"({', '.join(stale)}).\nRe-run with --force to move it to "
                  f"{os.path.basename(outdir)}_v1/ and continue.", file=sys.stderr)
            return common.EXIT_STALE_OUTPUT
        keep = outdir + "_v1"
        if os.path.exists(keep):
            shutil.rmtree(keep)
        shutil.move(outdir, keep)
        print(f"moved the superseded run to {os.path.relpath(keep)}", file=sys.stderr)

    os.makedirs(outdir, exist_ok=True)
    doss = os.path.join(outdir, "node_dossiers.json")

    dossiers = [os.path.join(HERE, "build_dossiers.py"), "--network", net, "--out", doss]
    if args.allow_no_evidence:
        dossiers.append("--allow-no-evidence")
    step("dossiers", dossiers)

    mine = [os.path.join(HERE, "mine_evidence_ids.py"), "--dossiers", doss]
    if args.no_fulltext:
        mine.append("--no-fulltext")
    if args.no_propagate:
        mine.append("--no-propagate")
    if args.species:
        mine += ["--species", args.species]
    step("identifiers in cited papers", mine)

    route = [os.path.join(HERE, "route_node.py"), "--dossiers", doss]
    if args.anchors:
        route += ["--anchors", args.anchors]
    step("routing", route)

    gather = [os.path.join(HERE, "gather_candidates.py"), "--dossiers", doss]
    if args.offline:
        gather.append("--offline")
    if args.no_plaza:
        gather.append("--no-plaza")
    if args.no_db_prefetch:
        gather.append("--no-db-prefetch")
    if args.workers:
        gather += ["--workers", str(args.workers)]
    if args.limit:
        gather += ["--limit", str(args.limit)]
    step("candidates", gather)

    d = common.load_json(doss)
    s = d["summary"]
    # Every node picks up a `gathered` key (identifier mining annotates them all), but only
    # the nodes the gatherer actually reached carry `candidates` -- which is what --limit
    # makes visible. Test for the key that means "this node was gathered".
    nodes = [n for n in d["nodes"]
             if n["mappable"] and "candidates" in n.get("gathered", {})]
    if not nodes:
        print(f"\nno mappable gene nodes were gathered in {args.net_dir}", file=sys.stderr)
        return common.EXIT_NO_GENE_NODES
    with_c = [n for n in nodes if n["gathered"]["candidates"]]

    rel = os.path.relpath(outdir)
    print(f"\nReady to judge: {rel}")
    print(f"  {s['n_mappable']} gene nodes; {len(with_c)} of {len(nodes)} prepared nodes "
          f"have candidates")
    for w in s.get("routing_warnings", []):
        print(f"  NOTE: {w}")
    print(f"\n  The flashp-gene-id-mapper subagent judges each node, writes "
          f"{os.path.join(rel, 'judgements.jsonl')}, then runs:")
    print(f"    python Agent/shared/idmap/emit_mapping.py "
          f"--dossiers {os.path.join(rel, 'node_dossiers.json')} "
          f"--judgements {os.path.join(rel, 'judgements.jsonl')} --outdir {rel}")
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""
Export a verified FLASH-P network as an atlas contribution.

    python Agent/shared/export_atlas.py <NET> [--to-id TO_0000660] [--label "Shoot branching"]

Writes ``<NET>/atlas/``:

    <to_id>.json         the website's atlas format — drop it into
                         FLASHP_WEBSITE/Flash-P-AI/public/trait-networks/atlas/edges/
                         and the existing trait page renders it, no code changes
    fulltext/*.txt       the open-access full texts it references
    rows.jsonl           merge-ready rows for Flash-P_DataBase/atlas.db
    catalog_entry.json   the index fragment for the website's catalog

Only verified and repaired claims are exported; quarantined ones stay local, and the
run reports how many were held back. Requires ``data/evidence.json`` — run
``verify_evidence.py`` first.

If you have a Trait Ontology id for the phenotype, pass ``--to-id``: the atlas keys
traits by TO id, and a contribution with a real one merges instead of arriving as a
new trait nobody can cross-reference.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from provenance.atlas_out import export_atlas       # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export a verified network as an atlas contribution.")
    ap.add_argument("network", nargs="+", help="network directory (containing data/evidence.json)")
    ap.add_argument("--to-id", default="", help="Trait Ontology id, e.g. TO_0000660")
    ap.add_argument("--label", default="", help="human-readable trait label")
    ap.add_argument("--out", default="", help="output directory (default <NET>/atlas)")
    args = ap.parse_args(argv)

    if len(args.network) > 1 and (args.to_id or args.out):
        # A TO id names one trait; silently applying it to several would mislabel them.
        print("--to-id/--out apply to a single network; pass one at a time.", file=sys.stderr)
        return 2

    for net in args.network:
        if not os.path.isdir(net):
            print(f"not a directory: {net}", file=sys.stderr)
            return 2
        r = export_atlas(net, to_id=args.to_id, label=args.label, out_dir=args.out)
        name = os.path.basename(os.path.abspath(net.rstrip("/\\")))
        print(f"\n{name}")
        print(f"  {r['edges']} grouped edges, {r['perturbations']} perturbation tests, "
              f"{r['papers']} papers ({r['fulltexts']} with full text)")
        print(f"  {r['rows']} rows for atlas.db")
        ex = r["excluded"]
        if ex["edges"] or ex["perturbations"]:
            print(f"  held back (unverified, not exported): {ex['edges']} edges, "
                  f"{ex['perturbations']} tests")
        print(f"  -> {r['out_dir']}")
        if not args.to_id:
            print("  note: no --to-id given, so the trait is keyed by its slug. Pass the "
                  "Trait Ontology id if you have one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

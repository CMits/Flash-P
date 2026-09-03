#!/usr/bin/env python3
"""Project an identifier a paper pairs with a node's name, once the mapper has ruled on it.

The gathering step deliberately does not project anchors. Whether a paper actually pairs an
accession with a gene name is reading comprehension, and the character-distance rule that
used to decide it was measurably wrong in both directions: on Stay_Green_In_Sorghum it
accepted "...regulating antenna size (AT3G56290)" as CGA1's identifier while rejecting
"CGA1 (CYTOKININ-RESPONSIVE GATA FACTOR 1; Sobic.010G173300)" over a semicolon.

So the mapper reads `gathered.anchors_for_review`, decides which pairings hold, and calls
this for each one it accepts. The verdict is recorded next to the script's own class so the
two can be compared -- see anchor_agreement.tsv, written by emit_mapping.py.

    python Agent/shared/idmap/project_anchor.py --dossiers <NET>/idmapping/node_dossiers.json \
        --node CGA1 --gene Sobic.010G173300 --from sorghum_bicolor \
        --verdict accept --why "the accession sits inside the parenthetical defining CGA1"

An accession already in the network's own species is recorded directly rather than projected
through orthology, since there is nothing to project.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402
import project_orthologs as proj  # noqa: E402

ANCHOR_WEIGHT = 0.60


def rescore(rec, routes):
    """Re-derive a candidate's score from the routes it still has.

    Best route, plus a third of each *other* route kind's best -- routes of the same kind
    are one line of evidence however many times they fired.
    """
    rec["routes"] = routes
    kinds = {r["route"] for r in routes}
    best = max(r["weight"] for r in routes)
    extra = sum(sorted((max(r["weight"] for r in routes if r["route"] == k) for k in kinds),
                       reverse=True)[1:])
    rec["score"] = round(min(0.98, best + 0.35 * extra), 3)
    rec["route_kinds"] = sorted(kinds)
    rec["n_routes"] = len(kinds)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", required=True)
    ap.add_argument("--node", required=True, help="node_id as it appears in the dossier")
    ap.add_argument("--gene", required=True, help="the identifier the paper pairs with the name")
    ap.add_argument("--from", dest="src", required=True,
                    help="species the identifier belongs to (Ensembl name)")
    ap.add_argument("--verdict", choices=("accept", "reject"), default="accept",
                    help="reject records the judgement without projecting anything")
    ap.add_argument("--why", default="", help="one line: what in the snippet decided it")
    ap.add_argument("--no-plaza", action="store_true")
    a = ap.parse_args()

    # The mapper types these from a snippet, so the spelling is the paper's.
    a.gene = common.canonical_case(a.gene, a.src)

    doss = common.load_json(a.dossiers)
    node = next((n for n in doss["nodes"] if n.get("node_id") == a.node), None)
    if node is None:
        sys.exit(f"no node {a.node!r} in {a.dossiers}")
    g = node.setdefault("gathered", {"candidates": [], "conflicts": []})

    verdicts = g.setdefault("anchor_verdicts", [])
    # Look in the review list first, then among every identifier mined from the papers.
    # The second lookup is what lets a mined identifier below the adjudication threshold be
    # admitted at all: it was never offered as a pairing, so accepting it is a judgement
    # made on the annotation rather than on the sentence, and it still has to be recorded
    # against what the script would have done -- which for these is nothing.
    entry = next((h for h in g.get("anchors_for_review", [])
                  if proj.canon(h.get("gene_id")) == proj.canon(a.gene)), None)
    if entry is None:
        entry = next((h for h in g.get("mined_identifiers", [])
                      if proj.canon(h.get("gene_id")) == proj.canon(a.gene)), {})
        if entry:
            entry = dict(entry, script_proximity=entry.get("proximity"))
    script_class = entry.get("script_proximity")
    verdicts.append({"gene_id": a.gene, "from": a.src, "verdict": a.verdict,
                     "script_proximity": script_class,
                     # Which rule the script applied here. It differs by relation, and the
                     # agreement rate is meaningless if the two are pooled: a foreign anchor
                     # was projected only at `appositive`, a native accession was admitted at
                     # `same_sentence` too.
                     "relation_to_target": entry.get("relation_to_target", "anchor"),
                     "why": a.why})

    # The verdict is written before anything is projected. Previously only the reject branch
    # reached the write, so on a species with no assembly an acceptance raised SystemExit and
    # was lost -- which biased anchor_agreement.tsv toward rejections for exactly the species
    # least able to afford a missing identifier.
    common.write_json(a.dossiers, doss)

    if a.verdict == "reject":
        # A rejection has to be able to take a candidate back out, not merely be noted
        # beside it. An identifier in the network's own species enters the candidate list
        # through `stated_id` without passing through here, so on a node like TAWRKY42 --
        # where "(TaLOX3, TraesCS4B02G295200)" reads as appositive to the node's name -- the
        # accession would otherwise sit at 0.80 with the mapper's rejection recorded
        # alongside it, saying two opposite things at once.
        withdrawn = []
        for rec in list(g.get("candidates", [])):
            if proj.canon(rec["gene_id"]) != proj.canon(a.gene):
                continue
            kept = [r for r in rec.get("routes", [])
                    if r["route"] not in ("stated_id", "anchor_then_project")]
            if len(kept) == len(rec.get("routes", [])):
                continue
            if kept:
                rescore(rec, kept)
                withdrawn.append(f"{rec['gene_id']} kept at {rec['score']:.3f} on "
                                 f"{'+'.join(rec['route_kinds'])}")
            else:
                g["candidates"].remove(rec)
                withdrawn.append(f"{rec['gene_id']} removed (no other route supported it)")
        common.write_json(a.dossiers, doss)
        print(f"recorded: {a.node} rejects {a.gene} (script called it {script_class})")
        for w in withdrawn:
            print(f"   {w}")
        return

    tgt = doss["summary"].get("network_species_ensembl")
    proxy_species = None
    if not tgt:
        # No assembly for this species. An accepted pairing is still a real finding: it is a
        # confirmed identifier in the source species, and it can be carried into whichever
        # relative the run is using as a stand-in. What it must never become is an
        # identifier presented as belonging to the species that has no assembly.
        proxy_species = next((pl.get("proxy_species")
                              for n2 in doss["nodes"]
                              for pl in (n2.get("route_plan") or [])
                              if pl.get("route") == "proxy_ortholog" and pl.get("proxy_species")),
                             None)
        tgt = proxy_species

    added = []
    if tgt and common.same_species(a.src, tgt):
        # The paper names an identifier in the network's own species. Orthology has nothing
        # to do here -- projecting a gene onto its own genome would answer with paralogs.
        added.append({"gene_id": proj.canon(a.gene), "score": 0.80, "relation": "native",
                      "support": ["stated_in_paper"]})
    elif tgt:
        pr = proj.project(a.gene, a.src, tgt, use_plaza=not a.no_plaza)
        for pc in pr.get("candidates", [])[:6]:
            added.append({"gene_id": pc["gene_id"],
                          "score": round(ANCHOR_WEIGHT * (pc["score"] / 0.95), 3),
                          "relation": pc["type"], "support": pc.get("support", [])})
    else:
        print(f"{a.node}: accepted {a.gene} ({a.src}); script called it {script_class}")
        print("   this network's species has no assembly and no proxy species is planned, so "
              "nothing was projected.")
        print("   The pairing is recorded. Report the identifier in source_gene_ids and leave "
              "target_gene_ids empty (judgement rule 3).")
        return

    by_id = {c["gene_id"]: c for c in g.get("candidates", [])}
    for c in added:
        rec = by_id.get(c["gene_id"])
        route = {"route": "anchor_then_project", "weight": c["score"],
                 "source_species": a.src, "source_gene_id": a.gene,
                 "relation": c["relation"], "support": c["support"],
                 "adjudicated": True, "why": a.why}
        if proxy_species:
            route["proxy_species"] = proxy_species
        if rec:
            rescore(rec, rec["routes"] + [route])
        else:
            newrec = {"gene_id": c["gene_id"], "score": c["score"], "routes": [route],
                      "route_kinds": ["anchor_then_project"], "n_routes": 1,
                      "id_system": "ensembl", "description": ""}
            if proxy_species:
                newrec["proxy_species"] = proxy_species
                newrec["score"] = round(newrec["score"] * 0.75, 3)
            g.setdefault("candidates", []).append(newrec)
    g["candidates"] = sorted(g["candidates"], key=lambda r: (-r["score"], r["gene_id"]))
    common.write_json(a.dossiers, doss)

    print(f"{a.node}: accepted {a.gene} ({a.src}); script called it {script_class}")
    if proxy_species:
        print(f"   NOTE: no assembly for this network's species; projected into "
              f"{proxy_species} instead. These are proxy identifiers -- relation must be "
              f"'proxy' and proxy_species must be set.")
    for c in added:
        print(f"   {c['score']:.3f} {c['gene_id']:24s} {c['relation']}")
    if not added:
        print("   no ortholog returned; the pairing is recorded but produced no candidate")
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

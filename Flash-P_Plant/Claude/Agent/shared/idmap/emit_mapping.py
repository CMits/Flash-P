#!/usr/bin/env python3
"""Merge the agent's judgements with the gathered evidence and write the final mapping.

The agent decides; this script records the decision together with everything that supports
it, and enforces the rules that keep the output honest:

  * every identifier emitted must be one that some route actually produced. An identifier
    the agent recalled rather than retrieved is rejected, because it cannot be traced.
  * a node whose candidates came only from a description shortlist cannot be reported as
    resolved. Descriptions identify a gene family, so a shortlist is a shortlist however
    plausible the top entry looks.
  * relation must be stated. Several genes is a real answer for a homoeolog set, a family
    node or a protein complex, and a confession of uncertainty for an ambiguous one; a bare
    list of identifiers does not distinguish those and is not useful downstream.
  * where the name comes from another species, both tiers are written: the identifier in
    the species the name belongs to, and the projected identifier here.

Outputs mapping.tsv (every mappable node), unresolved.tsv, and network.idmapped.json --
a copy of the network with identifiers attached, never an edit of the original.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RELATIONS = {
    "one2one":          "a single gene in this species",
    "homoeolog_set":    "the homoeologous copies of one gene in a polyploid",
    "family_set":       "the node stands for a gene family, and these are its members",
    "complex_members":  "the node is a protein complex, and these are its subunits",
    "proxy":            "no assembly for this species; identifier is in a relative",
    "ambiguous":        "could not be narrowed to one gene; these are the candidates",
    "unresolved":       "no identifier could be supported",
}
CONFIDENCES = ("high", "medium", "low", "none")

COLUMNS = [
    "node_id", "node_type", "network_species", "name_origin_species", "origin_basis",
    "source_gene_ids", "target_gene_ids", "relation", "confidence", "proxy_species",
    "id_system", "routes_agreeing", "n_candidates_considered", "components_expected",
    "components_resolved", "evidence_dois", "n_evidence", "conflicts", "rationale",
]


def load_judgements(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            j = json.loads(line)
            out[j["node_id"]] = j
    return out


def check(node, j, errors):
    """Reject a judgement that the gathered evidence does not support."""
    nid = node["node_id"]
    gathered = node.get("gathered") or {}
    known = {c["gene_id"] for c in gathered.get("candidates", [])}
    rel = j.get("relation")
    ids = j.get("target_gene_ids") or []

    if rel not in RELATIONS:
        errors.append(f"{nid}: relation {rel!r} is not one of {sorted(RELATIONS)}")
    if j.get("confidence") not in CONFIDENCES:
        errors.append(f"{nid}: confidence {j.get('confidence')!r} is not one of {CONFIDENCES}")

    unknown = [i for i in ids if i not in known]
    if unknown:
        errors.append(
            f"{nid}: {unknown} were not produced by any route. Only identifiers present in "
            "the gathered candidates may be emitted -- otherwise the answer cannot be traced.")

    if rel == "one2one" and len(ids) > 1:
        errors.append(f"{nid}: relation one2one but {len(ids)} identifiers given")
    if rel in ("ambiguous",) and len(ids) < 2:
        errors.append(f"{nid}: relation ambiguous needs at least two candidates")
    if rel == "unresolved" and ids:
        errors.append(f"{nid}: relation unresolved but identifiers were given")

    if ids and rel not in ("ambiguous", "unresolved"):
        for i in ids:
            c = next((c for c in gathered.get("candidates", []) if c["gene_id"] == i), None)
            if c and c["route_kinds"] == ["description_shortlist"]:
                errors.append(
                    f"{nid}: {i} came only from a description shortlist. Descriptions "
                    "identify a family, not a gene, so this cannot be reported as resolved "
                    "-- use relation 'ambiguous' or find a corroborating route.")
    if rel == "complex_members":
        parts = (node.get("gathered") or {}).get("components") or []
        if parts and len(ids) < len([p for p in parts if p.get("candidates")]):
            errors.append(
                f"{nid}: relation complex_members but fewer identifiers than subunits that "
                "resolved. List every subunit identifier that was found.")
    if rel == "proxy" and not j.get("proxy_species"):
        errors.append(f"{nid}: relation proxy requires proxy_species")
    # `proxy` describes the namespace an identifier lives in; the other relation values
    # describe cardinality and certainty. They are orthogonal, and a node can need both --
    # a strawberry node whose only candidates are an Arabidopsis family shortlist is a proxy
    # AND ambiguous. Forcing relation to "proxy" in that case was tried and is unsatisfiable:
    # it collides with the shortlist rule, which requires exactly "ambiguous" there.
    # So the safety property is enforced on the column that can always carry it -- an
    # identifier from another organism must be accompanied by the organism's name.
    if ids and rel == "proxy" and not j.get("proxy_species"):
        pass  # already reported above
    if j.get("proxy_species") and not ids and rel == "proxy":
        errors.append(f"{nid}: relation proxy but no identifiers were given")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", required=True)
    ap.add_argument("--judgements", required=True, help="JSON-lines, one object per node")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--allow-partial", action="store_true",
                    help="write output even if some nodes have no judgement")
    args = ap.parse_args()

    doss = common.load_json(args.dossiers)
    js = load_judgements(args.judgements)
    summary = doss["summary"]
    mappable = [n for n in doss["nodes"] if n["mappable"]]

    missing = [n["node_id"] for n in mappable if n["node_id"] not in js]
    if missing and not args.allow_partial:
        sys.exit(f"no judgement for {len(missing)} nodes: {missing[:12]}"
                 f"{' ...' if len(missing) > 12 else ''}\n"
                 "Every mappable node needs a judgement, including the ones that resolve to "
                 "nothing -- record those as relation 'unresolved'. Use --allow-partial to "
                 "write a partial run anyway.")

    errors = []
    for n in mappable:
        if n["node_id"] in js:
            check(n, js[n["node_id"]], errors)
    if errors:
        print("judgements rejected:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(common.EXIT_JUDGEMENT_REJECTED)

    os.makedirs(args.outdir, exist_ok=True)
    rows, unresolved = [], []
    for n in mappable:
        j = js.get(n["node_id"], {"relation": "unresolved", "confidence": "none",
                                  "target_gene_ids": [], "rationale": "not judged"})
        g = n.get("gathered") or {}
        ids = j.get("target_gene_ids") or []
        chosen = [c for c in g.get("candidates", []) if c["gene_id"] in ids]
        routes = sorted({r for c in chosen for r in c["route_kinds"]})

        # A proxy identifier belongs to the relative's namespace, not the network species'.
        ns_species = j.get("proxy_species") or summary.get("network_species_ensembl")
        by_gid = {c.get("gene_id"): c for c in chosen}
        id_systems = set()
        for gid in ids:
            declared = (by_gid.get(gid) or {}).get("id_system")
            if declared and declared != "ensembl":
                id_systems.add(declared)   # a route that knows its own namespace, e.g. PLAZA
            else:
                id_systems.add(common.classify_id_system(gid, ns_species))

        row = {
            "node_id": n["node_id"],
            "node_type": n["node_type"],
            "network_species": n["network_species"],
            "name_origin_species": n.get("name_origin_species") or "",
            "origin_basis": n.get("origin_basis", ""),
            "source_gene_ids": ";".join(j.get("source_gene_ids") or []),
            "target_gene_ids": ";".join(ids),
            "relation": j.get("relation", "unresolved"),
            "confidence": j.get("confidence", "none"),
            "proxy_species": j.get("proxy_species", ""),
            # Almost always Ensembl. It differs when the species has no Ensembl
            # annotation and the identifiers come from PLAZA's own accessions instead --
            # real identifiers, but in a different namespace, and not interchangeable.
            # Classified from the identifier's own shape, not assumed. Routes return
            # Phytozome and older-release accessions for species whose reference is
            # something else -- papers cite retired identifiers for years -- and calling
            # those `ensembl` makes them silently unjoinable.
            "id_system": ";".join(sorted(id_systems)) or "unrecognised",
            "routes_agreeing": ";".join(routes),
            "n_candidates_considered": g.get("n_total_candidates", 0),
            # For a protein complex or a family node, how many of the parts were named and
            # how many got an identifier. A complex whose subunits half resolved is a
            # partial answer, and saying so is more useful than picking a relation that
            # implies it was either complete or a failure.
            "components_expected": len(g.get("components") or g.get("family_members") or []),
            "components_resolved": sum(
                1 for c in (g.get("components") or g.get("family_members") or [])
                if c.get("candidates")),
            "evidence_dois": ";".join(sorted({p["doi"] for p in n.get("papers", []) if p.get("doi")})[:6]),
            "n_evidence": n.get("n_grounded", 0),
            "conflicts": " | ".join(g.get("conflicts", [])),
            "rationale": (j.get("rationale") or "").replace("\t", " ").replace("\n", " "),
        }
        rows.append(row)
        if row["relation"] in ("unresolved", "ambiguous"):
            unresolved.append(row)

    mapping_path = os.path.join(args.outdir, "mapping.tsv")
    with open(mapping_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    with open(os.path.join(args.outdir, "unresolved.tsv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(unresolved)

    # Sidecar network. The original network.json is never modified: Flash-P's schema drops
    # keys it does not know on the next round-trip, so an identifier written into it would
    # be silently lost.
    net_path = common.network_json(summary["network_dir"])
    if not net_path:
        print(f"no network.json under {summary['network_dir']}", file=sys.stderr)
        sys.exit(common.EXIT_NOT_A_NETWORK)
    net = common.load_json(net_path)
    by_id = {r["node_id"]: r for r in rows}
    for node in net.get("nodes", []):
        r = by_id.get(node.get("id"))
        if r and r["target_gene_ids"] and r["relation"] != "unresolved":
            node["gid"] = r["target_gene_ids"].split(";")
            node["gid_relation"] = r["relation"]
            node["gid_confidence"] = r["confidence"]
            if r["source_gene_ids"]:
                node["gid_source"] = {"species": r["name_origin_species"],
                                      "ids": r["source_gene_ids"].split(";")}
            if r["proxy_species"]:
                node["gid_proxy_species"] = r["proxy_species"]

    counts = {}
    for r in rows:
        counts[r["relation"]] = counts.get(r["relation"], 0) + 1
    conf = {}
    for r in rows:
        conf[r["confidence"]] = conf.get(r["confidence"], 0) + 1

    net.setdefault("metadata", {})["idmapping"] = {
        "resolver": "flashp-gene-id-mapper",
        "flash_p_version": common._flashp_version(),
        "network_species": summary["network_species"],
        "network_species_status": summary.get("network_species_status"),
        "n_mappable_nodes": len(mappable),
        "n_with_identifier": sum(1 for r in rows if r["target_gene_ids"]),
        "by_relation": counts,
        "by_confidence": conf,
        "routing_warnings": summary.get("routing_warnings", []),
        "see": "mapping.tsv for the full table, including how each answer was reached",
    }
    sidecar = os.path.join(args.outdir, "network.idmapped.json")

    # Whether a paper pairs an accession with a gene name is now the mapper's call, not the
    # character-distance rule's. Recording both verdicts is what turns "the rule felt wrong
    # on CGA1" into a rate: if the two agree nearly always, the rule was adequate and CGA1
    # was a rare shape; if they diverge, it never was.
    anchor_rows, agree, seen = [], 0, 0
    by_rel = {}                      # relation -> [agreeing, comparable, accepted]
    for n in mappable:
        for v in (n.get("gathered") or {}).get("anchor_verdicts", []):
            script_says = v.get("script_proximity") or "not_offered"
            # What the script would have done unaided, which is not one rule but two. A
            # foreign anchor was projected only when it read as appositive; an accession in
            # the network's own species was admitted as a `stated_id` candidate at
            # same_sentence as well. Scoring both against the appositive threshold would
            # book every accepted same_sentence native as a disagreement and understate the
            # rule by exactly the cases where it was right.
            rel = v.get("relation_to_target", "anchor")
            # A pairing propagated from another paper is scored with the first-hand ones:
            # the typography it rests on is identical, and the extra assumption it makes --
            # that the paper's symbol is this node's gene -- is precisely what the mapper is
            # being asked to rule on, so booking it as "the rule would not have offered it"
            # would credit the rule for a case it never saw.
            would_project = script_says in ("appositive", "appositive_by_symbol") or (
                rel == "native" and script_says == "same_sentence")
            accepted = v["verdict"] == "accept"
            tally = by_rel.setdefault(rel, [0, 0, 0])
            tally[2] += accepted
            if script_says != "not_offered":
                seen += 1
                agree += (would_project == accepted)
                tally[0] += (would_project == accepted)
                tally[1] += 1
            anchor_rows.append({
                "node_id": n["node_id"], "gene_id": v["gene_id"], "source_species": v["from"],
                "relation": rel, "script_proximity": script_says,
                "script_would_project": "yes" if would_project else "no",
                "mapper_verdict": v["verdict"],
                "agreement": "-" if script_says == "not_offered" else
                             ("agree" if would_project == accepted else "disagree"),
                "why": v.get("why", "")})
    if anchor_rows:
        cols = ["node_id", "gene_id", "source_species", "relation", "script_proximity",
                "script_would_project", "mapper_verdict", "agreement", "why"]
        with open(os.path.join(args.outdir, "anchor_agreement.tsv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t")
            w.writeheader()
            w.writerows(anchor_rows)
        net["metadata"]["idmapping"]["anchor_adjudication"] = {
            "n_adjudicated": len(anchor_rows), "n_comparable": seen,
            "n_agreeing": agree,
            "pct_agreeing": round(100.0 * agree / seen, 1) if seen else None,
            "by_relation": {k: {"n_adjudicated": v[1], "n_accepted": v[2],
                                "n_agreeing": v[0],
                                "pct_agreeing": round(100.0 * v[0] / v[1], 1) if v[1] else None}
                            for k, v in sorted(by_rel.items())},
            "see": "anchor_agreement.tsv"}

    # Written once, after the anchor block, so the adjudication summary is always in it.
    common.write_json(sidecar, net)

    out = {"nodes": len(rows), "with_identifier": net["metadata"]["idmapping"]["n_with_identifier"],
           "by_relation": counts, "by_confidence": conf, "outdir": args.outdir}
    if anchor_rows:
        out["anchor_adjudication"] = net["metadata"]["idmapping"]["anchor_adjudication"]
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

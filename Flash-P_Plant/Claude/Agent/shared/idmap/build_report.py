#!/usr/bin/env python3
"""Write the human-readable report for one mapping run, or a roll-up across many.

The report is meant to be read rather than skimmed for a number: what was attempted, what
each route bought, and what remains unresolved and why. It carries a glossary, the working
hypotheses behind the method with whatever the run says about them, and an FAQ that
accumulates across runs.
"""
import argparse
import csv
import datetime
import html
import json
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

GLOSSARY = [
    ("gene node", "A node in the Flash-P network that stands for a gene, a protein complex or a regulatory RNA. Metabolites, hormones, processes, environmental inputs and the phenotype itself are not mapped."),
    ("gene model identifier", "The stable accession a genome annotation gives a gene, such as SORBI_3001G068301 or Solyc03g031860. Unlike a gene symbol it is unambiguous within one annotation release."),
    ("name origin species", "The organism whose literature the node's name comes from. Flash-P records, for every supporting sentence, the species of the study it came from; where all of them are one species other than the network's own, the name has been borrowed."),
    ("origin basis", "How the name origin was decided: <em>evidence_native</em> (papers in the network's own species), <em>evidence_exclusive_foreign</em> (every paper in another species), <em>evidence_mixed</em>, <em>no_evidence</em> (no grounded supporting sentence), <em>no_species_evidence</em> (sentences exist but carry no usable species label)."),
    ("route", "One way of getting from a name to an identifier. The routes are independent of each other, which is what makes agreement between them meaningful."),
    ("ortholog projection", "Taking an identifier in one species and asking a comparative-genomics resource for the corresponding gene in another. Its reliability depends almost entirely on the relationship reported: one-to-one is right about 95% of the time, many-to-many about 37%."),
    ("reciprocal best hit", "Projecting a candidate back to the source species and finding the original gene returned first. A much stronger statement than a one-way match."),
    ("description shortlist", "Genes whose functional annotation matches the node's described function. These annotations are protein-family assignments, so they narrow to a family and never to a single gene; a shortlist corroborates other routes but is never an answer by itself."),
    ("relation", "What multiple identifiers mean for a node: <em>one2one</em>, <em>homoeolog_set</em> (the copies of one gene in a polyploid), <em>family_set</em> (the node stands for a family), <em>complex_members</em> (subunits of a complex), <em>proxy</em> (no assembly for this species, so the identifier is in a relative), <em>ambiguous</em> (could not be narrowed), <em>unresolved</em>."),
    ("proxy species", "A relative used to anchor a node when the network's own species has no reference annotation. A proxy identifier is not a gene in the network's species and must not be treated as one."),
    ("cache coverage", "How many genes in a species carry a gene symbol in the offline name cache. This varies enormously — about 1% in sorghum, 37% in Arabidopsis — and it sets a ceiling on what direct name lookup can ever achieve."),
]

HYPOTHESES = [
    ("H1", "Most node names that fail to resolve do so because they are not native to the network's species.",
     "Testable from origin_basis: if the foreign-origin share is large and native lookup succeeds mainly on native-origin nodes, the hypothesis holds."),
    ("H2", "The evidence Flash-P already collected is enough to decide where a name comes from, without any further literature search.",
     "Supported if most nodes carry a usable species label; refuted if no_species_evidence is common."),
    ("H3", "Identifiers stated outright in the cited papers are a small but high-precision route.",
     "Measured by how often a stated identifier agrees with an independent route, and how often it conflicts."),
    ("H4", "Functional descriptions cannot identify a gene alone, but break ties between ortholog candidates.",
     "Supported where description_agrees changes which candidate ranks first among otherwise equal projections."),
    ("H5", "Agreement between independent routes is a better confidence signal than any single route's own score.",
     "Requires the curated gold set to test properly; until then the report only records how often routes agree."),
]

FAQ = [
    ("Why not just write the identifiers into network.json?",
     "Flash-P's schema drops keys it does not recognise when the file is next read and written, so an identifier added there would be silently lost. The mapping is written alongside as network.idmapped.json instead, and the original is never modified."),
    ("A node resolved to several identifiers. Is that a failure?",
     "Not necessarily. Read the relation column. A homoeolog set in a polyploid, a node standing for a gene family, or the subunits of a protein complex are all genuinely several genes. Only <em>ambiguous</em> means the answer was not determined."),
    ("Why is coverage lower for some species than others?",
     "Mostly because of how many genes carry a symbol in the reference annotation. Sorghum has symbols for about 1% of its genes and Arabidopsis for 37%, so the same method yields very different numbers. Coverage figures are only comparable between species when read next to that."),
    ("A cited paper gives an accession that disagrees with the cache. Which is right?",
     "Neither automatically. Papers sometimes print an accession from an older annotation release, and occasionally simply the wrong one. The run records both and flags the conflict rather than choosing silently."),
    ("What does a proxy identifier mean?",
     "That the network's species has no reference annotation available, so the identifier given belongs to a relative. It anchors the node to a real gene model but is not a gene in the network's species, and should not be used as though it were orderable in that crop."),
]

CSS = """
:root{--fg:#1a1a1a;--bg:#fff;--muted:#666;--line:#ddd;--accent:#2a5d8f;--warn:#8a4b00;--warnbg:#fff6e8}
*{box-sizing:border-box}
body{margin:0;padding:2rem;font:16px/1.65 Georgia,'Times New Roman',serif;color:var(--fg);background:var(--bg);max-width:62rem;margin-inline:auto}
h1{font-size:1.9rem;margin:0 0 .2rem;line-height:1.25}
h2{font-size:1.3rem;margin:2.4rem 0 .6rem;padding-bottom:.25rem;border-bottom:2px solid var(--line)}
h3{font-size:1.05rem;margin:1.6rem 0 .4rem;color:var(--accent)}
.sub{color:var(--muted);margin:0 0 1.5rem;font-style:italic}
table{border-collapse:collapse;width:100%;margin:.6rem 0 1.2rem;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
caption{text-align:left;font:italic 14px/1.5 Georgia,serif;color:var(--muted);padding:.3rem 0 .5rem}
th,td{border:1px solid var(--line);padding:.4rem .55rem;text-align:left;vertical-align:top}
th{background:#f4f6f8;font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
code,.mono{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
.warn{background:var(--warnbg);border-left:4px solid var(--warn);padding:.7rem .9rem;margin:.8rem 0;color:var(--warn)}
dl dt{font-weight:600;margin-top:.7rem}
dl dd{margin:.15rem 0 0 1.2rem;color:#333}
.faq q{display:block;font-weight:600;margin-top:1rem}
.faq q:before{content:'Q. '}
.faq p{margin:.2rem 0 0}
.small{font-size:13px;color:var(--muted)}
tr.lowconf td{background:#fcf7f7}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def table(caption, headers, rows, numeric=()):
    h = "".join(f'<th class="{"num" if i in numeric else ""}">{esc(x)}</th>'
                for i, x in enumerate(headers))
    body = []
    for r in rows:
        cls = ' class="lowconf"' if (len(r) > 0 and "low" == str(r[-1])) else ""
        cells = "".join(f'<td class="{"num" if i in numeric else ""}">{c if str(c).startswith("<") else esc(c)}</td>'
                        for i, c in enumerate(r))
        body.append(f"<tr{cls}>{cells}</tr>")
    return (f'<table><caption>{esc(caption)}</caption><thead><tr>{h}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def read_tsv(path):
    with open(path) as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def network_report(outdir):
    doss = common.load_json(os.path.join(outdir, "node_dossiers.json"))
    rows = read_tsv(os.path.join(outdir, "mapping.tsv"))
    s = doss["summary"]
    prof = s.get("target_species_profile", {}) or {}

    n = len(rows)
    with_id = [r for r in rows if r["target_gene_ids"]]
    rel = collections.Counter(r["relation"] for r in rows)
    conf = collections.Counter(r["confidence"] for r in rows)
    basis = collections.Counter(r["origin_basis"] for r in rows)
    routes = collections.Counter()
    for r in with_id:
        for k in filter(None, r["routes_agreeing"].split(";")):
            routes[k] += 1
    n_multi_route = sum(1 for r in with_id if len(set(filter(None, r["routes_agreeing"].split(";")))) > 1)

    parts = [f"<h1>Gene identifiers for the {esc(s.get('phenotype') or s['network'])} network</h1>"]
    parts.append(f'<p class="sub">{esc(s["network"])} · {esc(s["network_species"])} · '
                 f'mapped {datetime.date.today().isoformat()}</p>')

    for w in s.get("routing_warnings", []):
        parts.append(f'<div class="warn"><strong>Note on this species.</strong> {esc(w)}</div>')

    parts.append("<h2>What was attempted</h2>")
    parts.append(f"""<p>The network has {s['n_nodes']} nodes, of which {s['n_mappable']} stand for
      genes, protein complexes or regulatory RNAs and were therefore candidates for mapping.
      Flash-P had collected grounded literature evidence for {s['n_mappable_with_evidence']} of them,
      drawn from {s.get('n_papers', 0)} papers. The remaining node types — metabolites, hormones,
      processes, environmental inputs and the phenotype — are not genes and were deliberately
      not mapped.</p>""")

    parts.append(table(
        "Table 1. Reference annotation available for this species, which sets the ceiling on direct name lookup",
        ["Property", "Value"],
        [["Species as declared by Flash-P", s["network_species"]],
         ["Reference annotation", prof.get("ensembl_name") or "none available"],
         ["Assembly", prof.get("assembly") or "—"],
         ["Genes carrying a symbol in the name cache", f"{prof.get('n_names', 0):,}"],
         ["Symbols usable for name lookup", "yes" if prof.get("usable_for_name_lookup") else "no"],
         ["Annotation name field holds", prof.get("name_class") or "—"]]))

    parts.append("<h2>Where the node names come from</h2>")
    parts.append("""<p>Flash-P records the species of the study behind every supporting sentence.
      Grouping those by node shows which names belong to this crop and which have been borrowed
      from another organism's literature — a distinction that decides how each name has to be
      resolved, because a borrowed name will not be found in this species' own annotation.</p>""")
    parts.append(table(
        "Table 2. Gene nodes by the species their supporting literature comes from",
        ["Origin of the name", "Nodes", "Share"],
        [[k, v, f"{100*v/max(1,n):.0f}%"] for k, v in basis.most_common()],
        numeric=(1, 2)))

    parts.append("<h2>What was resolved</h2>")
    parts.append(f"""<p>{len(with_id)} of {n} gene nodes ({100*len(with_id)/max(1,n):.0f}%) were
      given at least one identifier. Of those, {n_multi_route} were supported by more than one
      independent route, which is the strongest evidence available short of a curated reference.</p>""")
    parts.append(table(
        "Table 3. Outcome for each gene node, by what multiple identifiers mean",
        ["Relation", "Meaning", "Nodes"],
        [[k, {"one2one": "a single gene", "homoeolog_set": "copies of one gene in a polyploid",
              "family_set": "the node stands for a gene family",
              "complex_members": "subunits of a protein complex",
              "proxy": "identifier is in a relative, not this species",
              "ambiguous": "could not be narrowed to one gene",
              "unresolved": "no identifier could be supported"}.get(k, ""), v]
         for k, v in rel.most_common()],
        numeric=(2,)))
    parts.append(table(
        "Table 4. Confidence in the identifiers given",
        ["Confidence", "Nodes", "Share of all gene nodes"],
        [[k, v, f"{100*v/max(1,n):.0f}%"] for k, v in conf.most_common()],
        numeric=(1, 2)))
    if routes:
        parts.append(table(
            "Table 5. How the resolved nodes were reached (a node may appear under more than one route)",
            ["Route", "Nodes"], [[k, v] for k, v in routes.most_common()], numeric=(1,)))

    conflicts = [r for r in rows if r["conflicts"]]
    if conflicts:
        parts.append("<h2>Disagreements between sources</h2>")
        parts.append("<p>These nodes had routes that returned different identifiers. They are "
                     "recorded rather than resolved silently, because either source can be the "
                     "wrong one.</p>")
        parts.append(table("Table 6. Nodes where independent sources disagreed",
                           ["Node", "Identifier given", "Disagreement"],
                           [[r["node_id"], r["target_gene_ids"] or "—", r["conflicts"]]
                            for r in conflicts[:25]]))

    parts.append("<h2>Every gene node</h2>")
    parts.append(table(
        "Table 7. Full result for each gene node, with the reasoning behind it",
        ["Node", "Name origin", "Identifier in origin species", "Identifier here",
         "Relation", "Routes agreeing", "Reasoning", "Confidence"],
        [[r["node_id"],
          (r["name_origin_species"] or "—"),
          f'<span class="mono">{esc(r["source_gene_ids"] or "—")}</span>',
          f'<span class="mono">{esc(r["target_gene_ids"] or "—")}</span>',
          r["relation"], r["routes_agreeing"].replace(";", ", ") or "—",
          r["rationale"] or "—", r["confidence"]] for r in rows]))

    parts.append("<h2>Hypotheses behind the method</h2>")
    parts.append("<p>These are the assumptions the approach rests on, recorded so that they can "
                 "be argued with and eventually tested against a curated reference set.</p>")
    obs = {
        "H1": f"{basis.get('evidence_exclusive_foreign',0)} of {n} nodes here have foreign-only evidence.",
        "H2": f"{basis.get('no_species_evidence',0)} nodes had sentences but no usable species label; "
              f"{basis.get('no_evidence',0)} had no grounded sentence at all.",
        "H3": f"{routes.get('stated_id',0)} resolved nodes used an identifier stated in a paper; "
              f"{len(conflicts)} nodes showed a conflict between a stated identifier and another route.",
        "H4": f"{routes.get('description_agrees',0)} resolved nodes had a description that corroborated the candidate.",
        "H5": f"{n_multi_route} of {len(with_id)} resolved nodes were supported by more than one route.",
    }
    parts.append(table("Table 8. Working hypotheses and what this run says about them",
                       ["", "Hypothesis", "How it would be tested", "This run"],
                       [[h, t, howto, obs.get(h, "")] for h, t, howto in HYPOTHESES]))

    parts.append("<h2>Glossary</h2><dl>")
    for term, definition in GLOSSARY:
        parts.append(f"<dt>{esc(term)}</dt><dd>{definition}</dd>")
    parts.append("</dl>")

    parts.append('<h2>Questions and answers</h2><div class="faq">')
    for q, a in FAQ:
        parts.append(f"<q>{esc(q)}</q><p>{a}</p>")
    parts.append("</div>")

    parts.append(f'<p class="small">Generated from node_dossiers.json and mapping.tsv in '
                 f'{esc(outdir)}. Source network: {esc(s["network_dir"])}.</p>')

    return (f"<title>Gene identifiers · {esc(s['network'])}</title><style>{CSS}</style>"
            + "\n".join(parts))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", required=True, help="a network's idmapping directory, e.g. networks/<Trait>/idmapping")
    ap.add_argument("--out", help="output html path (default: <outdir>/report.html)")
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(args.outdir, "node_dossiers.json")):
        print(f"no node_dossiers.json in {args.outdir} -- run prepare.py first",
              file=sys.stderr)
        return common.EXIT_ERROR
    doc = network_report(args.outdir)
    out = args.out or os.path.join(args.outdir, "report.html")
    with open(out, "w") as fh:
        fh.write(doc)
    print(out)
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

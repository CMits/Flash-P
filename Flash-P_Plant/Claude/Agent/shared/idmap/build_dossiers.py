#!/usr/bin/env python3
"""Build a per-node evidence dossier for one Flash-P network.

Flash-P's Step 1.6 writes data/evidence.json: for every edge and perturbation test it
records the DOI that actually supports the claim, the verbatim supporting sentence, and
-- critically for identifier mapping -- the species of the study the sentence came from.

That species field is why this script exists. A sorghum network node called PIF4 is an
Arabidopsis symbol borrowed into a sorghum network; resolving it against the sorghum name
cache is guaranteed to fail. Grouping the evidence by node tells us which species each
name actually belongs to, so the caller can resolve it where it is known and project the
ortholog afterwards.

Reads:  <network>/network/network.json, <network>/data/evidence.json
Writes: node_dossiers.json  (one record per node, mappable ones flagged)
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# Node type vocabularies. Flash-P emits short codes in newer networks and long words in
# older ones, and both appear in the current corpus, so every consumer must accept both.
GENE_TYPES = {"G", "GENE"}
COMPLEX_TYPES = {"PC", "PROTEIN_COMPLEX"}
RNA_TYPES = {"R", "REGULATORY_RNA"}
MAPPABLE_TYPES = GENE_TYPES | COMPLEX_TYPES | RNA_TYPES

TYPE_LABEL = {
    "G": "gene", "GENE": "gene",
    "PC": "protein_complex", "PROTEIN_COMPLEX": "protein_complex",
    "R": "regulatory_rna", "REGULATORY_RNA": "regulatory_rna",
    "M": "metabolite", "METABOLITE": "metabolite",
    "H": "hormone", "HORMONE": "hormone",
    "E": "environment", "ENVIRONMENT": "environment",
    "PR": "process", "PROCESS": "process",
    "P": "phenotype", "PHENOTYPE": "phenotype",
}

TAG_RE = re.compile(r"<[^>]{1,40}>")


def clean(text):
    """Strip the HTML fragments Europe PMC leaves in abstracts (<i>, <sub>, <h4>...)."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", TAG_RE.sub("", text)).strip()


def join_key(name):
    """Normalise a node name for joining evidence to nodes.

    network.json uppercases node ids (ZmEPF2 -> ZMEPF2) while evidence.json preserves the
    spelling used in the paper, so the two cannot be joined on the raw string.
    """
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def classify_origin(evidence_species, network_species):
    """Decide which species a node's name actually comes from.

    Returns (origin_species, basis). The basis matters as much as the answer: a name
    supported only by foreign-species papers needs a different resolution route from one
    with native support, and a name with no grounded evidence at all needs a third.

    Species labels Flash-P produced by mis-detection ("Arabidopsis inflorescence",
    "Sorghum stay") are excluded here rather than earlier, so that a node whose only
    labels are artefacts is reported as having no usable species evidence instead of
    being routed to a species that does not exist.
    """
    usable = {
        s: n for s, n in evidence_species.items()
        if common.species_weight(common.classify_species(s)["status"]) > 0
    }
    if not usable:
        return None, "no_species_evidence" if evidence_species else "no_evidence"

    native_key = None
    for s in usable:
        if s == network_species or (
            common.classify_species(s)["binomial"]
            == common.classify_species(network_species)["binomial"]
        ):
            native_key = s
            break

    native = usable.get(native_key, 0) if native_key else 0
    foreign = {s: n for s, n in usable.items() if s != native_key}
    if native and not foreign:
        return network_species, "evidence_native"
    if foreign and not native:
        # The whole point of this script: every paper behind this node studied another
        # organism, so the name is borrowed and must be resolved there first.
        top = max(foreign.items(), key=lambda kv: kv[1])[0]
        return top, "evidence_exclusive_foreign"
    top = max(usable.items(), key=lambda kv: kv[1])[0]
    return top, "evidence_mixed"


def build(network_dir, allow_no_evidence=False):
    net_path = common.network_json(network_dir)
    ev_path = os.path.join(network_dir, "data", "evidence.json")
    if not net_path:
        print(f"no network.json under {network_dir} -- not a Flash-P network directory",
              file=sys.stderr)
        sys.exit(common.EXIT_NOT_A_NETWORK)
    if not os.path.exists(ev_path) and not allow_no_evidence:
        print(
            f"no data/evidence.json under {network_dir}\n"
            "This network predates Flash-P Step 1.6. Evidence-free networks are out of scope.\n"
            "Pass --allow-no-evidence to map it anyway from network.json alone. Every route\n"
            "that reads the literature is then inert: no identifiers are mined from papers,\n"
            "there is nothing to adjudicate, and the species a name comes from is unknown, so\n"
            "a borrowed symbol can only be found by probing the well-annotated species.",
            file=sys.stderr)
        sys.exit(common.EXIT_NO_EVIDENCE)

    net = load_json(net_path)
    # An evidence-free network still has node names, node types and Flash-P's own one-line
    # function text, which is what the cache, projection and description routes actually
    # consume. What is lost is every judgement that rests on what a paper said.
    ev = load_json(ev_path) if os.path.exists(ev_path) else {"papers": {}, "metadata": {}}
    network_species = net.get("metadata", {}).get("species") or ev.get("metadata", {}).get("species")

    papers = ev.get("papers", {})

    # Accumulators keyed by the normalised node name.
    sentences = collections.defaultdict(list)
    ev_species = collections.defaultdict(collections.Counter)
    spellings = collections.defaultdict(collections.Counter)
    quarantined = collections.Counter()
    claimed = collections.Counter()

    def absorb(names, rec, kind, detail):
        """Attach one evidence record to every node it mentions."""
        # "backfilled" is a reconstructed sentence from backfill_evidence.py, for networks
        # built before Flash-P wrote evidence.json. It is grounded -- there is a real
        # sentence from a real paper behind it -- but it was chosen because it names the
        # genes, not because a judge checked the claim against it, and it carries a lower
        # confidence to say so. Kept as its own label rather than folded into "verified" so
        # the distinction survives into the dossier.
        grounded = (rec.get("verification") in ("verified", "repaired", "backfilled")
                    and rec.get("evidence"))
        for raw in names:
            if not raw:
                continue
            key = join_key(raw)
            claimed[key] += 1
            spellings[key][raw] += 1
            if not grounded:
                quarantined[key] += 1
                continue
            species = rec.get("species") or ""
            if species:
                ev_species[key][species] += 1
            sentences[key].append({
                "kind": kind,
                "detail": detail,
                "doi": rec.get("doi", ""),
                "text": clean(rec.get("evidence", "")),
                "locator": rec.get("source_locator", ""),
                "species": species,
                "species_source": rec.get("species_source", ""),
                "confidence": rec.get("confidence", 0.0),
                "verification": rec.get("verification", ""),
            })

    for rec in ev.get("edges", []):
        sign = "activates" if rec.get("x", 1) == 1 else "inhibits"
        absorb([rec.get("s"), rec.get("t")], rec, "edge",
               f"{rec.get('s')} {sign} {rec.get('t')}")
    for rec in ev.get("perturbations", []):
        absorb([rec.get("g")], rec, "perturbation",
               f"{rec.get('pt')} -> {rec.get('ed')}")

    dossiers = []
    for node in net.get("nodes", []):
        ty = node.get("ty", "")
        key = join_key(node.get("id"))
        spec = ev_species.get(key, collections.Counter())
        origin, basis = classify_origin(spec, network_species)
        origin_info = common.classify_species(origin) if origin else {}
        artefacts = sorted(
            s for s in spec
            if common.species_weight(common.classify_species(s)["status"]) == 0
        )

        # Sentences are the agent's raw material; put the best-supported first and cap the
        # volume so a single node cannot flood the context.
        sents = sorted(sentences.get(key, []), key=lambda s: -s["confidence"])

        cited = []
        seen = set()
        for s in sents:
            doi = s["doi"]
            if doi and doi not in seen:
                seen.add(doi)
                p = papers.get(doi, {})
                cited.append({
                    "doi": doi,
                    "title": p.get("title", ""),
                    "year": p.get("year", ""),
                    "has_fulltext": p.get("has_fulltext", False),
                    "fulltext_file": p.get("fulltext_file", ""),
                })

        dossiers.append({
            "node_id": node.get("id"),
            "ty": ty,
            "node_type": TYPE_LABEL.get(ty, ty.lower()),
            "mappable": ty in MAPPABLE_TYPES,
            "fn": node.get("fn", ""),
            "is_source_node": bool(node.get("src")),
            "network_species": network_species,
            # Original spellings from the literature. network.json uppercases node ids, so
            # this is the only place the species prefix casing (FaPG1, ZmEPF2) survives --
            # and that casing is a strong hint about which organism the name belongs to.
            "literature_spellings": [s for s, _ in spellings.get(key, collections.Counter()).most_common()],
            "evidence_species": dict(spec),
            "name_origin_species": origin,
            "name_origin_ensembl": origin_info.get("ensembl_name"),
            "name_origin_status": origin_info.get("status"),
            "name_origin_taxid": origin_info.get("taxid"),
            "origin_basis": basis,
            "discarded_species_labels": artefacts,
            "n_claims": claimed.get(key, 0),
            "n_grounded": len(sents),
            "n_quarantined": quarantined.get(key, 0),
            "sentences": sents[:12],
            "papers": cited[:12],
            "ids_in_text": [],   # filled in by mine_evidence_ids.py
        })

    net_sp = common.classify_species(network_species)
    summary = {
        "network": os.path.basename(os.path.abspath(network_dir)),
        "network_dir": os.path.abspath(network_dir),
        "network_species": network_species,
        "network_species_status": net_sp.get("status"),
        "network_species_ensembl": net_sp.get("ensembl_name"),
        "network_species_taxid": net_sp.get("taxid"),
        "network_species_n_names": net_sp.get("n_names", 0),
        "phenotype": net.get("metadata", {}).get("phenotype", ""),
        "flash_p_version": net.get("metadata", {}).get("flash_p_version", ""),
        "evidence_version": ev.get("metadata", {}).get("flash_p_version", ""),
        "n_nodes": len(dossiers),
        "n_mappable": sum(1 for d in dossiers if d["mappable"]),
        "n_mappable_with_evidence": sum(1 for d in dossiers if d["mappable"] and d["n_grounded"]),
        "origin_basis_counts": dict(collections.Counter(
            d["origin_basis"] for d in dossiers if d["mappable"])),
        "n_papers": len(papers),
    }
    return {"summary": summary, "nodes": dossiers}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", required=True, help="path to a Flash-P network directory")
    ap.add_argument("--out", help="output JSON path (default: stdout)")
    ap.add_argument("--summary-only", action="store_true", help="print the summary block only")
    ap.add_argument("--allow-no-evidence", action="store_true",
                    help="map a pre-Step-1.6 network from network.json alone; the "
                         "literature-derived routes produce nothing and confidence is "
                         "correspondingly lower")
    args = ap.parse_args()

    result = build(args.network, allow_no_evidence=args.allow_no_evidence)
    if args.summary_only:
        print(json.dumps(result["summary"], indent=2))
        return
    if args.out:
        common.write_json(args.out, result)
        print(f"wrote {args.out}: {result['summary']['n_mappable']} mappable nodes "
              f"of {result['summary']['n_nodes']}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()

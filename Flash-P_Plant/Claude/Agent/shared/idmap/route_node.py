#!/usr/bin/env python3
"""Decide, per node, which resolution routes are worth trying and in what order.

Routing is driven by measured cache coverage rather than by any list of species, because
this ships with Flash-P and will be run on species we have never seen. The manifest records
how many genes in each species actually carry a name; that number, not the species'
identity, decides whether a native lookup is worth attempting.

Three facts from the manifest change the plan for a species:
  n_names               how many name-to-identifier pairs exist at all
  gff3_name_class       'identifier_alias' means the Name= field holds a second identifier
                        system rather than gene symbols, so cache hits are not name hits
  status == 'outside'   no Ensembl assembly; the answer can only be a flagged proxy

Reads:  node_dossiers.json (after mine_evidence_ids.py)
Writes: the same structure with a route_plan on every mappable node
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# Below this many named genes a native name lookup is a long shot and should not be the
# first thing tried. Sorghum sits at 475 and answers ~1% of its nodes; Arabidopsis at
# 55,876 answers most of them.
SPARSE_NAME_CACHE = 3000

STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "to", "for", "with", "protein", "gene",
    "family", "putative", "probable", "like", "related", "domain", "containing", "type",
    "subunit", "isoform", "homolog", "homologue", "factor", "gene-", "enzyme",
}


def description_terms(node, limit=10, for_filtering=False):
    """Functional words worth searching an annotation description for.

    Compound terms are split as well as kept whole: "4-COUMARATE:CoA LIGASE" has to yield
    "coumarate", because that is the word the annotation uses and it is rare enough to be
    decisive, whereas "ligase" alone matches hundreds of genes.

    The species' own name is dropped. A gloss reading "CAFFEIC ACID O-METHYLTRANSFERASE
    (sorghum bmr12)" would otherwise search for "sorghum" in a sorghum annotation.
    """
    terms = []
    species_words = {w.lower() for w in re.split(r"[\s_]+", node.get("network_species") or "")}

    def add(word, min_len=4):
        w = word.strip(" ,.;:()[]-").lower()
        if (len(w) >= min_len and w not in STOPWORDS and w not in species_words
                and w not in terms and not w.isdigit()):
            terms.append(w)

    def add_compound(token, min_len=4):
        add(token, min_len)
        for part in re.split(r"[^A-Za-z0-9]+", token):
            add(part, min_len)

    # The symbol itself is worth searching for: annotation descriptions often contain it
    # verbatim ("ABC TRANSPORTER G FAMILY MEMBER 29" for ABCG29).
    #
    # Both spellings, with and without the species prefix. Annotations name the gene, not
    # the organism: rice Ghd7 is "transcription factor GHD7-like" and Arabidopsis APETALA1
    # is "...(AP1)", neither of which contains the sbghd7 or sbap1 that a sorghum node is
    # called. Searching only the prefixed form made an annotation fail to corroborate its
    # own gene, which cost SbGHD7 its second route and made the anchor-plausibility test
    # call the correct rice source contradicted.
    #
    # Three characters is enough for a symbol, where four is right for a gloss word. AP1,
    # ID1 and SPL are gene names; they are specific strings, not the vocabulary noise the
    # floor exists to keep out. Two is still too short -- CO and FD would match the front of
    # "cofactor" and "FD-like" and corroborate anything.
    for n in ([node.get("node_id")] + list(node.get("literature_spellings") or []))[:3]:
        if n:
            add_compound(n, min_len=3)
            bare = common.strip_species_prefix(n)
            if bare:
                add_compound(bare, min_len=3)
    for w in re.split(r"\s+", node.get("fn", "")):
        add_compound(w)
    n_from_function = len(terms)
    for s in node.get("sentences", [])[:4]:
        # Expanded names in papers are usually written out in full capitals or title case.
        for phrase in re.findall(r"\b[A-Z][A-Za-z-]{3,}(?:\s+[a-z-]{3,}){0,3}", s.get("text", "")):
            for w in phrase.split():
                add_compound(w)
    if for_filtering:
        # Sentence-derived terms are fine for retrieval, where a wrong term costs a little
        # precision, and dangerous for filtering, where it would discard a right answer.
        # They pick up author surnames ("borrell") and boilerplate ("arabidopsis",
        # "thaliana") along with real function words. Only the symbol and the network's own
        # gloss of the node are clean enough to reject a candidate on.
        return terms[:n_from_function]
    return terms[:limit]


def species_profile(token):
    info = common.classify_species(token)
    ens = info.get("ensembl_name")
    prof = {
        "token": token,
        "status": info.get("status"),
        "ensembl_name": ens,
        "taxid": info.get("taxid"),
        "n_names": info.get("n_names", 0) or 0,
        "name_class": None,
        "names_are_symbols": True,
        "usable_for_name_lookup": False,
    }
    if ens:
        sr = common._resolver()
        meta = sr._manifest().get(ens, {})
        prof["assembly"] = meta.get("assembly")
        prof["name_class"] = meta.get("gff3_name_class")
        # A cache whose Name= field carries a second identifier system looks well populated
        # while holding no gene symbols at all -- Brassica napus reports 102,047 names on
        # this basis. Treating those as symbols produces confident nonsense.
        prof["names_are_symbols"] = meta.get("gff3_name_class") != "identifier_alias"
        prof["usable_for_name_lookup"] = prof["n_names"] > 0 and prof["names_are_symbols"]
        prof["sparse"] = prof["n_names"] < SPARSE_NAME_CACHE
    return prof


def choose_proxy(target, anchors, cache_profiles):
    """A relative to anchor a species that has no reference annotation of its own.

    A congener first. Wild barley's proxy is cultivated barley and Nicotiana benthamiana's
    is Nicotiana attenuata, which are the answers a reader would expect and are far closer
    than anything the evidence-derived anchors offer. Where the genus has no sequenced
    member -- Macadamia, Mangifera, the cultivated strawberry -- fall back to the best
    anchor this network already draws on.

    Whatever is chosen, identifiers from it are not genes in the network's species and are
    reported as proxies throughout.
    """
    sr = common._resolver()
    genus = (target.get("token") or "").split()[0].lower()
    congeners = []
    for d in sr._species_dirs():
        if not d.startswith(genus + "_"):
            continue
        prof = cache_profiles.get(d) or species_profile(d)
        cache_profiles[d] = prof
        if prof.get("usable_for_name_lookup"):
            congeners.append((prof.get("n_names", 0), d))
    if congeners:
        congeners.sort(reverse=True)
        return {"species": congeners[0][1], "basis": "same genus"}
    if anchors:
        return {"species": anchors[0],
                "basis": "no sequenced relative in this genus; using the best-annotated "
                         "species this network's evidence draws on"}
    return None


def _binomial(name):
    """Genus and species, dropping any assembly, cultivar or accession suffix.

    `common.same_species` only knows the `_gca` form, and widening it would change what
    every other caller means by "the same species". Here the question is narrower and
    purely about not wasting a probe slot: triticum_aestivum, triticum_aestivum_refseqv2 and
    triticum_aestivum_paragon are one organism to ask about a gene symbol, however many
    assemblies of it Ensembl carries.
    """
    return "_".join(re.split(r"[\s_]+", (name or "").lower().strip())[:2])


def _related_candidates(target_key, pool_min_names=SPARSE_NAME_CACHE):
    """Species with enough gene symbols to be worth probing, closest relative first.

    The pool is taken from the manifest by symbol count, so it is whatever the cache
    actually holds rather than a list of model organisms written down here. Ordering is by
    shared NCBI lineage with the target.

    `target_key` may be an Ensembl species name or a plain binomial. A species with no
    assembly still has a lineage, and it is exactly the case that needs this most: Macadamia,
    Mango and the cultivated strawberry used to return no panel at all and fall through to
    Arabidopsis, when strawberry has Rosaceae relatives in the cache and mango has Sapindales
    ones. Nothing about the ranking requires the target to be sequenced.
    """
    if not target_key:
        return []
    try:
        manifest = common._resolver()._manifest()
    except Exception:
        return []
    pool = []
    seen_species = []
    ordered = sorted(manifest.items(),
                     key=lambda kv: -int((kv[1].get("n_names") or 0)
                                         if str(kv[1].get("n_names") or 0).isdigit() else 0))
    for ens, meta in ordered:
        # Skip the target and any other assembly of it. A second wheat assembly is not a
        # second opinion about a wheat symbol, and it would spend one of four probe slots
        # re-asking the species that already failed to answer.
        if ens == target_key or _binomial(ens) == _binomial(target_key):
            continue
        try:
            n = int(meta.get("n_names") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < pool_min_names:
            continue
        # One assembly per species, the best-annotated. Pangenome accessions otherwise fill
        # the panel with a dozen spellings of the same organism.
        if _binomial(ens) in seen_species:
            continue
        seen_species.append(_binomial(ens))
        pool.append(ens)
    if not pool:
        return []
    try:
        common.ensure_lineages([target_key] + pool + ["arabidopsis_thaliana"])
        ranked = sorted(pool, key=lambda e: -common.relatedness(target_key, e))
    except Exception:
        return []
    # Anything no closer to the target than Arabidopsis is dropped. Arabidopsis is already
    # appended as the last resort and carries more gene symbols than almost anything else,
    # so a species that is equally distant and less annotated can only take a probe slot
    # away from it -- which is how cork oak came to be the fourth species probed for a
    # sorghum network.
    if _binomial(target_key) != "arabidopsis_thaliana":
        floor = common.relatedness(target_key, "arabidopsis_thaliana")
        ranked = [e for e in ranked
                  if e == "arabidopsis_thaliana"
                  or common.relatedness(target_key, e) > floor]
    return ranked


def anchor_panel(doss, target_key, cache_profiles, limit=4):
    """Reference species to probe when a node's own evidence names no species.

    Taken from the network's own evidence rather than a fixed list: the species that this
    network's other nodes were actually described in are the ones its unattributed names
    are most likely borrowed from too. Arabidopsis is appended as a last resort because
    plant gene symbol nomenclature is more often derived from it than from anywhere else,
    and its cache carries symbols for 37% of its genes against 1-4% for most crops.
    """
    tally = {}
    for n in doss["nodes"]:
        if not n["mappable"]:
            continue
        for sp, k in (n.get("evidence_species") or {}).items():
            info = common.classify_species(sp)
            ens = info.get("ensembl_name")
            if ens and ens != target_key:
                tally[ens] = tally.get(ens, 0) + k
    ranked = [e for e, _ in sorted(tally.items(), key=lambda kv: -kv[1])]

    # Then whatever the evidence did not name, closest relative first. Evidence still leads:
    # a species this network's other nodes were actually described in beats a species that
    # is merely related. But when the evidence names nothing -- a backfilled network whose
    # legacy records carry no species at all -- Arabidopsis alone is a poor panel for a
    # grass. SbEHD1 is what that costs: probing Arabidopsis for EHD1 finds an EPS15-domain
    # endocytosis protein and projects three sorghum orthologs with every support flag set,
    # because grass EARLY HEADING DATE 1 is a homonym and lives in rice.
    #
    # Relatedness cannot be the only criterion either. Setaria and Panicum are closer to
    # sorghum than rice is, and carry seven gene symbols each; a probe into them answers
    # nothing. The existing sparse/symbol filter below is what keeps those out, so the two
    # tests together are what make this work: close enough to share nomenclature, annotated
    # enough to have any.
    for ens in _related_candidates(target_key):
        if ens not in ranked:
            ranked.append(ens)
    if "arabidopsis_thaliana" not in ranked and _binomial(target_key) != "arabidopsis_thaliana":
        ranked.append("arabidopsis_thaliana")
    out = []
    for ens in ranked:
        prof = cache_profiles.get(ens) or species_profile(ens)
        cache_profiles[ens] = prof
        if prof.get("usable_for_name_lookup") and not prof.get("sparse"):
            out.append(ens)
        if len(out) >= limit:
            break
    return out


def plan_node(node, target, cache_profiles, anchors=(), proxy=None):
    """Ordered routes for one node. Cheap and offline routes first."""
    plan = []
    # For a species with no annotation of its own, the proxy takes the target's place in
    # every route. Leaving it as None made the projection routes fire with no destination.
    tgt_ens = target["ensembl_name"] or ((proxy or {}).get("species"))

    def add(route, why, **args):
        plan.append({"route": route, "why": why, **args})

    # 1. An identifier the cited paper stated outright, in the target's own system.
    native_ids = [h for h in node.get("ids_in_text", [])
                  if h["relation_to_target"] == "native" and h["score"] >= 0.6]
    if native_ids:
        add("stated_id", "a paper behind this node names the identifier directly",
            candidates=[h["gene_id"] for h in native_ids[:5]])

    origin = node.get("name_origin_species")
    origin_ens = node.get("name_origin_ensembl")
    basis = node.get("origin_basis")
    foreign = basis == "evidence_exclusive_foreign"

    # 2. Native name lookup, unless the evidence says the name is not native, or the cache
    #    for this species holds too few symbols to be worth asking.
    if target.get("usable_for_name_lookup"):
        if foreign and target.get("sparse"):
            add("cache_native", "tried second: the evidence is all foreign and this cache is sparse",
                species=tgt_ens, deprioritised=True)
        else:
            add("cache_native", "the name may be native to this species", species=tgt_ens)

    # 3. Resolve where the name actually lives, then project.
    origin_profile = cache_profiles.get(origin) if origin else None
    if origin_ens and origin_ens != tgt_ens and origin_profile \
            and origin_profile.get("usable_for_name_lookup"):
        add("origin_then_project",
            f"every paper behind this node studied {origin}, so the name belongs there"
            if foreign else f"the name is best known in {origin}",
            origin=origin_ens, target=tgt_ens,
            origin_names=node.get("literature_spellings") or [node["node_id"]])

    # 3b. Probe the well-annotated species this network draws on, and project from there.
    #     Two situations need this. Either nothing in the evidence says where the name comes
    #     from -- a symbol like BLH6 or ABCG29 is Arabidopsis nomenclature whether or not
    #     Flash-P managed to attach a species to the sentence. Or the evidence does name a
    #     species but that species has no usable name cache: barley's NCED is attributed to
    #     Lolium rigidum, which has no assembly here, and without this fallback the node
    #     would get no projection route at all despite being a perfectly ordinary symbol.
    #     It is planned for every node, not only these cases, and the gatherer runs it only
    #     when the earlier routes came back empty. A name can be attributed to a species
    #     that does have a cache and still be missing from it -- HvNRT2.1 is attributed to
    #     barley and is not among barley's 1,054 symbols -- and without a fallback such a
    #     node fails despite NRT2.1 being unambiguous in Arabidopsis.
    if anchors:
        add("symbol_probe_then_project",
            "the evidence names no species for this node, so the best-annotated species "
            "this network draws on are probed for the symbol, then projected",
            anchors=[a for a in anchors if a != tgt_ens],
            target=tgt_ens,
            names=node.get("literature_spellings") or [node["node_id"]])

    # 4. An identifier stated in a paper is a seed in its own right -- a projection seed if
    #    it is foreign, a usable answer directly if it is native -- but
    #    only when the paper actually pairs it with this node's name -- and that question is
    #    reading comprehension, not string geometry, so it is put to the mapper rather than
    #    decided here.
    #
    #    Measured on Stay_Green_In_Sorghum, the character-distance rule inverted CGA1: it
    #    called AT3G56290 appositive at a gap of 31 ("...regulating antenna size
    #    (AT3G56290)", where the accession belongs to a phrase, not to CGA1) while demoting
    #    the real pairing, "CGA1 (CYTOKININ-RESPONSIVE GATA FACTOR 1; Sobic.010G173300)", to
    #    same_sentence at a gap of 38 because a semicolon sits inside the parenthetical. The
    #    two gaps are seven characters apart and carry no signal; both snippets are
    #    unambiguous to anyone who reads them.
    #
    #    So nothing is projected from an anchor until the mapper has ruled on the pairing.
    #    The script's own class travels alongside as `script_proximity`, so how often the
    #    two disagree can be counted rather than guessed at.
    #    Identifiers in the network's own species go to the same place, for the part of the
    #    range route 1 does not cover. Route 1 takes a native accession only at appositive or
    #    same_sentence proximity; below that it was dropped outright, while a *foreign*
    #    accession at the very same proximity was adjudicated. That asymmetry cost real
    #    answers on Grain_Protein_Content_Wheat, where the paper prints "TaWRKY42-B
    #    (TraesCS2B02G187500)" and the node was reported unresolved because its own species'
    #    accession had nowhere to go. A native identifier is if anything the more valuable
    #    of the two, since nothing has to be projected for it to be usable.
    #    Native identifiers strong enough for route 1 are listed here as well, rather than
    #    only there. Route 1 admits them as candidates unadjudicated, and the promotion above
    #    made that exposure worse: on TAWRKY42 the clause "TaWRKY42-B can promote JA
    #    biosynthesis by interacting with ... its ortholog (TaLOX3, TraesCS4B02G295200)" now
    #    classes TaLOX3's accession as appositive to TAWRKY42, which is precisely the CGA1
    #    error in the network's own species. Listing them costs nothing -- route 1 still
    #    supplies the candidate, so a node whose pairings are never adjudicated behaves
    #    exactly as before -- but a rejection can now take one back out.
    #    `appositive_by_symbol` is the same typography read out of a paper this node may not
    #    cite, matched to the node on the symbol the paper itself apposed to the identifier.
    #    It carries `matched_label` -- what the paper wrote -- which is the thing to judge:
    #    for a joined node the label is a component, so the identifier is a complex member
    #    rather than the node's whole answer.
    review = [h for h in node.get("ids_in_text", [])
              if h["proximity"] in ("appositive", "appositive_by_symbol",
                                    "same_sentence", "same_paragraph")]
    if review:
        add("anchor_review",
            "a paper names a gene identifier near this node's name; decide whether the "
            "paper pairs them, then record the ones that hold",
            anchors=[{"gene_id": h["gene_id"], "from": h["id_system"],
                      "relation_to_target": h["relation_to_target"],
                      # "legacy" means a superseded annotation release of that species --
                      # GRMZM/Zm00001d for maize, Sb01g for sorghum. The gene is real and the
                      # pairing may be perfectly sound; the identifier still has to be
                      # converted before it can be compared with anything else here.
                      "id_release": h.get("id_release", "reference"),
                      "matched_label": h.get("matched_label", ""),
                      "cited_by_node": h.get("cited_by_node"),
                      "script_proximity": h["proximity"], "gap": h.get("gap"),
                      "doi": h.get("doi"), "snippet": h.get("snippet", "")[:300]}
                     for h in review[:8]],
            target=tgt_ens)

    # 5. Annotation descriptions. Family-level only, so this is a shortlist generator whose
    #    value is in being intersected with whatever the projection routes returned.
    terms = description_terms(node)
    if tgt_ens and terms:
        add("description_shortlist",
            "narrows to a gene family; only decisive when it agrees with a projection",
            species=tgt_ens, terms=terms, shortlist_only=True)

    # 6. Live databases, for names the offline cache never had.
    if tgt_ens:
        add("db_native", "Ensembl, NCBI Gene and UniProt, queried together",
            species=tgt_ens, names=node.get("literature_spellings") or [node["node_id"]],
            function_terms=description_terms(node, for_filtering=True))

    # 7. No assembly at all: the honest answer is an identifier in a relative, flagged.
    if target.get("status") == "outside" and proxy:
        add("proxy_ortholog",
            f"{target['token']} has no Ensembl Plants assembly, so identifiers are resolved "
            f"in {proxy['species']} ({proxy['basis']}) and reported as proxies",
            proxy_species=proxy["species"], proxy_basis=proxy["basis"],
            names=node.get("literature_spellings") or [node["node_id"]])

    return plan


def anchor_rationale(doss, target, anchors):
    """Why this panel, in a sentence a reader can check.

    Written into the summary because for a species nobody anticipated there is no way to
    tell a good panel from a resigned one by looking at the list. "maize, rice, Arabidopsis"
    for sorghum is a real phylogenetic answer; "Arabidopsis" for mango is the cache saying
    it has nothing in Sapindales, and those two deserve to look different.
    """
    key = target["ensembl_name"] or target.get("token")
    from_evidence = []
    for n in doss["nodes"]:
        for sp in (n.get("evidence_species") or {}):
            ens = common.classify_species(sp).get("ensembl_name")
            if ens and ens in anchors and ens not in from_evidence:
                from_evidence.append(ens)
    by_lineage = [a for a in anchors if a not in from_evidence]
    lineage_known = bool(common.ensure_lineages([key]).get(key))
    return {"anchors": anchors,
            "from_evidence": from_evidence,
            "from_lineage": by_lineage,
            "target_lineage_resolved": lineage_known,
            "relatedness": {a: common.relatedness(key, a) for a in anchors}}


def run(doss, anchor_override=()):
    target = species_profile(doss["summary"]["network_species"])
    profiles = {}
    for n in doss["nodes"]:
        o = n.get("name_origin_species")
        if o and o not in profiles:
            profiles[o] = species_profile(o)

    # The target's own binomial stands in when it has no assembly, so a species Ensembl has
    # never sequenced is still ranked against its relatives rather than dropped.
    if anchor_override:
        anchors = []
        for tok in anchor_override:
            ens = common.classify_species(tok).get("ensembl_name")
            if ens:
                anchors.append(ens)
            else:
                print(f"  --anchors: no Ensembl Plants assembly for {tok!r}; skipped",
                      file=sys.stderr)
    else:
        anchors = anchor_panel(doss, target["ensembl_name"] or target.get("token"), profiles)
    proxy = choose_proxy(target, anchors, profiles) if target.get("status") == "outside" else None
    doss["summary"]["anchor_species"] = anchors
    doss["summary"]["anchor_basis"] = ({"anchors": anchors, "source": "user_override"}
                                       if anchor_override
                                       else anchor_rationale(doss, target, anchors))
    doss["summary"]["proxy_species"] = proxy

    counts = {}
    for n in doss["nodes"]:
        if not n["mappable"]:
            n["route_plan"] = []
            continue
        n["route_plan"] = plan_node(n, target, profiles, anchors=anchors, proxy=proxy)
        for step in n["route_plan"]:
            counts[step["route"]] = counts.get(step["route"], 0) + 1

    doss["summary"]["target_species_profile"] = target
    doss["summary"]["origin_species_profiles"] = profiles
    doss["summary"]["route_plan_counts"] = counts
    warnings = []
    basis = doss["summary"]["anchor_basis"]
    if not anchor_override:
        if not basis.get("target_lineage_resolved"):
            warnings.append(
                f"no NCBI lineage could be resolved for {target.get('token')}, so the "
                "well-annotated species to borrow symbols from were not ranked by "
                "relatedness and the panel fell back to Arabidopsis. Pass --anchors to "
                "name better ones.")
        elif anchors == ["arabidopsis_thaliana"] or not anchors:
            warnings.append(
                f"nothing in the cache is both closer to {target.get('token')} than "
                "Arabidopsis and carries enough gene symbols to be worth probing, so "
                "Arabidopsis is the only anchor. Symbols coined in a nearer relative will "
                "not be found. Pass --anchors to name a species you know is the "
                "nomenclature source for this trait.")
        elif not basis.get("from_evidence"):
            warnings.append(
                "the evidence names no species, so the anchor species were chosen by "
                f"relatedness alone: {', '.join(anchors)}. Nomenclature does not always "
                "follow phylogeny -- check these are where this trait's gene names come "
                "from, and pass --anchors if not.")
    if target.get("status") == "outside":
        warnings.append(
            f"{target['token']} has no Ensembl Plants assembly. Identifiers for this network "
            f"are resolved in {(proxy or {}).get('species', 'a relative')} "
            f"({(proxy or {}).get('basis', 'no relative found')}) and every one of them is a "
            "proxy, not a gene in this species.")
    if target.get("ensembl_name") and not target.get("names_are_symbols"):
        warnings.append(
            f"{target['ensembl_name']} reports {target['n_names']} cache names, but its "
            "GFF3 Name field holds a second identifier system rather than gene symbols "
            "(name_class=identifier_alias). Cache hits here are not name matches.")
    if target.get("ensembl_name") and target.get("sparse"):
        warnings.append(
            f"{target['ensembl_name']} has only {target['n_names']} named genes; native "
            "name lookup will answer few nodes and the projection routes carry this network.")
    doss["summary"]["routing_warnings"] = warnings
    return doss


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", required=True)
    ap.add_argument("--out")
    ap.add_argument("--anchors", default="",
                    help="comma-separated species to probe for gene symbols, overriding the "
                         "relatedness ranking. Use when you know which species this trait's "
                         "nomenclature comes from -- legume nodulation genes are named in "
                         "Medicago, grass flowering genes in rice -- and that species is "
                         "either sparse in the cache or not the nearest relative.")
    args = ap.parse_args()
    override = [x.strip() for x in args.anchors.split(",") if x.strip()]
    doss = run(common.load_json(args.dossiers), anchor_override=override)
    common.write_json(args.out or args.dossiers, doss)
    s = doss["summary"]
    print(json.dumps({"target": s["target_species_profile"],
                      "anchors": s.get("anchor_basis"),
                      "routes": s["route_plan_counts"],
                      "warnings": s["routing_warnings"]}, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()

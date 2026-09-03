#!/usr/bin/env python3
"""Run every applicable resolution route for each node and collect what they return.

This does the mechanical work so that judgement is all that is left. For one node it may
consult the offline name cache, the same cache in the species the name actually comes from
followed by an ortholog projection, identifiers stated outright in the cited papers, live
databases, and the annotation description layer -- then put every candidate side by side
with the routes that produced it.

Agreement between routes is the thing to read. No single route is reliable enough to be
believed alone: the name cache holds symbols for barely 1% of sorghum genes, projection
across many-to-many orthologs is right about a third of the time, descriptions identify a
family rather than a gene, and papers occasionally print an accession that disagrees with
the reference annotation. Two independent routes landing on the same identifier is worth
far more than any one of them landing on it confidently.

Conflicts are surfaced, never resolved silently. Where a paper states an identifier that
disagrees with the cache, both appear, flagged.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import describe_genes  # noqa: E402
import project_orthologs as proj  # noqa: E402

# What each route is worth on its own, before agreement is taken into account. These come
# from the held-out benchmarking of the underlying steps, not from intuition.
ROUTE_WEIGHT = {
    "stated_id": 0.80,          # appositive accession in the node's own cited paper
    "cache_exact": 0.85,        # exact key in the offline name cache
    "cache_prefix_stripped": 0.50,   # matched only after stripping a species prefix
    "db_on_reference": 0.60,
    "origin_then_project": 0.55,     # scaled by the projection's own tier
    "anchor_then_project": 0.60,
    "description_shortlist": 0.15,   # family-level; never an answer alone
    "symbol_probe_then_project": 0.45,
    "family_member": 0.55,
    "complex_component": 0.70,
    "proxy_ortholog": 0.40,
}


def run_jsonl(script, args, timeout=600):
    """Run a resolver-skill script and parse its JSON-lines output."""
    try:
        p = subprocess.run([sys.executable, script] + args, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    rows = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def query_names(node):
    """Name spellings to ask about, most trustworthy first.

    The literature spelling comes first because network.json uppercases node ids, and the
    original casing is what the caches were built from: 'SbMATE' resolves where 'SBMATE'
    returns nothing at all.
    """
    out = []
    for n in (node.get("literature_spellings") or []) + [node["node_id"]]:
        for v in (n, n.replace("_", "-"), n.replace("_", ".")):
            if v and v not in out:
                out.append(v)
    return out[:8]


def deprefixed(names):
    """Name spellings with a leading species abbreviation removed.

    A symbol carries its organism when it is written for a foreign audience -- HvNRT2.1,
    SbMATE, SlPIF3 -- and that prefix is exactly what stops it matching in the species it
    is being probed against, where the same gene is simply NRT2.1.

    Stripping is deliberately narrow. Earlier work here had a prefix rule that turned STH1
    into h1, so the prefix must be an uppercase letter followed by a lowercase one, and
    what remains must still start with an uppercase letter or a digit. STH1 is all upper
    case after the S, so it is left alone.

    Two characters is enough. The old floor of three silently excluded CO, FD, FT and GI --
    the core of any flowering network, and all four exact keys in the Arabidopsis cache --
    while the native cache route had no such floor, so the two routes disagreed about what
    counted as a name. Loosening it also admits a few meaningless strips (Lr34 -> 34,
    Cs1A -> 1A), which is the right trade: a probe that matches nothing costs nothing, and
    a probe that matches the wrong gene is caught downstream, where every anchor hit is now
    checked against the node's own function before it can outrank another anchor's.
    """
    out = []
    for n in names:
        bare = common.strip_species_prefix(n)
        if bare and bare not in out:
            out.append(bare)
    return out


def cache_lookup(names, species):
    rows = run_jsonl(common.resolver_script("lookup_cache.py"),
                     ["--species", species, "--names", ",".join(names)])
    out = []
    for r in rows:
        if not r.get("resolved"):
            continue
        route = ("cache_exact" if r.get("matched_via") == "exact"
                 else "cache_prefix_stripped")
        for gid in r.get("candidates", []):
            out.append({"gene_id": gid, "route": route, "queried": r.get("name"),
                        "n_candidates": r.get("n_candidates", 1),
                        "note": r.get("note", "")})
    return out


# UniProt is queried as free text over the whole entry, not by the gene-name field --
# resolve_databases.py does this deliberately, because it lifts recall from 9% to 35%, and
# says in its own docstring to expect to filter. Nothing here was filtering. The result is
# that a query for COI1 returns the JAZ/TIFY proteins whose entries *mention* COI1 as the
# receptor that degrades them, and not one F-box gene; a query for NAP returns Nucleosome
# Assembly Protein 1. Ensembl and NCBI are queried by symbol and do not behave this way, so
# which source produced a hit is the signal that separates a lookup from a text match.
STRUCTURED_DB = {"ensembl", "ncbi"}
FREE_TEXT_WEIGHT = 0.22        # annotation silent
FREE_TEXT_CORROBORATED = 0.45
# A free-text hit whose annotation names a different kind of protein. Ranked below an
# unexamined one rather than removed, so it is visible and last.
FREE_TEXT_CONTRADICTED = 0.08
# Applied to a symbol probe whose anchor gene is annotated as something the node is not.
# Enough that any anchor with a compatible or merely silent annotation outranks it, while
# leaving it visible and rankable if nothing better exists.
SOURCE_CONTRADICTED = 0.2  # annotation names the right kind of protein


# ---------------------------------------------------------------------------
# The live database route, asked in batches
#
# resolve_databases.py queries Ensembl Plants, NCBI Gene and UniProt for a name and pools
# the answers; the three sources agree on only 13% of cases, which is why all three are
# asked. It already takes a list of names and fans them across its own thread pool -- five
# names cost 7.5s together against 23.7s one at a time -- but it was being invoked once
# per node with only that node's spellings, so the pool never had more than a couple of
# names to work with and the fixed per-invocation cost was paid once per node.
#
# Asking for every node's names up front costs a handful of invocations instead of one per
# node, and it removes a nesting problem as well: a per-node call runs up to four name
# threads times three sources, so putting node-level workers on top of it multiplies the
# requests in flight. Batching flattens that back out.
#
# Rows come back keyed by the name asked, so demultiplexing is exact rather than
# positional. Chunked rather than sent as one list: a crash then costs a chunk instead of
# the whole network.
# ---------------------------------------------------------------------------
DB_BATCH = 10          # names per invocation
DB_WORKERS = 5         # name threads inside one invocation

_DB_ROWS = {}          # (species, name) -> row from resolve_databases.py, {} if it held none
_DB_LOCK = threading.Lock()


def _run_resolver(names, species, workers=DB_WORKERS):
    """One resolve_databases.py invocation for a list of names -> {name: row}.

    --names is comma-separated, so a name containing a comma would be read as two. Gene
    symbols do not contain commas, but the resolver's own docstring warns that a
    mis-passed argument is reported as a clean unresolved rather than an error, and a
    silent wrong answer is exactly what this tool must not produce. Such names go through
    --name individually.
    """
    script = common.resolver_script("resolve_databases.py")
    safe = [n for n in names if "," not in n]
    odd = [n for n in names if "," in n]
    rows = []
    if safe:
        rows += run_jsonl(script, ["--species", species, "--names", ",".join(safe),
                                   "--workers", str(workers)])
    for n in odd:
        rows += run_jsonl(script, ["--species", species, "--name", n])
    return {r.get("name"): r for r in rows if r.get("name")}


def db_species_for(node, target_ens):
    """The species gather_node will ask the live databases about, or None.

    Mirrors the plan handling at the top of gather_node: a network whose own species has
    no reference annotation is resolved in a proxy instead, and every route including this
    one then runs against that proxy.
    """
    plan = {s["route"]: s for s in node.get("route_plan", [])}
    if not target_ens:
        pr = plan.get("proxy_ortholog")
        return (pr or {}).get("proxy_species")
    return target_ens if "db_native" in plan else None


def prefetch_db_lookups(nodes, target_ens, quiet=False):
    """Ask the databases for every node's names before the node loop starts."""
    by_species = collections.OrderedDict()
    for node in nodes:
        if not node.get("mappable"):
            continue
        species = db_species_for(node, target_ens)
        if not species:
            continue
        seen = by_species.setdefault(species, [])
        for n in query_names(node):
            if n and n not in seen:
                seen.append(n)
    for species, names in by_species.items():
        for i in range(0, len(names), DB_BATCH):
            chunk = names[i:i + DB_BATCH]
            fetched = _run_resolver(chunk, species)
            with _DB_LOCK:
                for n in chunk:
                    _DB_ROWS.setdefault((species, n), fetched.get(n, {}))
        if not quiet:
            print(f"  database route: {len(names)} name(s) in {species} asked in "
                  f"{-(-len(names) // DB_BATCH)} call(s)", file=sys.stderr)


def db_lookup(names, species):
    want = [n for n in names if n]
    rows, missing = [], []
    with _DB_LOCK:
        for n in want:
            r = _DB_ROWS.get((species, n))
            if r is None:
                missing.append(n)
            elif r:
                rows.append(r)
    # A name the prefetch did not cover -- a spelling reached by a route that decides its
    # species inside gather_node. Asked on its own rather than skipped.
    if missing:
        fetched = _run_resolver(missing, species)
        with _DB_LOCK:
            for n in missing:
                _DB_ROWS.setdefault((species, n), fetched.get(n, {}))
        rows += [fetched[n] for n in missing if fetched.get(n)]
    out = []
    for r in rows:
        by_source = {src: {proj.canon(g) for g in (r.get(src) or [])}
                     for src in ("ensembl", "ncbi", "uniprot")}

        def sources_for(gid):
            c = proj.canon(gid)
            return sorted(src for src, ids in by_source.items() if c in ids)

        for gid in r.get("on_reference", []):
            src = sources_for(gid)
            out.append({"gene_id": gid, "route": "db_on_reference", "queried": r.get("name"),
                        "n_sources": r.get("n_sources", 0), "sources": src,
                        "free_text_only": not (set(src) & STRUCTURED_DB)})
        for gid in r.get("other_release_or_system", []):
            src = sources_for(gid)
            out.append({"gene_id": gid, "route": "db_other_release", "queried": r.get("name"),
                        "n_sources": r.get("n_sources", 0), "sources": src,
                        "free_text_only": not (set(src) & STRUCTURED_DB),
                        "note": "identifier is from another annotation release or system"})
    return out


def plausibility(gene_id, target_ens, terms):
    """What the annotation says about a free-text database hit: corroborated, silent or contradicted.

    Three states, not two. The reason to distrust a hit that only UniProt's full-entry
    search produced is that it may be a different protein that merely mentions this gene --
    and an annotation naming the right kind of protein answers exactly that doubt, so a
    corroborated hit should not be penalised as though it were unexamined. A gene with no
    description at all is never rejected: most crop gene models have none, and silence is
    not evidence of the wrong gene.
    """
    if not terms:
        return "silent"
    d = describe_genes.annotate([gene_id], target_ens)
    text = (d[0].get("description") if d else "") or ""
    if not text:
        return "silent"
    low = text.lower()
    hit = any(re.search(r"\b" + re.escape(t), low) for t in terms)
    return "corroborated" if hit else "contradicted"


# How many candidates a node may carry. Was 12, which cut 97 candidates across the corpus
# for no measured gain -- the median node has three, so the list was never the crowded thing
# it was being protected from. Kept finite only so a runaway family probe cannot produce a
# dossier too large to read.
MAX_CANDIDATES = 60

# How long one Ensembl description batch may take during the mined-identifier pass. Short
# on purpose: these descriptions improve a judgement, none of them is required to make one.
MINED_LOOKUP_TIMEOUT = 15


COMPLEX_TYPES = {"protein_complex"}


def split_complex(node):
    """Component symbols of a protein complex node, or [] if it is not one.

    Flash-P writes a complex as its subunits joined by underscores -- BTR1_BTR2. Looking
    that string up as a gene name cannot work, but each half is a real symbol, and the
    node's honest answer is the set of subunit identifiers.

    Only applied to nodes Flash-P typed as a complex. Applying it by name shape would
    wreck ordinary gene nodes: NAM_B1 and NRT2_1 are single genes whose names happen to
    contain an underscore.
    """
    if node.get("node_type") not in COMPLEX_TYPES:
        return []
    parts = [p for p in re.split(r"[_/+-]", node["node_id"]) if len(p) >= 2]
    return parts if len(parts) >= 2 else []


# How many numbered members must resolve before a bare symbol is treated as a family
# rather than a name that simply is not in the cache.
MIN_FAMILY_MEMBERS = 2


def probe_family(node, target_ens, plan, use_plaza=True):
    """Numbered members of a gene family, when the bare family name resolves to nothing.

    Only attempted for a plain alphabetic symbol. A name that already ends in a digit is a
    specific gene, and treating PSY1 as a family would invent PSY11 and PSY12.
    """
    name = node["node_id"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z]{1,9}", name):
        return []

    species = None
    for key in ("origin_then_project", "symbol_probe_then_project"):
        step = plan.get(key)
        if step:
            species = step.get("origin") or (step.get("anchors") or [None])[0]
            break
    if not species:
        species = target_ens
    if not species:
        return []

    variants = [f"{name}{i}" for i in range(1, 13)]
    hits = cache_lookup(variants, species)
    by_member = collections.OrderedDict()
    for h in hits:
        by_member.setdefault(h["queried"], []).append(h)
    if len(by_member) < MIN_FAMILY_MEMBERS:
        return []

    out = []
    for member, hs in list(by_member.items())[:8]:
        gid = hs[0]["gene_id"]
        if species == target_ens:
            out.append({"member": member, "source_species": species, "source_gene_id": gid,
                        "candidates": [{"gene_id": gid, "score": 0.7,
                                        "route_kinds": ["cache_exact"]}]})
        else:
            pr = proj.project(gid, species, target_ens, use_plaza=use_plaza)
            out.append({"member": member, "source_species": species, "source_gene_id": gid,
                        "candidates": [{"gene_id": c["gene_id"], "score": c["score"],
                                        "relation": c["type"], "route_kinds": ["family_member"]}
                                       for c in pr.get("candidates", [])[:2]]})
    return out


def gather_node(node, target_ens, offline=False, use_plaza=True):
    cands = collections.OrderedDict()
    # Homoeolog sets reported by the projections, keyed by the source gene they came from.
    # A polyploid's counterpart to one gene is a set, and collapsing that into competing
    # candidates loses the fact that they are one answer.
    homoeologs = []

    def note_homoeologs(pr, source_gene, source_species):
        hs = pr.get("homoeolog_set") or []
        if len(hs) > 1:
            homoeologs.append({
                "source_gene_id": source_gene, "source_species": source_species,
                "members": hs,
                "subgenomes_covered": pr.get("subgenomes_covered", ""),
                "subgenomes_expected": pr.get("subgenomes_expected", ""),
                "complete": (pr.get("subgenomes_covered", "") ==
                             pr.get("subgenomes_expected", ""))})

    def add(gene_id, route, weight, **extra):
        gene_id = proj.canon(gene_id)
        if not gene_id:
            return
        rec = cands.setdefault(gene_id, {"gene_id": gene_id, "routes": [], "score": 0.0,
                                         "id_system": "ensembl"})
        if extra.get("id_system"):
            rec["id_system"] = extra["id_system"]
        rec["routes"].append({"route": route, "weight": round(weight, 3), **extra})

    plan = {s["route"]: s for s in node.get("route_plan", [])}
    names = query_names(node)

    # A species with no reference annotation is resolved in a relative instead. Every route
    # below then runs against that relative, and every identifier it returns is recorded as
    # a proxy -- it anchors the node to a real gene model but is not a gene in the network's
    # species, and must never be handed on as though it were.
    proxy_species = None
    if not target_ens and "proxy_ortholog" in plan:
        proxy_species = plan["proxy_ortholog"]["proxy_species"]
        target_ens = proxy_species
        # The proxy stands in for the target, so a probe into it is a direct lookup there,
        # not something to project onward.
        plan.setdefault("cache_native", {"route": "cache_native", "species": proxy_species})
        plan.setdefault("db_native", {"route": "db_native", "species": proxy_species})

    if "stated_id" in plan:
        for h in node.get("ids_in_text", []):
            if h["relation_to_target"] == "native" and h["score"] >= 0.6:
                # A superseded release is the right gene under an identifier the rest of the
                # pipeline cannot match, so it must not sit level with a current accession:
                # it would win the ranking and be emitted in a namespace nothing else uses.
                legacy = h.get("id_release") == "legacy"
                add(h["gene_id"], "stated_id",
                    ROUTE_WEIGHT["stated_id"]
                    * (1.0 if h["proximity"] == "appositive" else 0.75)
                    * (0.6 if legacy else 1.0),
                    doi=h["doi"], proximity=h["proximity"],
                    id_release=h.get("id_release", "reference"),
                    matched_label=h.get("matched_label", ""),
                    snippet=h["snippet"][:180])

    if "cache_native" in plan and target_ens:
        for c in cache_lookup(names, target_ens):
            add(c["gene_id"], c["route"], ROUTE_WEIGHT[c["route"]],
                queried=c["queried"], note=c.get("note", ""))

    if "origin_then_project" in plan and not offline:
        step = plan["origin_then_project"]
        src_ens = step["origin"]
        src_hits = cache_lookup(names + deprefixed(names), src_ens)
        if src_ens == target_ens:
            # The name's own species is the one we are resolving into -- for a proxy this
            # happens whenever the proxy is the congener the literature used. There is
            # nothing to project; the cache hit is already the answer.
            for c in src_hits[:4]:
                add(c["gene_id"], c["route"], ROUTE_WEIGHT.get(c["route"], 0.5),
                    queried=c["queried"], note=c.get("note", ""))
            src_hits = []
        seen_src = []
        # Two source genes is enough. Each one costs a projection plus its reciprocal
        # checks, and a third spelling of the same name rarely adds a new candidate.
        for c in src_hits[:2]:
            if c["gene_id"] in seen_src:
                continue
            seen_src.append(c["gene_id"])
            pr = proj.project(c["gene_id"], src_ens, target_ens, use_plaza=use_plaza)
            note_homoeologs(pr, c["gene_id"], src_ens)
            for pc in pr.get("candidates", [])[:6]:
                add(pc["gene_id"], "origin_then_project",
                    ROUTE_WEIGHT["origin_then_project"] * (pc["score"] / 0.95),
                    source_species=src_ens, source_gene_id=c["gene_id"],
                    source_route=c["route"], relation=pc["type"],
                    perc_id=pc.get("perc_id"), support=pc["support"])

    # Last resort, and only when everything before it came back empty: probe a
    # well-annotated reference for the symbol and project from there.
    if "symbol_probe_then_project" in plan and not offline and not cands:
        step = plan["symbol_probe_then_project"]
        # Every anchor is probed, not just the first that recognises the string.
        #
        # Stopping at the first hit assumed a symbol means the same thing wherever it is
        # known. It does not. Maize `ap1` is Zm00001eb076170, which also answers to Px11 and
        # is a peroxidase; rice `spl` is Os01g0100900, also S1PL, sphingosine-1-phosphate
        # lyase. Both are real cache entries, so the loop stopped there and Arabidopsis --
        # which holds APETALA1 at AT1G69120 -- was never asked. The projections off those
        # wrong sources were flawless: one2one, reciprocal best, Gramene and PLAZA agreeing,
        # a single candidate at high score. Every support flag true and the gene family
        # wrong, because the error happened at symbol lookup before any orthology ran.
        #
        # So the anchors compete instead. Each hit is checked against what the node is said
        # to be, using the same three-state test as the free-text route: an anchor gene whose
        # own annotation contradicts the node's function is kept and heavily discounted
        # rather than dropped, because a keyword test is not entitled to delete a candidate
        # -- it only has to stop one outranking a better-supported rival.
        fn_terms = (plan.get("db_native") or {}).get("function_terms") or []
        probes = []
        for anchor in step["anchors"]:
            for c in cache_lookup(names + deprefixed(names), anchor)[:2]:
                probes.append((anchor, c,
                               plausibility(c["gene_id"], anchor, fn_terms) if fn_terms else None))

        # The penalty is relative, not absolute. A keyword test comparing Flash-P's one-line
        # gloss against a PANTHER family name says "contradicted" far too readily: rice Ghd7
        # is called contradicted against "grain number, plant height and heading date", and
        # it is the correct source gene. Discounting it outright would cost the very node
        # this route exists to answer. So a contradicted anchor is demoted only when some
        # other anchor produced a hit that is not contradicted -- when there is a rival to
        # prefer. When every anchor looks equally implausible, the test has told us nothing
        # and the projections are left to compete on their own evidence.
        has_rival = any(v != "contradicted" for _a, _c, v in probes)
        for anchor, c, verdict in probes:
            penalty = (SOURCE_CONTRADICTED
                       if verdict == "contradicted" and has_rival else 1.0)
            pr = proj.project(c["gene_id"], anchor, target_ens, use_plaza=use_plaza)
            note_homoeologs(pr, c["gene_id"], anchor)
            for pc in pr.get("candidates", [])[:5]:
                # Discounted against origin_then_project: there the evidence told us
                # which species the name belongs to, whereas here we are inferring it
                # from the symbol being known in a well-annotated reference.
                add(pc["gene_id"], "symbol_probe_then_project",
                    0.45 * (pc["score"] / 0.95)
                    * (1.0 if c["route"] == "cache_exact" else 0.6) * penalty,
                    probed_species=anchor, source_gene_id=c["gene_id"],
                    source_route=c["route"], relation=pc["type"],
                    source_annotation=verdict,
                    perc_id=pc.get("perc_id"), support=pc["support"])

    # Anchors are no longer projected here. Whether a paper pairs an accession with this
    # node's name is a judgement the mapper makes from the snippet; the accepted ones are
    # projected afterwards by project_anchor.py. See route_node.py step 4.
    anchors_for_review = list(plan.get("anchor_review", {}).get("anchors", []))

    if "db_native" in plan and target_ens and not offline:
        fn_terms = plan["db_native"].get("function_terms") or []
        for c in db_lookup(names, target_ens):
            w = ROUTE_WEIGHT.get(c["route"], 0.3)
            verdict = None
            if c.get("free_text_only"):
                # A hit only UniProt's full-entry search produced. Drop it when the
                # annotation says it is a different kind of protein; keep it at reduced
                # weight when the annotation is silent; and keep most of its weight when
                # the annotation confirms the protein, which is the doubt that made the
                # route weak in the first place. Even corroborated it stays below a
                # structured symbol lookup, because a text match plus a family-level
                # annotation is not the same as a database naming the gene.
                verdict = plausibility(c["gene_id"], target_ens, fn_terms)
                # Contradicted hits used to be dropped here. They are kept and labelled
                # instead. A deletion is a judgement made by a keyword test -- the terms
                # come from the node's stated function, and a gene can be the right one
                # while its PANTHER family name shares no vocabulary with that phrasing.
                # The agent has the description and the function text side by side and can
                # see which it is; a dropped candidate it can never see.
                w = min(w, FREE_TEXT_CORROBORATED if verdict == "corroborated"
                        else FREE_TEXT_CONTRADICTED if verdict == "contradicted"
                        else FREE_TEXT_WEIGHT)
            add(c["gene_id"], c["route"], w, queried=c["queried"],
                n_sources=c.get("n_sources", 0), sources=c.get("sources", []),
                free_text_only=c.get("free_text_only", False),
                annotation=verdict, note=c.get("note", ""))

    # A node named for a gene family. NCED, ACS and YUCCA are not genes: the genes are
    # NCED1..NCED9, ACS1..ACS12, YUC1..YUC11. The bare symbol resolves nowhere, which
    # looks like failure but is really a category difference -- the honest answer is the
    # family's members, reported as a family_set.
    family = []
    if not cands and not offline:
        family = probe_family(node, target_ens, plan, use_plaza=use_plaza)
        for m in family:
            for c in m["candidates"][:2]:
                add(c["gene_id"], "family_member",
                    min(0.55, c["score"] * 0.8), member=m["member"])

    # A protein complex is not one gene. Resolve each subunit separately and report them
    # as components, so the node can be given relation complex_members rather than being
    # recorded as an unresolvable name.
    components = []
    for part in split_complex(node):
        sub = {"node_id": part, "literature_spellings": [part],
               "node_type": "gene", "mappable": True,
               "network_species": node.get("network_species"),
               "ids_in_text": [], "sentences": node.get("sentences", []),
               "route_plan": [s for s in node.get("route_plan", [])
                              if s["route"] in ("cache_native", "db_native",
                                                "origin_then_project",
                                                "symbol_probe_then_project")],
               "name_origin_species": node.get("name_origin_species"),
               "name_origin_ensembl": node.get("name_origin_ensembl"),
               "origin_basis": node.get("origin_basis")}
        got = gather_node(sub, target_ens, offline=offline, use_plaza=use_plaza)
        components.append({"component": part,
                           "candidates": got["candidates"][:4],
                           "n_candidates": got["n_total_candidates"]})
        for c in got["candidates"][:2]:
            add(c["gene_id"], "complex_component", min(0.7, c["score"]), component=part)

    # Descriptions come last and deliberately do not create new candidates on their own
    # unless nothing else did. Their job is to corroborate the candidates already found.
    shortlist = []
    if "description_shortlist" in plan and target_ens:
        terms = plan["description_shortlist"]["terms"]
        shortlist = describe_genes.search(terms, target_ens, limit=40)
        short_ids = {r["gene_id"] for r in shortlist}
        for gid in list(cands):
            if gid in short_ids:
                hit = next(r for r in shortlist if r["gene_id"] == gid)
                # Two independent annotations saying the same thing is a real second
                # opinion; two that disagree on the member number are the annotation route
                # admitting it cannot settle this gene, and should not corroborate at full
                # weight. One source alone stays where it was.
                ann = describe_genes.annotate([gid], target_ens)
                agree = (ann[0].get("sources_agree") if ann else None)
                w = 0.25 if agree is None else (0.38 if agree else 0.12)
                # Whether "auxin response factor 4" can be the node's ARF2 is a reading
                # question, not a parsing one, and parsing it here went wrong twice: once
                # calling ORE1 a conflict with ANAC092, which is its own alias, and once
                # reading PANTHER's accession digits as member numbers and penalising PIF5
                # for its own correct annotation. So the observation is recorded and the
                # weight is left alone -- the agent has the full text of both annotations
                # in front of it and can see what the number means.
                desc = (ann[0].get("description") if ann else "") or hit["description"]
                conflict = any(describe_genes.member_conflict(nm, desc) for nm in names)
                add(gid, "description_agrees", w,
                    matched_terms=hit["matched_terms"], description=hit["description"],
                    annotation_sources=(ann[0].get("n_annotation_sources") if ann else 1),
                    sources_agree=agree, member_conflict=conflict)
        if not cands and shortlist:
            # The whole shortlist, not the first eight. It is only reached when no other
            # route produced anything, so a node here has nothing else to offer; cutting it
            # to eight discarded a median of 32 family members per node on the strength of
            # a keyword score that was never meant to rank genes, only to find a family.
            for r in shortlist:
                add(r["gene_id"], "description_shortlist",
                    ROUTE_WEIGHT["description_shortlist"] * r["score"],
                    matched_terms=r["matched_terms"], description=r["description"],
                    shortlist_only=True)

    # Score: the strongest single route, plus a bonus for each additional independent
    # route that agrees. Repeats of the same route are not independent and add nothing.
    for rec in cands.values():
        kinds = {r["route"] for r in rec["routes"]}
        best = max(r["weight"] for r in rec["routes"])
        extra = sum(sorted((max(r["weight"] for r in rec["routes"] if r["route"] == k)
                            for k in kinds), reverse=True)[1:])
        rec["score"] = round(min(0.98, best + 0.35 * extra), 3)
        rec["n_routes"] = len(kinds)
        rec["route_kinds"] = sorted(kinds)

    if proxy_species:
        for rec in cands.values():
            rec["proxy_species"] = proxy_species
            # Nothing found in a stand-in species can be as good as the same finding in the
            # species actually asked about; the discount says so rather than leaving the
            # caller to remember it.
            rec["score"] = round(rec["score"] * 0.75, 3)

    ranked = sorted(cands.values(), key=lambda r: (-r["score"], r["gene_id"]))

    # Attach descriptions so the candidates can actually be told apart.
    if ranked and target_ens:
        descs = {d["gene_id"]: d for d in
                 describe_genes.annotate([r["gene_id"] for r in ranked[:MAX_CANDIDATES]],
                                         target_ens)}
        for r in ranked:
            d = descs.get(r["gene_id"], {})
            r["description"] = d.get("description", "")
            r["description_source"] = d.get("source", "none")
            # Both annotations, labelled by who wrote them. The merged string runs them
            # together with a pipe, which hides that these are two independent curations
            # that sometimes contradict each other -- and which of them says what is
            # exactly the thing worth knowing when they do.
            if d.get("by_source"):
                r["description_by_source"] = d["by_source"]
                r["annotation_sources_agree"] = d.get("sources_agree")

    conflicts = []
    # A symbol probe whose every anchor gene is annotated as something the node is not.
    # Scoring cannot express this: with no better-annotated rival there is nothing to prefer,
    # so the discount does not apply and the candidate stands alone looking well supported.
    # SbSPL is the case -- rice `spl` is sphingosine-1-phosphate lyase, no other anchor knows
    # the symbol, and the projection off it is one2one with every support flag set. The
    # answer is not a lower score, it is telling the mapper what the route actually did.
    probe_rows = [r for c in ranked for r in c["routes"]
                  if r["route"] == "symbol_probe_then_project"]
    if probe_rows and all(r.get("source_annotation") == "contradicted" for r in probe_rows):
        srcs = sorted({f"{r.get('probed_species')}:{r.get('source_gene_id')}"
                       for r in probe_rows})
        conflicts.append(
            f"every symbol probe that matched came from a gene annotated as something this "
            f"node is not ({', '.join(srcs)}). The symbol may mean a different gene in those "
            "species -- rice `spl` is a sphingosine-1-phosphate lyase, maize `ap1` a "
            "peroxidase -- in which case the projection is sound and the source is wrong. "
            "Check the source gene's own annotation before accepting any of these.")
    if len(ranked) < 3:
        conflicts.append(
            "few candidates; gathered.mined_identifiers lists every identifier the cited "
            "papers named, with its annotation. One of them may be this gene even though "
            "no route proposed it -- judge those on what the annotation says, not on where "
            "the accession sat, and admit any you accept with project_anchor.py")
    if anchors_for_review:
        conflicts.append(
            f"{len(anchors_for_review)} identifier(s) from cited papers are awaiting "
            "adjudication; read gathered.anchors_for_review before judging this node")
    stated = {r["gene_id"] for r in ranked if "stated_id" in r["route_kinds"]}
    cached = {r["gene_id"] for r in ranked if "cache_exact" in r["route_kinds"]}
    if stated and cached and not (stated & cached):
        conflicts.append(
            f"a cited paper states {sorted(stated)} but the reference annotation cache "
            f"gives {sorted(cached)}; published accessions are sometimes wrong or from a "
            "different annotation release, so neither is automatically right")

    return {"candidates": ranked[:MAX_CANDIDATES], "anchors_for_review": anchors_for_review,
            "homoeolog_sets": homoeologs,
            "shortlist_size": len(shortlist),
            "conflicts": conflicts, "n_total_candidates": len(cands),
            "components": components, "family_members": family,
            "proxy_species": proxy_species}


def annotate_mined_identifiers(nodes, offline=False):
    """Attach the annotation of every identifier mined from the papers, whatever its proximity.

    Proximity answers "does this accession sit near this gene's name", which is a question
    about typography. For the identifiers that clear the adjudication threshold that is the
    right question, because a paper writing "GENE (ID)" is asserting the pairing. Below the
    threshold it is not weak evidence of a pairing but the absence of any: `same_paper` means
    the name occurs nowhere within 400 characters, so the snippet handed to a reader does not
    contain the gene name and there is nothing in it to read.

    What makes such a hit judgeable is not where it sits but what it is. "prolamin-box binding
    factor" against a node called PBF settles the question with no proximity involved, and it
    settles it from an annotation database rather than from recall. So every mined identifier
    is annotated and reported; none of them becomes a candidate on that basis, and whether any
    of them is this node's gene stays the mapper's call.

    Descriptions are fetched once per species for the whole network. Per node it would be
    thousands of lookups for a few hundred distinct genes.
    """
    wanted = collections.defaultdict(set)
    for n in nodes:
        for h in n.get("ids_in_text", []):
            if h.get("id_system"):
                wanted[h["id_system"]].add(h["gene_id"])

    # Best-effort, and bounded. This pass is enrichment: it must not build a description
    # layer for a species that contributed three accessions to one paper, and it must not
    # stall a run when Ensembl is unwell. Measured while building it -- the REST endpoint
    # was returning 500s -- the unbounded version turned a four-node run into eight minutes.
    # The offline layers answer most of it anyway, so a species Ensembl cannot serve costs
    # some descriptions rather than the run.
    desc = {}
    for sp, ids in wanted.items():
        try:
            layer = describe_genes.load_layer(sp)
        except Exception as exc:                      # no layer for this species
            print(f"  no description layer for {sp}: {exc}", file=sys.stderr)
            layer = {}
        for g in ids:
            desc[(sp, g)] = (layer.get(g) or {}).get("description") or ""
        missing = sorted(g for g in ids if not desc[(sp, g)])
        if missing and not offline:
            for g, text in describe_genes.ensembl_descriptions(
                    missing, timeout=MINED_LOOKUP_TIMEOUT).items():
                desc[(sp, g)] = text

    for n in nodes:
        mined = []
        for h in n.get("ids_in_text", []):
            mined.append({"gene_id": h["gene_id"], "id_system": h.get("id_system", ""),
                          "relation_to_target": h.get("relation_to_target", ""),
                          "proximity": h["proximity"],
                          "id_release": h.get("id_release", "reference"),
                          # Only set for a pairing propagated from elsewhere in the corpus:
                          # the symbol the paper apposed, and whether this node cites it.
                          "matched_label": h.get("matched_label", ""),
                          "cited_by_node": h.get("cited_by_node"),
                          "description": desc.get((h.get("id_system"), h["gene_id"]), ""),
                          "doi": h.get("doi", ""),
                          "snippet": (h.get("snippet") or "")[:240]})
        if mined:
            n.setdefault("gathered", {})["mined_identifiers"] = mined
    return sum(1 for v in desc.values() if v), sum(len(v) for v in wanted.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", required=True)
    ap.add_argument("--out")
    ap.add_argument("--node", help="only this node id (repeatable via commas)")
    ap.add_argument("--offline", action="store_true",
                    help="cache and descriptions only; no network calls")
    ap.add_argument("--no-plaza", action="store_true",
                    help="skip PLAZA, avoiding a large one-off download for a species pair "
                         "not seen before; projections then rest on Compara and Gramene")
    ap.add_argument("--limit", type=int, help="stop after this many nodes (for testing)")
    ap.add_argument("--workers", type=int, default=3,
                    help="nodes gathered at once. Each node is independent -- gather_node "
                         "reads only its own node and the target species -- so this is "
                         "safe to raise; the ceiling on requests is the per-host budget in "
                         "common.py, not this number. 1 restores serial behaviour.")
    ap.add_argument("--no-db-prefetch", action="store_true",
                    help="ask the live databases per node instead of in batches")
    args = ap.parse_args()

    doss = common.load_json(args.dossiers)
    target_ens = (doss["summary"].get("target_species_profile") or {}).get("ensembl_name")
    only = {n.strip() for n in args.node.split(",")} if args.node else None

    todo = [n for n in doss["nodes"]
            if n["mappable"] and not (only and n["node_id"] not in only)]
    if args.limit:
        todo = todo[:args.limit]

    if not args.offline and not args.no_db_prefetch:
        prefetch_db_lookups(todo, target_ens)

    def run(node):
        node["gathered"] = gather_node(node, target_ens, offline=args.offline,
                                       use_plaza=not args.no_plaza)
        return node

    def log(node):
        g = node["gathered"]
        top = g["candidates"][0]["gene_id"] if g["candidates"] else "-"
        print(f"  {node['node_id']:22s} {g['n_total_candidates']:3d} candidates  top={top}",
              file=sys.stderr)

    # Nodes are gathered concurrently but logged in dossier order: ex.map yields in input
    # order, so the log stays the same readable progress list it was when this ran serially,
    # and anything watching it can still find a node by name.
    workers = max(1, args.workers)
    done = 0
    try:
        if workers == 1:
            for node in todo:
                log(run(node))
                done += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for node in ex.map(run, todo):
                    log(node)
                    done += 1
    finally:
        proj.flush_cache()

    # Every identifier the papers named, annotated, whatever its proximity. Nodes the run
    # skipped are included: a mined identifier costs one description lookup and needs none
    # of the network calls the candidate routes make.
    with_desc, n_mined = annotate_mined_identifiers(doss["nodes"], offline=args.offline)
    print(f"  annotated {with_desc} of {n_mined} mined identifiers", file=sys.stderr)

    out = args.out or args.dossiers
    common.write_json(out, doss)
    print(f"gathered candidates for {done} nodes -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

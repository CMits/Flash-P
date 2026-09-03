#!/usr/bin/env python3
"""Extract gene model identifiers stated in the papers Flash-P already cited.

Papers occasionally spell out the identifier behind a symbol -- "ACS2, Solyc01g095080;
ACS4, Solyc05g050010" or "OsDi19-1, LOC_Os05g48800". Where that happens it is the most
direct evidence available, because it is the authors of the very paper the network's edge
rests on saying which gene they mean.

It is not common: across the 58 evidence-bearing networks only about 1% of evidence
sentences and abstracts contain a recognisable identifier. So this is a corroborating
route, not a primary one, and the scoring below reflects that -- an identifier is only
treated as strong when it sits immediately beside the gene name.

Identifiers found in a foreign identifier system are kept too. A sorghum network quoting
"AT3G02260 (BIG)" has handed us an Arabidopsis anchor to project from, which is worth as
much as a native hit given how many node names are borrowed from other species.

Reads:  node_dossiers.json (from build_dossiers.py)
Writes: the same structure with ids_in_text populated
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# Identifier systems that appear in the literature but are not the current Ensembl
# reference for their species, so derived_pattern() cannot produce them. Kept short and
# explicit: each entry is a system a reader would recognise on sight.
LEGACY_SYSTEMS = {
    "sorghum_bicolor": [r"Sb\d{2}g\d{6}"],                 # Sbicolor v1.4
    "oryza_sativa": [r"Os\d{2}t\d{7}", r"LOC_Os\d{2}g\d{5}"],   # transcripts; MSU v7
    "zea_mays": [r"GRMZM\d[GT]\d{6}", r"AC\d{6}\.\d+_FG\d+",   # B73 v3
                 r"Zm00001d\d{6}"],                        # B73 v4, superseded by v5 Zm00001eb
}

# Alternative spellings of the *current* reference identifier, not a different release.
# Papers print AGI codes as "At4g31800" constantly; canonical_case() restores the casing and
# what comes out is the reference accession itself. Kept apart from LEGACY_SYSTEMS because
# only the latter marks a hit as needing conversion -- lumping the two together labelled
# every lowercase-spelled Arabidopsis accession in the corpus as a superseded release.
SPELLING_VARIANTS = {
    "arabidopsis_thaliana": [r"At\d[Gg]\d{5}"],
}

# How much weight an identifier gets, by how it sits relative to the gene name.
PROXIMITY_SCORE = {
    "appositive": 0.90,   # "PSY1 (Solyc03g031860)" or "PSY1, Solyc03g031860"
    # The same typography, but found by indexing the whole network's papers rather than by
    # walking this node's own evidence trail -- see harvest_pairings(). Scored below a
    # first-hand appositive because one extra assumption is being made: that the symbol the
    # paper apposed to the identifier is the same gene as the node carrying that symbol.
    # Still well above same_sentence, because the pairing itself is just as explicit.
    "appositive_by_symbol": 0.75,
    "same_sentence": 0.60,
    "same_paragraph": 0.30,
    "same_paper": 0.10,
}

WORD = r"[A-Za-z0-9][A-Za-z0-9._-]*"


def name_variants(dossier):
    """Every plausible spelling of this node's name, longest first.

    network.json uppercases node ids and replaces separators with underscores, so PHYA,
    NRT2_1 and FAPG1 must all be matched back to NRT2.1, NRT2-1, FaPG1 as written.
    """
    seen = []
    raw = [dossier["node_id"]] + list(dossier.get("literature_spellings", []))
    for name in raw:
        if not name:
            continue
        for v in {name, name.replace("_", "-"), name.replace("_", "."),
                  name.replace("_", " "), name.replace("_", "")}:
            if v and v not in seen:
                seen.append(v)
    return sorted(seen, key=len, reverse=True)


def build_panel(species_names):
    """Identifier patterns for every species in play, derived from the cache itself.

    Deriving the reference pattern from cache contents rather than hard-coding it is what
    keeps this usable for a species we have never seen.
    """
    sr = common._resolver()
    panel = []
    for token in species_names:
        info = common.classify_species(token)
        ens = info.get("ensembl_name")
        if not ens:
            continue
        pats = []
        try:
            ref, cov = sr.derived_pattern(ens)
            if ref and cov >= 0.5:
                pats.append(ref)
        except Exception:
            pass
        cur = sr._curated_for(ens) or {}
        pats.extend(cur.get("extra_id_patterns", []) or [])
        legacy = LEGACY_SYSTEMS.get(ens, [])
        pats.extend(legacy)
        pats.extend(SPELLING_VARIANTS.get(ens, []))
        if not pats:
            continue
        panel.append({
            "ensembl_name": ens,
            "binomial": info.get("binomial") or token,
            "regex": re.compile(r"\b(?:" + "|".join(f"(?:{p})" for p in pats) + r")\b"),
            # Whether a hit is written in the release the rest of the pipeline resolves
            # against. Every shape is tagged with the species' current system, so without
            # this a superseded accession is indistinguishable from a current one -- and
            # papers cite superseded ones constantly: across the mapped corpus, seventeen of
            # the twenty propagated pairings that appear to contradict an emitted answer are
            # the same gene written in an older release (Zm00001d for maize v4, GRMZM for v3,
            # Sb01g for sorghum v1.4), not a different gene.
            "legacy_regex": (re.compile(r"\b(?:" + "|".join(f"(?:{p})" for p in legacy) + r")\b")
                             if legacy else None),
        })
    # De-duplicate on ensembl_name, keeping the first (species order carries priority).
    out, seen = [], set()
    for p in panel:
        if p["ensembl_name"] not in seen:
            seen.add(p["ensembl_name"])
            out.append(p)
    return out


def find_ids(text, panel):
    """All identifier hits in one block of text, with their span and owning species."""
    hits = []
    for entry in panel:
        for m in entry["regex"].finditer(text):
            hits.append({
                # Respelled the way the species writes it. Papers print AGI codes as
                # "At4g31800" constantly, and Ensembl's REST endpoints are case-sensitive:
                # projecting the paper's spelling returns zero orthologs rather than an
                # error, so a perfectly good identifier is discarded without trace. Fixing
                # it here means every downstream step -- adjudication, projection, the
                # emitted mapping -- sees one spelling.
                "gene_id": common.canonical_case(m.group(0), entry["ensembl_name"]),
                "id_system": entry["ensembl_name"],
                "id_species": entry["binomial"],
                # "legacy" is not a lower-quality hit -- it is a correct identifier in a
                # superseded release, and needs converting before it can be compared with
                # anything the cache or the databases returned.
                "id_release": ("legacy" if entry["legacy_regex"]
                               and entry["legacy_regex"].fullmatch(m.group(0))
                               else "reference"),
                "start": m.start(),
                "end": m.end(),
            })
    return hits


CHUNK_SPLIT = re.compile(r"[;\n]|(?<=[a-z0-9])\.\s+(?=[A-Z])")


# A copy designator that papers append to a gene name in a polyploid: TaWRKY42-B, SGR-A1,
# Glu-D1, TaNAC-3B. It denotes which subgenome copy is meant, not a different gene, so a
# node called TAWRKY42 must still match "TaWRKY42-B" -- otherwise the paper's own statement
# "TaWRKY42-B (TraesCS2B02G187500)" is classed same_paper and the accession is never seen.
#
# Deliberately narrow. Only a subgenome letter qualifies, optionally preceded by a
# chromosome-group digit and followed by a copy number. A bare digit does not: allowing it
# would let NRT2 swallow NRT2-1, which is a different gene. That is the same distinction the
# lookahead below exists to protect, kept intact.
COPY_SUFFIX = r"(?:[-_.](?:[1-9])?[ABDU](?:[1-9])?)?"

# Species prefixes papers put in front of a symbol: TaWRKY42, OsNAC2, AtPIF4. The word
# boundary sits before the prefix, so without this a node called NYC1 never matches the
# paper's "TaNYC1" at all.
#
# This part is matched case-sensitively and the rest is not, which is the whole reason it is
# safe. Capital-then-lower-case followed by an upper-case or digit is the shape of a species
# code and very little else; matched case-insensitively it degenerates into "any two letters"
# and a node called RIN starts matching CURIN.
SPECIES_PREFIX = r"(?:[A-Z][a-z](?=[A-Z0-9]))?"


def name_regex(v):
    """A node-name variant as papers actually write it: optional species prefix, optional
    subgenome copy suffix, and the guard that stops a short name matching a longer one.

    Callers must not pass re.IGNORECASE -- the case-insensitive part is scoped inside.
    """
    return (r"\b" + SPECIES_PREFIX + r"(?i:" + re.escape(v) + COPY_SUFFIX + r")"
            + r"(?![-_.]?[A-Za-z0-9])")


def _name_in(text, variants):
    """Longest node-name variant occurring in text as a whole name.

    The negative lookahead stops a short name matching the head of a longer one -- without
    it "NOR" matches inside "NOR-like1", which is a different gene with a different
    accession sitting in the same list.
    """
    for v in variants:
        if len(v) < 2:
            continue
        if re.search(name_regex(v), text):
            return v
    return None


# What may sit between a name and its identifier for the pairing to be real.
# Forward ("PSY1, Solyc03g031860" / "ZEP (Solyc04g051190"): whitespace, an opening bracket,
# a comma or a colon. Reverse ("Solyc01g095080 (ACS2)"): a closing bracket then an opening
# one. A semicolon is excluded in both directions because it is what ends a list entry --
# in "PSY1, Solyc03g031860; GGPPS2, Solyc04g079960" the identifier before the semicolon
# belongs to PSY1, and allowing the gap to cross it hands it to GGPPS2 instead.
#
# A slash-run of synonyms may also intervene. Papers write "ORE1/NAC2/ANAC092 (AT5G39610)"
# and "NYE1/SGR (AT4G22920)", where the accession belongs to every alias in the run. Without
# this the pairing is missed for all but the last alias, and the node falls back to a
# same-sentence hit -- which in an accession list is somebody else's identifier.
ALIAS_RUN = r"(?:/[A-Za-z][A-Za-z0-9._-]{0,14})*"
FORWARD_GAP = re.compile(r"^(" + ALIAS_RUN + r")\s*[\(\[]?\s*(?:[:,]\s*)?[\(\[]?\s*$")
REVERSE_GAP = re.compile(r"^\s*[\)\]]?\s*[\(\[]?\s*$")


def _apposition(text, hit, variants):
    """Distance to a name that is genuinely apposed to this identifier, else None.

    Direction matters and so does what lies between. "PSY1, Solyc03g031860" is a pairing;
    "Solyc03g031860; GGPPS2" is two list entries that happen to be adjacent.
    """
    best = None
    for v in variants:
        if len(v) < 2:
            continue
        for m in re.finditer(name_regex(v), text):
            aliases = 0
            if m.end() <= hit["start"]:
                between = text[m.end():hit["start"]]
                fm = FORWARD_GAP.match(between)
                aliases = len(fm.group(1)) if fm else 0
                ok = fm and (len(between) - aliases) <= 4
            elif m.start() >= hit["end"]:
                between = text[hit["end"]:m.start()]
                ok = len(between) <= 4 and REVERSE_GAP.match(between)
            else:
                between, ok = "", True
            if ok:
                # Rank on the gap excluding the synonym run, so a directly adjacent pairing
                # still beats one reached across a chain of aliases.
                d = len(between) - aliases
                best = d if best is None else min(best, d)
    return best


def _nearest(text, hit, variants):
    """Plain character distance, used only to rank hits of equal class."""
    best = None
    for v in variants:
        if len(v) < 2:
            continue
        for m in re.finditer(name_regex(v), text):
            d = (hit["start"] - m.end()) if m.end() <= hit["start"] else (
                m.start() - hit["end"] if m.start() >= hit["end"] else 0)
            best = d if best is None else min(best, max(d, 0))
    return best if best is not None else 999


def proximity(text, hit, variants):
    """Classify how tightly an identifier is bound to the gene name.

    Two situations produce a trustworthy pairing. The first is direct apposition --
    "ZEP (Solyc04g051190)" -- where nothing but a bracket separates the two. The second is
    an accession list, "ACS2, Solyc01g095080; ACS4, Solyc05g050010; NAC-NOR (NOR),
    Solyc10g006880", which papers put in a data-availability section.

    Lists are parsed structurally rather than by looking for a name near an identifier. A
    proximity window cannot tell "PG2a, Solyc10g080210; NOR-like1" apart from a genuine
    pairing, and silently hands NOR the identifier belonging to PG2a. Splitting on the
    separators the list itself uses, then requiring one identifier per chunk, removes that
    whole class of error.

    Returns (class, gap); gap breaks ties between equally-classified hits so that the
    adjacent pairing wins over a name that merely shares the chunk.
    """
    apposed = _apposition(text, hit, variants)
    gap = _nearest(text, hit, variants)

    # Direct apposition: nothing but a bracket, comma or colon separates the two.
    if apposed is not None:
        return "appositive", apposed

    bounds, pos = [], 0
    for m in CHUNK_SPLIT.finditer(text):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(text)))
    chunk_span = next(((a, b) for a, b in bounds if a <= hit["start"] < b), None)
    if chunk_span:
        a, b = chunk_span
        chunk = text[a:b]
        n_ids = sum(len(e["regex"].findall(chunk)) for e in (hit.get("_panel") or []))
        # The one-identifier-per-chunk rule is only safe when the name is actually near
        # the identifier. A "chunk" spanning a whole paragraph would otherwise pair a name
        # with an accession hundreds of characters away.
        if n_ids == 1 and gap <= 120 and _name_in(chunk, variants):
            return "appositive", gap

    sent_lo = text.rfind(".", 0, hit["start"]) + 1
    sent_hi = text.find(".", hit["end"])
    sentence = text[sent_lo: sent_hi if sent_hi > 0 else len(text)]
    if _name_in(sentence, variants):
        return "same_sentence", gap

    lo = max(0, hit["start"] - 400)
    hi = min(len(text), hit["end"] + 400)
    if _name_in(text[lo:hi], variants):
        return "same_paragraph", gap
    return "same_paper", gap


def snippet(text, hit, pad=110):
    lo = max(0, hit["start"] - pad)
    hi = min(len(text), hit["end"] + pad)
    return ("..." if lo else "") + re.sub(r"\s+", " ", text[lo:hi]).strip() + ("..." if hi < len(text) else "")


# ----------------------------------------------------------------------------------------
# Corpus-wide pairings.
#
# Everything above answers "is this node's name beside this identifier", asked separately
# for each node against only the papers that node cites. That is the wrong unit of work.
# A paper's data-availability section names a dozen genes and their accessions at once, but
# the pairings it establishes reach only the nodes that happen to cite it. Measured on
# Lycopene_Content_In_Tomato, the corpus prints "SlPIF1a (Solyc09g063010)" and "FUL1
# (Solyc06g069430) and FUL2 (Solyc03g114830)" verbatim -- the correct answers for two nodes
# that were reported unresolved -- because the papers stating them are cited by PSY1 and
# MYC2 instead.
#
# So the pairing is harvested once for the whole network, keyed on the symbol the paper
# itself apposed to the identifier, and then offered to whichever node carries that symbol.
#
# Only appositive pairings propagate. That restriction is the whole safety argument. The
# looser classes are contaminated by exactly the construct this fixes: one accession-list
# sentence makes every accession in it same_sentence to every gene named in it, which on
# tomato produced 57 candidates of which zero were correct. Apposition is decided per list
# entry, so the entry "ACS2, Solyc01g095080" hands its identifier to ACS2 and to nothing
# else, however many other genes share the sentence.

LABEL = r"[A-Za-z][A-Za-z0-9._-]{1,23}"
# Papers write runs of synonyms for one gene: "ORE1/NAC2/ANAC092 (AT5G39610)". The
# identifier belongs to every name in the run, so the run is captured whole and split after.
ALIAS_LABEL = LABEL + r"(?:/" + LABEL + r")*"

# The structural forms a pairing takes, with the same gap rules _apposition() uses in the
# other direction: a comma, colon or bracket may sit between the name and the identifier,
# and nothing else.
FORWARD_PAIR = re.compile(
    r"(?:^|[\s(\[,;:])(?P<label>" + ALIAS_LABEL + r")"
    r"(?P<gap>\s*[\(\[]?\s*(?:[:,]\s*)?[\(\[]?\s*)$")
REVERSE_BRACKET = re.compile(
    r"^(?P<gap>\s*[\)\]]?\s*[\(\[]\s*)(?P<label>" + ALIAS_LABEL + r")\s*[\)\]]")
# Some papers write the list the other way round: "At5g45900 for ATG7, At1g64280 for NPR1,
# At2g39940 for COI1". Read forwards, every name in such a list binds to the *next*
# accession, which on Stay_Green_In_Sorghum produced four confidently wrong pairings -- each
# one the neighbouring gene's identifier. An explicit connective is what marks the direction,
# so it is matched here and, below, given precedence over bare adjacency.
REVERSE_CONNECTIVE = re.compile(
    r"^(?P<gap>[\s,;:]*(?:for|=)\s+)(?P<label>" + ALIAS_LABEL + r")\b")
# Fallback for a list entry whose name is not adjacent: "NAC-NOR (NOR), Solyc10g006880".
CHUNK_HEAD = re.compile(r"^\W*(?P<label>" + ALIAS_LABEL + r")")

MAX_GAP = 4          # characters between name and identifier for an apposition
CHUNK_GAP = 120      # how far the head of a one-identifier list entry may sit from it

# Words that turn up where a gene symbol would, in text that is otherwise shaped like a
# pairing. None of them is a plausible node name, so the cost of the list is nil and it
# keeps the index readable when it is inspected by hand.
NOT_A_SYMBOL = {
    "gene", "genes", "locus", "loci", "id", "ids", "accession", "accessions", "protein",
    "proteins", "the", "and", "or", "of", "in", "at", "by", "for", "with", "from", "to",
    "cv", "cultivar", "fig", "figure", "table", "supplementary", "data", "no", "see",
    "respectively", "gene_id", "geneid", "number", "numbers", "version", "chr",
}

COPY_TAIL = re.compile(r"[-_.](?:[1-9])?[ABDU](?:[1-9])?$")


def match_keys(name):
    """Every normalised form under which one spelling of a symbol may be indexed.

    A paper's "SlPIF1a" and a node's "PIF1A" are the same gene, and so are "TaWRKY42-B" and
    "TAWRKY42". Rather than run the tolerant name_regex() over every label in the corpus for
    every node -- which is the same comparison done hundreds of thousands of times -- the two
    tolerances name_regex() grants (a species prefix, a subgenome copy suffix) are applied
    here to produce a small set of exact keys, and matching becomes a set intersection.

    Separators are dropped, so a node written NRT2_1 meets a paper's NRT2.1.
    """
    out = set()
    for s in {name, common.strip_species_prefix(name) or name}:
        for t in {s, COPY_TAIL.sub("", s)}:
            k = re.sub(r"[^A-Z0-9]", "", (t or "").upper())
            if len(k) >= 2:
                out.add(k)
    return out


def component_keys(node):
    """Keys for the individual genes named by a joined node id: FUL1_FUL2, BTR1_BTR2.

    Flash-P writes a two-gene node as one underscore-joined name, and no paper ever prints
    that string, so such a node can never match a pairing under its own name. Matching its
    parts is what lets "FUL1 (Solyc06g069430)" and "FUL2 (Solyc03g114830)" reach it.

    Guarded so that a single gene whose name merely contains an underscore is left alone:
    every part must be at least three characters and carry a digit. NAM_B1 is one gene
    written with a subgenome suffix, and splitting it would look for a gene called B1.
    """
    parts = [p for p in (node.get("node_id") or "").split("_") if p]
    if len(parts) < 2 or not all(
            len(p) >= 3 and re.search(r"\d", p) and re.search(r"[A-Za-z]", p) for p in parts):
        return set()
    out = set()
    for p in parts:
        out |= match_keys(p)
    return out


def _is_identifier(label, panel):
    return any(e["regex"].fullmatch(label) for e in panel)


def _plausible_label(label, panel):
    """Does this token read as a gene symbol rather than as ordinary prose?

    Nothing here knows which node is asking, so a token that is merely a word becomes an index
    key rather than failing to match a name. Requiring a digit or full capitalisation is what
    separates PSY1, NAC-NOR and SlPIF1a from the prose that surrounds them: without it "the
    transcript Solyc03g031860" files that accession under "transcript", and "encodes a
    phytoene synthase" files one under "phytoene".

    The test is applied to the symbol without its organism tag, or SlPHYA, AtNAP and OsSGR --
    a species prefix on an all-capital symbol that happens to carry no number -- would fail
    it. Ordinary words are unharmed by that: "tomato" and "Overexpression" are not shaped
    like a prefixed symbol, so nothing is stripped from them and they still fail.
    """
    if len(label) < 2 or not re.search(r"[A-Za-z]", label):
        return False
    if label.lower() in NOT_A_SYMBOL or _is_identifier(label, panel):
        return False
    core = common.strip_species_prefix(label) or label
    return re.search(r"\d", core) is not None or core.upper() == core


def _apposed_labels(text, hit, panel):
    """The gene symbol(s) a paper apposes to this identifier, with the gap in characters.

    The mirror image of _apposition(): that asks whether a known name sits beside the
    identifier, this asks which name does, so the answer can be found once and then offered
    to whichever node carries it.
    """
    # Order matters, and it is the whole defence against reading a list backwards. A bracket
    # or an explicit "for" is the paper asserting the pairing; bare adjacency across a comma
    # is only typography, and in a reversed list it is somebody else's identifier. So a
    # name bound by punctuation wins, and the forward rule is consulted only when no such
    # binding exists.
    runs = []
    post = text[hit["end"]: hit["end"] + 48]
    m = REVERSE_BRACKET.match(post)
    if m and len(m.group("gap")) <= MAX_GAP:
        runs.append((m.group("label"), len(m.group("gap"))))
    if not runs:
        m = REVERSE_CONNECTIVE.match(post)
        if m:
            runs.append((m.group("label"), len(m.group("gap"))))
    if not runs:
        m = FORWARD_PAIR.search(text[max(0, hit["start"] - 80): hit["start"]])
        if m and len(m.group("gap")) <= MAX_GAP:
            runs.append((m.group("label"), len(m.group("gap"))))

    if not runs:
        # No name is bound to the identifier by punctuation. It may still be a list entry
        # whose name leads the entry: "NAC-NOR (NOR), Solyc10g006880". The one-identifier
        # rule is what makes that safe -- an entry naming two accessions is not a pairing.
        bounds, pos = [], 0
        for sep in CHUNK_SPLIT.finditer(text):
            bounds.append((pos, sep.start()))
            pos = sep.end()
        bounds.append((pos, len(text)))
        span = next(((a, b) for a, b in bounds if a <= hit["start"] < b), None)
        if span:
            a, b = span
            chunk = text[a:b]
            if sum(len(e["regex"].findall(chunk)) for e in panel) == 1:
                m = CHUNK_HEAD.match(chunk)
                if m and (hit["start"] - a - m.end()) <= CHUNK_GAP:
                    runs.append((m.group("label"), hit["start"] - a - m.end()))

    out = []
    for run, gap in runs:
        for lab in run.split("/"):
            lab = lab.strip(" .,-_")
            if lab and _plausible_label(lab, panel):
                out.append((lab, max(gap, 0)))
    return out


def harvest_pairings(texts, panel):
    """Index every appositive name-identifier pairing in the network's papers, by symbol.

    Returns (index, n), where index maps a normalised symbol key to the identifiers apposed
    to that symbol anywhere in the corpus. Nothing here knows about nodes; the join happens
    in mine(), so a pairing is available to any node whose name matches, not only to the
    nodes that cite the paper it came from.
    """
    index = collections.defaultdict(dict)
    n = 0
    for text, doi, source in texts:
        if not text:
            continue
        for hit in find_ids(text, panel):
            for label, gap in _apposed_labels(text, hit, panel):
                rec = {
                    "gene_id": hit["gene_id"],
                    "id_system": hit["id_system"],
                    "id_species": hit["id_species"],
                    "id_release": hit["id_release"],
                    "label": label,
                    "gap": gap,
                    "doi": doi,
                    "source": source,
                    "snippet": snippet(text, hit),
                }
                key = (hit["gene_id"], hit["id_system"])
                for k in match_keys(label):
                    prev = index[k].get(key)
                    if prev is None or gap < prev["gap"]:
                        index[k][key] = rec
                n += 1
    return index, n


def mine(doss, network_dir, use_fulltext=True, max_fulltext_chars=400000, extra_species=(),
         propagate=True):
    summary = doss["summary"]
    net_species = summary["network_species"]

    # Species order matters: the network's own system first, then wherever the names come
    # from, then anything else the evidence touched.
    #
    # `extra_species` widens that panel by hand. It exists for evidence that carries no
    # species tags -- a network backfilled from a pre-Step-1.6 build, where the legacy files
    # record a DOI but never which organism the claim was made in. The panel then collapses
    # to the network's own species and every foreign accession in the papers is invisible,
    # not because it is absent but because nothing is looking for its shape. Widening it is
    # a stated judgement about what a set of papers discusses, so it is a flag rather than
    # something inferred: reading species out of the sentences is exactly how tags like
    # "Arabidopsis hub" and "Sorghum stay" come into existence.
    species_order = [net_species]
    species_order.extend(extra_species or ())
    for n in doss["nodes"]:
        if n.get("name_origin_species"):
            species_order.append(n["name_origin_species"])
    for n in doss["nodes"]:
        species_order.extend(n.get("evidence_species", {}).keys())
    seen = set()
    ordered = [s for s in species_order if s and not (s in seen or seen.add(s))]
    panel = build_panel(ordered)

    net_ens = common.classify_species(net_species).get("ensembl_name")

    ft_dir = os.path.join(network_dir, "data", "fulltext")
    ft_cache = {}

    def fulltext(fname):
        if not use_fulltext or not fname:
            return ""
        if fname not in ft_cache:
            path = os.path.join(network_dir, "data", fname)
            try:
                with open(path, errors="replace") as fh:
                    ft_cache[fname] = fh.read(max_fulltext_chars)
            except Exception:
                ft_cache[fname] = ""
        return ft_cache[fname]

    # One pass over every distinct block of text in the network, before any node is
    # considered, so that a pairing stated in one paper is available to every node whose
    # name it mentions rather than only to the nodes citing that paper.
    index, n_pairings = {}, 0
    if propagate:
        texts, seen_text, seen_file = [], set(), set()
        for node in doss["nodes"]:
            for s in node.get("sentences", []):
                t = s.get("text", "")
                if t and t not in seen_text:
                    seen_text.add(t)
                    texts.append((t, s.get("doi", ""), "evidence_sentence"))
            for p in node.get("papers", []):
                f = p.get("fulltext_file") or ""
                if p.get("has_fulltext") and f and f not in seen_file:
                    seen_file.add(f)
                    texts.append((fulltext(f), p.get("doi", ""), "fulltext"))
        index, n_pairings = harvest_pairings(texts, panel)

    stats = collections.Counter()
    for node in doss["nodes"]:
        if not node["mappable"]:
            continue
        variants = name_variants(node)
        found = {}

        def absorb(text, doi, source):
            if not text:
                return
            for hit in find_ids(text, panel):
                hit["_panel"] = panel
                prox, gap = proximity(text, hit, variants)
                score = PROXIMITY_SCORE[prox]
                key = (hit["gene_id"], hit["id_system"])
                prev = found.get(key)
                if prev and (prev["score"], -prev["gap"]) >= (score, -gap):
                    continue
                found[key] = {
                    "gene_id": hit["gene_id"],
                    "id_system": hit["id_system"],
                    "id_species": hit["id_species"],
                    "id_release": hit["id_release"],
                    "relation_to_target": ("native" if hit["id_system"] == net_ens else "anchor"),
                    "proximity": prox,
                    "score": score,
                    "gap": gap,
                    "doi": doi,
                    "source": source,
                    "snippet": snippet(text, hit),
                }

        for s in node.get("sentences", []):
            absorb(s.get("text", ""), s.get("doi", ""), "evidence_sentence")
        for p in node.get("papers", []):
            if p.get("has_fulltext") and p.get("fulltext_file"):
                absorb(fulltext(p["fulltext_file"]), p.get("doi", ""), "fulltext")

        # Pairings the corpus states for this node's symbol, wherever they were stated. A
        # first-hand hit always wins -- the score test below never lets a propagated pairing
        # displace one found in the node's own evidence -- so this can only add.
        if index:
            cited = {p.get("doi") for p in node.get("papers", []) if p.get("doi")}
            keys = set()
            for v in variants:
                keys |= match_keys(v)
            keys |= component_keys(node)
            score = PROXIMITY_SCORE["appositive_by_symbol"]
            added = 0
            for k in sorted(keys):
                for key, rec in sorted(index.get(k, {}).items()):
                    prev = found.get(key)
                    if prev and prev["score"] >= score:
                        continue
                    found[key] = {
                        "gene_id": rec["gene_id"],
                        "id_system": rec["id_system"],
                        "id_species": rec["id_species"],
                        "id_release": rec["id_release"],
                        "relation_to_target": ("native" if rec["id_system"] == net_ens
                                               else "anchor"),
                        "proximity": "appositive_by_symbol",
                        "score": score,
                        "gap": rec["gap"],
                        "doi": rec["doi"],
                        "source": rec["source"],
                        # What the paper actually wrote. For a joined node this is the
                        # component -- FUL1 against a node called FUL1_FUL2 -- which is the
                        # difference between a complex member and the node's whole answer.
                        "matched_label": rec["label"],
                        "cited_by_node": rec["doi"] in cited,
                        "snippet": rec["snippet"],
                    }
                    added += 1
            if added:
                stats["propagated_pairings"] += added
                stats["nodes_with_propagated_pairing"] += 1

        # Rank by strength, then by how close the name sits to the identifier: an adjacent
        # "GGPPS2, Solyc04g079960" must beat an identifier that merely shares its sentence.
        ids = sorted(found.values(), key=lambda h: (-h["score"], h["gap"], h["gene_id"]))
        node["ids_in_text"] = ids[:20]
        for h in ids:
            stats[h["proximity"]] += 1
            if h["id_release"] == "legacy":
                stats["legacy_release_ids"] += 1
        if ids:
            stats["nodes_with_any_id"] += 1
            if any(h["score"] >= PROXIMITY_SCORE["same_sentence"] for h in ids):
                stats["nodes_with_strong_id"] += 1

    summary["id_mining"] = {
        "panel_species": [p["ensembl_name"] for p in panel],
        "n_fulltext_read": len(ft_cache),
        "corpus_pairings_indexed": n_pairings,
        **{k: v for k, v in stats.items()},
    }
    return doss


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", required=True, help="node_dossiers.json from build_dossiers.py")
    ap.add_argument("--out", help="output path (default: overwrite --dossiers in place)")
    ap.add_argument("--species", default="",
                    help="comma-separated extra species to add to the identifier panel, for "
                         "evidence that carries no species tags of its own")
    ap.add_argument("--no-propagate", action="store_true",
                    help="do not offer a pairing stated in one paper to nodes that cite a "
                         "different one; each node then sees only its own evidence trail")
    ap.add_argument("--no-fulltext", action="store_true",
                    help="scan only evidence sentences, not cached full texts")
    args = ap.parse_args()

    doss = common.load_json(args.dossiers)
    network_dir = doss["summary"]["network_dir"]
    extra = [x.strip() for x in args.species.split(",") if x.strip()]
    doss = mine(doss, network_dir, use_fulltext=not args.no_fulltext, extra_species=extra,
                propagate=not args.no_propagate)
    if extra:
        doss["summary"].setdefault("id_mining", {})["panel_widened_by_hand"] = extra

    out = args.out or args.dossiers
    common.write_json(out, doss)
    print(json.dumps(doss["summary"]["id_mining"], indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()

"""
Which organism a piece of evidence actually came from.

A FLASH-P network is built for one species, but the literature it is built from often
is not. In the wheat salinity network, 20 of 39 perturbation tests are *Arabidopsis*
and only 12 are wheat — a fact that changes how much a reader should trust the model,
and which was invisible because nothing surfaced it. Perturbation tests carry a curated
``species`` field; edges do not, so for those the organism has to be read back out of
the paper the claim was grounded in.

Detection is deliberately conservative — a wrong species is worse than a blank one:

  * a binomial is only accepted when the genus is one we know, so "Arabidopsis
    mitogen-activated" cannot become a species named *Arabidopsis mitogen*;
  * a genus on its own resolves to its type species only where that is unambiguous in
    this literature (*Arabidopsis* -> *A. thaliana*), never for genera like *Triticum*
    or *Solanum* where several species are studied side by side;
  * common names map to binomials only as whole words, so "rice" matches and "price"
    does not.

The supporting sentence is asked first, then the title, then the abstract: the closer
the mention is to the sentence that carries the claim, the more likely it describes the
experiment rather than a comparison in the discussion.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

__all__ = ["detect_species", "same_species", "short_species"]

# Genera whose binomials we accept. Anything outside this list is not a species name as
# far as this module is concerned, which is what stops adjectives being read as epithets.
_GENERA = {
    # plants
    "arabidopsis", "triticum", "aegilops", "hordeum", "oryza", "zea", "sorghum",
    "brachypodium", "setaria", "solanum", "nicotiana", "glycine", "medicago",
    "lotus", "phaseolus", "pisum", "vigna", "cicer", "brassica", "raphanus",
    "camelina", "gossypium", "cucumis", "cucurbita", "citrullus", "vitis", "malus",
    "prunus", "citrus", "populus", "eucalyptus", "pinus", "picea", "physcomitrella",
    "physcomitrium", "marchantia", "chlamydomonas", "selaginella", "amborella",
    "eutrema", "thellungiella", "salicornia", "mesembryanthemum", "beta", "spinacia",
    "helianthus", "lactuca", "daucus", "capsicum", "fragaria", "musa", "manihot",
    "ipomoea", "saccharum", "panicum", "festuca", "lolium", "trifolium", "hevea",
    "theobroma", "coffea", "camellia", "olea", "phoenix", "elaeis", "ananas",
    # fungi / microbes used as hosts or heterologous systems
    "saccharomyces", "schizosaccharomyces", "pichia", "escherichia", "agrobacterium",
    "pseudomonas", "bacillus", "synechocystis",
    # animals (Medical/Animal editions share this module)
    "homo", "mus", "rattus", "danio", "drosophila", "caenorhabditis", "xenopus",
    "gallus", "bos", "sus", "ovis", "canis", "felis", "equus", "macaca", "oryctolagus",
}

# A genus alone is only resolved when one species dominates this literature so heavily
# that the bare genus is unambiguous. Triticum, Solanum, Brassica and friends are
# deliberately absent: "Triticum" could be aestivum, turgidum or monococcum, and
# guessing would invent provenance.
_GENUS_ONLY = {
    "arabidopsis": "Arabidopsis thaliana",
    "chlamydomonas": "Chlamydomonas reinhardtii",
    "physcomitrella": "Physcomitrium patens",
    "marchantia": "Marchantia polymorpha",
    "brachypodium": "Brachypodium distachyon",
    "drosophila": "Drosophila melanogaster",
    "caenorhabditis": "Caenorhabditis elegans",
    "saccharomyces": "Saccharomyces cerevisiae",
    "escherichia": "Escherichia coli",
}

# Whole-word common names. Ordered longest-first at match time so "bread wheat" wins
# over "wheat" and "durum wheat" is not read as bread wheat.
_COMMON: Dict[str, str] = {
    "bread wheat": "Triticum aestivum",
    "common wheat": "Triticum aestivum",
    "durum wheat": "Triticum turgidum",
    "wheat": "Triticum aestivum",
    "thale cress": "Arabidopsis thaliana",
    "rice": "Oryza sativa",
    "maize": "Zea mays",
    "corn": "Zea mays",
    "barley": "Hordeum vulgare",
    "tomato": "Solanum lycopersicum",
    "potato": "Solanum tuberosum",
    "soybean": "Glycine max",
    "soyabean": "Glycine max",
    "cotton": "Gossypium hirsutum",
    "cucumber": "Cucumis sativus",
    "tobacco": "Nicotiana tabacum",
    "grapevine": "Vitis vinifera",
    "poplar": "Populus",
    "sorghum": "Sorghum bicolor",
    "sugarcane": "Saccharum officinarum",
    "cassava": "Manihot esculenta",
    "sunflower": "Helianthus annuus",
    "rapeseed": "Brassica napus",
    "oilseed rape": "Brassica napus",
    "canola": "Brassica napus",
    "baker's yeast": "Saccharomyces cerevisiae",
    "budding yeast": "Saccharomyces cerevisiae",
    "fission yeast": "Schizosaccharomyces pombe",
    "human": "Homo sapiens",
    "mouse": "Mus musculus",
    "murine": "Mus musculus",
    "rat": "Rattus norvegicus",
    "zebrafish": "Danio rerio",
    "fruit fly": "Drosophila melanogaster",
    "chicken": "Gallus gallus",
    "rabbit": "Oryctolagus cuniculus",
    "cattle": "Bos taurus",
    "bovine": "Bos taurus",
    "porcine": "Sus scrofa",
}

_BINOMIAL = re.compile(r"\b([A-Z][a-z]{2,})\.?\s+([a-z]{3,})\b")
_COMMON_KEYS: Sequence[str] = sorted(_COMMON, key=len, reverse=True)

# A genus is followed by an English word far more often than by its epithet —
# "Arabidopsis mitogen-activated", "Triticum plants", "Oryza genes". Without this the
# binomial matcher invents species like *Arabidopsis mitogen*. Rejecting the match and
# continuing lets the genus rule downgrade "Arabidopsis mitogen-activated…" to plain
# A. thaliana instead of fabricating an organism.
_NOT_EPITHETS = {
    "and", "are", "the", "was", "were", "has", "have", "had", "its", "his", "her",
    "plant", "plants", "mutant", "mutants", "gene", "genes", "genome", "genomic",
    "protein", "proteins", "seedling", "seedlings", "root", "roots", "shoot", "shoots",
    "leaf", "leaves", "cell", "cells", "line", "lines", "ecotype", "accession",
    "accessions", "homolog", "homologue", "homologs", "ortholog", "orthologue",
    "orthologs", "mitogen", "vacuolar", "plasma", "cytosolic", "transgenic", "wild",
    "type", "salt", "stress", "tolerance", "response", "responses", "expression",
    "transcription", "kinase", "receptor", "transporter", "family", "growth",
    "development", "seed", "seeds", "pollen", "flower", "flowers", "study", "studies",
    "research", "data", "results", "using", "under", "with", "from", "that", "this",
    "which", "also", "both", "such", "more", "most", "other", "than", "when", "after",
    "before", "during", "into", "onto", "over", "shows", "showed", "reveals",
    "revealed", "encodes", "encoding", "contains", "carries", "requires", "confers",
    "produces", "exhibits", "displays", "roots", "tissue", "tissues", "genotype",
    "genotypes", "cultivar", "cultivars", "variety", "varieties", "species",
}


def _from_binomial(text: str) -> Optional[str]:
    for m in _BINOMIAL.finditer(text):
        genus, epithet = m.group(1), m.group(2)
        if genus.lower() in _GENERA and epithet not in _NOT_EPITHETS:
            return f"{genus} {epithet}"
    return None


def _from_genus(text: str) -> Optional[str]:
    low = text.lower()
    for genus, full in _GENUS_ONLY.items():
        if re.search(r"\b" + genus + r"\b", low):
            return full
    return None


def _from_common(text: str) -> Optional[str]:
    low = text.lower()
    for name in _COMMON_KEYS:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return _COMMON[name]
    return None


def detect_species(*texts: str) -> str:
    """Best-guess organism for a claim, from its sources in order of closeness.

    Each text is tried in full — binomial, then bare genus, then common name — before
    moving to the next, so a weaker signal in the supporting sentence still beats a
    stronger one in the abstract. Returns '' when nothing is confident enough, which is
    the honest answer and must not be rendered as a species.
    """
    for text in texts:
        if not text:
            continue
        for finder in (_from_binomial, _from_genus, _from_common):
            hit = finder(text)
            if hit:
                return hit
    return ""


def _norm(name: str) -> str:
    """Comparable form: lowercase, no parenthetical gloss, no qualifier tail."""
    s = re.sub(r"\([^)]*\)", " ", str(name or "")).lower()
    s = re.sub(r"\b(heterologous|grafted|ssp\.?|subsp\.?|var\.?|cv\.?)\b", " ", s)
    s = re.sub(r"[^a-z ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_species(a: str, b: str) -> bool:
    """Do these two names describe the same organism, loosely enough to be useful?

    Genus-level agreement counts: a claim from *Triticum turgidum* in a *Triticum
    aestivum* network is close enough that flagging it as foreign would cry wolf, while
    *Arabidopsis* in a wheat network is exactly what a reader needs to see.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return True                     # unknown is not a mismatch; say nothing
    if na == nb:
        return True
    ga, gb = na.split(" ")[0], nb.split(" ")[0]
    return bool(ga) and ga == gb


def short_species(name: str) -> str:
    """*Triticum aestivum (wheat)* -> *T. aestivum*, for a table cell."""
    s = re.sub(r"\s*\([^)]*\)", "", str(name or "")).strip()
    parts = s.split()
    if len(parts) >= 2 and parts[0][:1].isupper():
        return f"{parts[0][0]}. {parts[1]}"
    return s


if __name__ == "__main__":
    cases = [
        ("The Arabidopsis thaliana SOS2 and SOS3 genes are required for tolerance.",
         "Arabidopsis thaliana"),
        ("Arabidopsis mitogen-activated protein kinase 4 regulates defence.",
         "Arabidopsis thaliana"),          # genus resolves; the adjective must not
        ("Wheat grain yield on saline soils is improved by an ancestral gene.",
         "Triticum aestivum"),
        ("TmHKT1;5-A was crossed into durum wheat.", "Triticum turgidum"),
        ("Expression was measured in Oryza sativa roots.", "Oryza sativa"),
        ("A general model of ion transport.", ""),
        ("Overexpression in Nicotiana benthamiana leaves.", "Nicotiana benthamiana"),
    ]
    ok = True
    for text, want in cases:
        got = detect_species(text)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {flag} {got!r:<28} <- {text[:56]}")

    pairs = [("Triticum aestivum (wheat)", "Triticum turgidum durum", True),
             ("Triticum aestivum", "Arabidopsis thaliana", False),
             ("Arabidopsis thaliana (heterologous)", "Arabidopsis thaliana", True),
             ("", "Arabidopsis thaliana", True)]
    for a, b, want in pairs:
        got = same_species(a, b)
        if got != want:
            ok = False
            print(f"  FAIL same_species({a!r}, {b!r}) = {got}, want {want}")
    print(f"  short: {short_species('Triticum aestivum (wheat)')}")
    print("species self-test:", "OK" if ok else "FAILED")

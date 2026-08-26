"""
Which organism a piece of evidence actually came from.

A FLASH-P network is built for one species, but the literature it is built from often
is not. In the wheat salinity network, 20 of 39 perturbation tests are *Arabidopsis*
and only 12 are wheat — a fact that changes how much a reader should trust the model,
and which was invisible because nothing surfaced it. Perturbation tests carry a curated
``species`` field; edges do not, so for those the organism has to be read back out of
the paper the claim was grounded in.

Detection is deliberately conservative — a wrong species is worse than a blank one:

  * a binomial is only accepted when the genus AND the epithet are both on a known
    list — see ``_SPECIES`` below — so "Arabidopsis mitogen-activated" cannot become
    a species named *Arabidopsis mitogen*, nor "Citrus fruits" become *Citrus fruits*;
  * a genus on its own resolves to its type species only where that is unambiguous in
    this literature (*Arabidopsis* -> *A. thaliana*), never for genera like *Triticum*
    or *Solanum* where several species are studied side by side;
  * common names map to binomials only as whole words, so "rice" matches and "price"
    does not.

The supporting sentence is asked first, then the title, then the abstract: the closer
the mention is to the sentence that carries the claim, the more likely it describes the
experiment rather than a comparison in the discussion.

Which genus a network needs is not known in advance — a network built for a crop nobody
anticipated must not silently degrade to guessing. ``_SPECIES`` is only the *fast path*:
a genus not on it is looked up live in NCBI Taxonomy (``litapi.ncbi_taxonomy_species``,
same allowlist-not-denylist rule as the seed list) the first time it is met, and cached
in the run's ``Store`` so every later mention of it — this run and every run after —
answers from disk, not the network. Pass ``store=`` (and respect ``offline=``) to enable
this; without a store, an unseeded genus is simply unknown, exactly as before.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Sequence

try:
    from . import litapi
except ImportError:                                   # run directly as a script
    import litapi

__all__ = ["detect_species", "same_species", "short_species"]

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
    "rice bean": "Vigna umbellata",       # not Oryza — checked before bare "rice" below
    "rice": "Oryza sativa",
    "pigeon pea": "Cajanus cajan",        # not Pisum — checked before bare "pea" below
    "sweet pea": "Lathyrus odoratus",     # ornamental, not the edible pea
    "chickpea": "Cicer arietinum",
    "pea": "Pisum sativum",
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
    "strawberry": "Fragaria x ananassa",
    "mango": "Mangifera indica",
    "avocado": "Persea americana",
    "loquat": "Eriobotrya japonica",
    "macadamia": "Macadamia integrifolia",
    "peanut": "Arachis hypogaea",
    "groundnut": "Arachis hypogaea",
}

_BINOMIAL = re.compile(r"\b([A-Z][a-z]{2,})\.?\s+([a-z]{3,})\b")
_COMMON_KEYS: Sequence[str] = sorted(_COMMON, key=len, reverse=True)

# Non-plant animal genera. A plant-signaling paper's abstract routinely name-drops one of
# these as a discussion aside ("...a mechanism also described in humans...") without the
# paper being about that organism at all — unlike a plant/microbe common name, which is
# reliable wherever it appears because it is rarely used as a passing comparison. So
# these are only trusted when they come from the claim's own supporting sentence (the
# first text `detect_species` is given), never from the title/abstract fallback.
_ANIMAL_GENERA = {
    "homo", "mus", "rattus", "danio", "drosophila", "caenorhabditis", "xenopus",
    "gallus", "bos", "sus", "ovis", "canis", "felis", "equus", "macaca", "oryctolagus",
}


def _is_animal(species_name: str) -> bool:
    genus = species_name.split(" ", 1)[0].lower()
    return genus in _ANIMAL_GENERA


# A genus is followed by an English word far more often than by its epithet —
# "Arabidopsis mitogen-activated", "Triticum plants", "Oryza genes", "Citrus fruits".
# An earlier version of this module tried to reject epithets with a hand-built list of
# forbidden words; that is a denylist for an open-ended set (English prose) and it
# leaked constantly — "Arabidopsis counterpart", "Sorghum stay", "Populus stems" and
# dozens more slipped through as invented species (see git history for the incident).
# The only sound check is the other direction: accept an epithet only when it is on the
# list of species actually known for that genus. Anything not in this list is rejected
# and the genus rule (below) can still downgrade the mention to the bare genus's type
# species where that is unambiguous.
#
# The list itself comes from NCBI Taxonomy, not from memory: ``build_species_data.py``
# queries E-utils once per genus for every descendant at species rank and writes the
# result to ``species_data.json``, which is loaded below. That keeps the allowlist
# authoritative and re-generatable (add a genus to that script and re-run it) instead of
# permanently bounded by whatever species one person happened to type in by hand. Run
# the build script by hand when the genus list changes — it is not part of the pipeline.
_DATA_FILE = Path(__file__).parent / "species_data.json"
_SPECIES: Dict[str, set] = {
    g: set(eps) for g, eps in json.loads(_DATA_FILE.read_text())["genera"].items()
}

# A handful of genus names are still the standard usage in crop/plant genomics
# literature even though a molecular-phylogeny revision has folded them, in NCBI's
# current tree, into a different genus as synonyms — so an NCBI subtree search under
# the old name comes back empty (or full of the *other* genus's species) rather than
# giving a false answer. Patched in by hand since the authoritative source and the
# literature's working vocabulary disagree here.
_MANUAL_OVERRIDES: Dict[str, set] = {
    "pisum": {"sativum"},                          # NCBI: folded into Lathyrus
    "thellungiella": {"halophila", "salsuginea"},  # NCBI: folded into Eutrema
    "physcomitrella": {"patens"},                   # NCBI: folded into Physcomitrium
}
for _genus, _eps in _MANUAL_OVERRIDES.items():
    _SPECIES.setdefault(_genus, set()).update(_eps)
del _genus, _eps


# Purely a courtesy prefilter, not a correctness gate: skips the most common
# sentence-initial / discourse words ("The results showed...", "Here we demonstrate...")
# before even trying a live NCBI lookup for them, since none of them is ever a genus. A
# word missing from this list costs nothing beyond one wasted lookup — it still fails
# NCBI validation and gets cached as empty, exactly like any other non-genus — so this
# never needs to be exhaustive.
_UNLIKELY_GENUS = {
    "the", "this", "these", "those", "here", "there", "such", "both", "each",
    "several", "many", "some", "all", "most", "other", "another", "first", "second",
    "third", "data", "results", "analysis", "analyses", "study", "studies", "given",
    "since", "while", "although", "because", "therefore", "thus", "hence", "however",
    "moreover", "additionally", "importantly", "overall", "finally", "interestingly",
    "notably", "indeed", "specifically", "taken", "under", "following", "according",
    "compared", "unlike", "recent", "previous", "further", "additional", "together",
    "collectively", "consistent", "similar", "our", "author", "authors", "figure",
    "table", "supplementary", "materials", "methods", "conclusion", "abstract",
    "introduction", "discussion", "background",
}

# Live NCBI Taxonomy lookups are cheap but not free — this bounds the worst case (a
# paper whose text is mostly non-taxonomic capitalised prose) to a fixed number of extra
# round trips per process, rather than one per distinct capitalised word encountered.
# Genuinely novel genera are rare per run and get cached after their first lookup, so
# this cap is not expected to bind in ordinary use.
_LIVE_LOOKUP_CAP = 50
_live_lock = threading.Lock()
_live_lookup_count = 0


def _species_for(genus: str, store, offline: bool) -> set:
    """Epithets known for this genus: the static/already-discovered fast path first,
    then (with a ``store`` and not ``offline``) a live NCBI Taxonomy lookup, cached in
    ``store`` and memoized in ``_SPECIES`` so this genus never asks twice."""
    global _live_lookup_count
    low = genus.lower()
    hit = _SPECIES.get(low)
    if hit is not None:
        return hit
    if store is None or offline or low in _UNLIKELY_GENUS:
        return set()
    with store.gate(f"genus:{low}"):
        hit = _SPECIES.get(low)                    # someone else just filled it in
        if hit is not None:
            return hit
        cached = store.get_genus(low)
        if cached is not None:
            _SPECIES[low] = cached
            return cached
        with _live_lock:
            if _live_lookup_count >= _LIVE_LOOKUP_CAP:
                return set()                        # cap hit; leave uncached, try again next run
            _live_lookup_count += 1
        fetched = litapi.ncbi_taxonomy_species(genus)
        store.put_genus(low, fetched)
        _SPECIES[low] = fetched
        return fetched


def _from_binomial(text: str, store=None, offline: bool = False) -> Optional[str]:
    for m in _BINOMIAL.finditer(text):
        genus, epithet = m.group(1), m.group(2)
        if epithet.lower() in _species_for(genus, store, offline):
            return f"{genus} {epithet.lower()}"
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


def detect_species(*texts: str, store=None, offline: bool = False) -> str:
    """Best-guess organism for a claim, from its sources in order of closeness.

    Each text is tried in full — binomial, then bare genus, then common name — before
    moving to the next, so a weaker signal in the supporting sentence still beats a
    stronger one in the abstract. Returns '' when nothing is confident enough, which is
    the honest answer and must not be rendered as a species.

    The first text is the claim's own supporting sentence — the one closest to the
    experiment. An animal genus (``_ANIMAL_GENERA``) is only accepted from there; from
    every later text (title, abstract) an animal hit is skipped and the search moves on,
    since a plant paper's abstract/title routinely name-drops "humans" or "mouse" as a
    discussion aside rather than describing the organism actually studied. Plant and
    microbe names carry no such restriction — those are reliable wherever they appear.

    ``store`` (a ``provenance.store.Store``) lets the binomial check extend itself live
    via NCBI Taxonomy for a genus the static seed list does not have — see the module
    docstring. Omit it (the default) for pure, offline, network-free behaviour, exactly
    as before this existed; pass ``offline=True`` to keep a store's *cache* but skip the
    network call, matching ``verify_evidence.py --offline`` elsewhere in this package.
    """
    for i, text in enumerate(texts):
        if not text:
            continue
        for finder in (lambda t: _from_binomial(t, store, offline),
                       _from_genus, _from_common):
            hit = finder(text)
            if hit and (i == 0 or not _is_animal(hit)):
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
        # Regression cases for the denylist bug: an unlisted epithet must be rejected
        # outright, not accepted just because it isn't on a list of banned words.
        ("Light signaling induces its Arabidopsis counterpart to accumulate.",
         "Arabidopsis thaliana"),          # genus resolves; "counterpart" is not a species
        ("Capsicum fruits accumulate capsaicinoids during ripening.", ""),
        ("Citrus fruits were harvested at three ripening stages.", ""),
    ]
    ok = True
    for text, want in cases:
        got = detect_species(text)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {flag} {got!r:<28} <- {text[:56]}")

    # Multi-arg calls exercise the quote -> title -> abstract fallback and the
    # animal-genus restriction on the non-quote texts.
    multi = [
        (("", "", "A comparable pathway also operates in humans."), ""),
        (("Expressed in human HEK293 cells for validation.", "", ""),
         "Homo sapiens"),                    # animal hit OK from the quote itself
        (("", "", "Rice seedlings showed reduced tillering under drought."),
         "Oryza sativa"),                    # non-animal common name still OK from abstract
    ]
    for texts, want in multi:
        got = detect_species(*texts)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {flag} {got!r:<28} <- {texts}")

    # Live check: a genus NOT in the seed list, resolved via NCBI Taxonomy on demand and
    # cached. Network-dependent, so a failure here is reported but does not fail the
    # suite — the offline behaviour above is what must always hold.
    try:
        import os
        import tempfile
        from store import Store  # noqa: E402  (script-mode import, see top of file)
        with Store(os.path.join(tempfile.mkdtemp(), "test.db")) as st:
            text = "Fruit softening in Actinidia deliciosa (kiwifruit) is driven by cell-wall loosening."
            got = detect_species(text, store=st)
            print(f"  {'ok ' if got == 'Actinidia deliciosa' else 'FAIL'} "
                  f"{got!r:<28} <- live NCBI lookup for an unseeded genus")
            cached = st.get_genus("actinidia")
            print(f"  {'ok ' if cached else 'FAIL'} genus cached after lookup: {bool(cached)}")
    except Exception as exc:                                          # noqa: BLE001
        print(f"  (skipped live NCBI check: {exc})")

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

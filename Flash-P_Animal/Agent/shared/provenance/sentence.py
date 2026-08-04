"""
Claim modelling and supporting-sentence selection — the free, deterministic core.

FLASH-P networks record a claim as a triple (``source``, ``target``, ``sign``) with a
bare DOI and no quote. To make that claim checkable we have to find, in the paper
itself, a sentence that mentions **both** entities. That is the whole job here, and it
needs no model: a sentence either names the two things or it does not.

Why the hard gate is "both entities present":

  Direction words are unreliable in isolation. *"max3 mutants showed increased
  branching"* supports MAX3 ⊣ branching — the sentence says "increased" while the edge
  is negative, because the subject is a loss-of-function mutant. Requiring a
  directional cue to agree with the sign would reject the most common way biologists
  write results. So co-mention is the requirement and direction is a confidence
  signal, never a veto.

Alias handling is the other thing that matters in practice. Network nodes are written
``ZmEPF2`` / ``Stomatal_Conductance``; the paper says ``EPF2`` and ``stomatal
conductance``. Without stripping the species prefix and the underscores, almost every
real citation looks ungrounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Set

# Imported as functions, not as `from . import ground`: the package exports a
# function named `ground`, which would shadow the submodule of the same name.
try:
    from .ground import ground as locate, norm, split_sentences
except ImportError:                                   # run directly as a script
    from ground import ground as locate, norm, split_sentences

__all__ = ["Claim", "Support", "aliases_for", "best_support"]

# Two-letter genus/species tags that prefix gene symbols (ZmEPF2, AtBRC1, OsD27).
# Only stripped when what follows still looks like a symbol, so the metabolite
# node "Ca" is never mistaken for a prefixed name.
_SPECIES_PREFIX = re.compile(
    r"^(Zm|At|Os|Ta|Sl|Gm|Hv|Bd|Nt|Pt|Md|Vv|Ph|Ps|Le|Cs|Br|Mt|Sb|Si|Pp|Cr|Dm|Hs|Mm|Rn)"
    r"(?=[A-Z][A-Za-z0-9]{1,})")

# Head nouns too generic to identify anything on their own. "Abscisic acid" must not
# become "acid"; "Stomatal density / number" must not become "number".
_GENERIC_HEADS = {
    "acid", "acids", "protein", "proteins", "factor", "factors", "gene", "genes",
    "cell", "cells", "complex", "pathway", "pathways", "response", "responses",
    "signal", "signals", "signalling", "signaling", "level", "levels", "content",
    "activity", "rate", "rates", "size", "number", "system", "process", "processes",
    "function", "formation", "development", "growth", "expression", "status",
    "state", "index", "ratio", "amount", "production", "accumulation",
}

# Sections where a paper is restating what other people found, not reporting its own
# result. A sentence lifted from an Introduction grounds the claim in that paper's
# *citation* of someone else — which is one step further from the evidence than the
# bare DOI we started with. Still usable, but it must lose to a real finding.
_SECONDHAND_SECTIONS = re.compile(
    r"^(introduction|background|overview|literature review|general discussion|"
    r"concluding remarks|perspectives?|future)", re.I)

_POSITIVE_CUES = (
    "promot", "activat", "induc", "increas", "enhanc", "upregulat", "up-regulat",
    "stimulat", "required for", "necessary for", "positively regulat", "positive regulator",
    "elevat", "accumulat", "trigger",
)
_NEGATIVE_CUES = (
    "repress", "inhibit", "suppress", "decreas", "reduc", "downregulat", "down-regulat",
    "block", "antagoni", "negatively regulat", "negative regulator", "abolish",
    "attenuat", "prevent", "restrict", "limit",
)


class Support(NamedTuple):
    """A located, entity-verified supporting sentence."""
    quote: str            # exact source substring — safe to highlight
    locator: str          # 'abstract' | 'full_text:<section>'
    confidence: float
    direction_ok: bool    # a cue consistent with the claim's sign was present


@dataclass
class Claim:
    """One checkable assertion: two named entities and, optionally, a direction."""
    entity_a: str
    entity_b: str
    sign: int = 0                       # 1 activation, -1 inhibition, 0 unspecified
    species: str = ""
    aliases_a: Set[str] = field(default_factory=set)
    aliases_b: Set[str] = field(default_factory=set)
    label: str = ""

    def query(self) -> str:
        """Free-text search string used when the DOI has to be re-found."""
        bits = [_readable(self.entity_a), _readable(self.entity_b)]
        if self.species:
            bits.append(self.species)
        return " ".join(b for b in bits if b)


def _readable(name: str) -> str:
    return re.sub(r"[_]+", " ", str(name or "")).strip()


def aliases_for(name: str, full_name: str = "") -> Set[str]:
    """Every plausible way a paper might write this node's name.

    Covers the four transformations that actually break matching: underscores in node
    ids, species prefixes on gene symbols, the ``fn`` full name carrying an
    abbreviation in parentheses (``"Stomatal conductance (gs)"``), and the head noun
    of a multi-word phenotype.

    That last one is not optional. The paper that defines BRC1 writes *"BRC1 responds
    to … stimuli controlling branching"* — never once the full phrase "shoot
    branching". Requiring the whole node name would call the field's foundational
    citation ungrounded.
    """
    out: Set[str] = set()

    def add(s: str) -> None:
        s = re.sub(r"\s+", " ", str(s or "")).strip(" .,;:")
        if len(s) >= 2:
            out.add(s)

    raw = str(name or "").strip()
    add(raw)
    add(_readable(raw))

    # Two-letter symbols are real (ER, HA, CA), so the floor is 2 — anything higher
    # silently drops ZmER -> ER and every claim about it looks ungrounded.
    stripped = _SPECIES_PREFIX.sub("", raw)
    if stripped != raw and len(stripped) >= 2:
        add(stripped)
        add(_readable(stripped))

    fn = str(full_name or "").strip()
    if fn:
        # "Stomatal conductance (gs)" -> the phrase, and the parenthetical alone.
        for inner in re.findall(r"\(([^)]{2,})\)", fn):
            for part in re.split(r"[/,;]", inner):
                add(part)
        base = re.sub(r"\([^)]*\)", " ", fn)
        # "Stomatal density / number" -> both halves are usable names.
        for part in re.split(r"\s*/\s*", base):
            add(part)
        add(base)

    # A gene symbol IS the identifying name; when we have one, nothing is gained by
    # also matching words out of its description — and "ERECTA receptor kinase" would
    # otherwise contribute "kinase", which matches half the signalling literature.
    has_symbol = any(len(a) <= 8 and " " not in a and re.search(r"[A-Z0-9]", a)
                     for a in out)

    for a in list(out):
        words = [w for w in re.split(r"[^A-Za-z0-9]+", a) if w]
        if len(words) < 2:
            continue

        # Leading ALL-CAPS token of a short description is the symbol itself:
        # "ERECTA receptor kinase" -> ERECTA, "YODA MAPKKK" -> YODA. Capped at three
        # words so "EPIDERMAL PATTERNING FACTOR 2" does not contribute "EPIDERMAL".
        if len(words) <= 3 and len(words[0]) >= 4 and words[0].isupper():
            add(words[0])

        # Head noun of a multi-word *phenotype* — "shoot branching" -> "branching".
        # Skipped entirely when a symbol exists, since then this is a protein
        # description rather than a phrase the literature uses as a name.
        if not has_symbol:
            head = words[-1]
            if len(head) >= 6 and head.lower() not in _GENERIC_HEADS:
                add(head)

    return out


def _alias_pattern(aliases: Sequence[str]) -> Optional[re.Pattern]:
    """One regex matching any alias, on normalised text, with soft word boundaries.

    Boundaries are "not a letter or digit" rather than ``\\b`` so ``BRC1-like`` and
    ``max3-1`` still match ``BRC1`` / ``max3``, which is how mutant alleles are
    written. An optional trailing ``s`` covers ``strigolactone`` / ``strigolactones``.
    """
    parts = []
    for a in sorted({norm(x) for x in aliases if x}, key=len, reverse=True):
        if len(a) < 2:
            continue
        parts.append(re.escape(a) + ("s?" if len(a) > 3 and not a.endswith("s") else ""))
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])")


def _cue_sign(norm_sentence: str) -> int:
    """+1 / -1 / 0 from the directional language present in a sentence."""
    pos = any(c in norm_sentence for c in _POSITIVE_CUES)
    neg = any(c in norm_sentence for c in _NEGATIVE_CUES)
    if pos and not neg:
        return 1
    if neg and not pos:
        return -1
    return 0


def best_support(claim: Claim, abstract: str, fulltext: str = "") -> Optional[Support]:
    """Best sentence in this paper that mentions both of the claim's entities.

    The abstract is searched first and scores higher: an abstract-grounded citation is
    checkable by any reader, paywall or not. Returns None when no sentence names both
    entities — which is the signal that this paper does not support this claim, and
    the DOI needs repairing.
    """
    set_a = claim.aliases_a or {claim.entity_a}
    set_b = claim.aliases_b or {claim.entity_b}
    pat_a = _alias_pattern(set_a)
    pat_b = _alias_pattern(set_b)
    if pat_a is None or pat_b is None:
        return None

    # Full multi-word names are stronger evidence than a bare head noun; a sentence
    # naming "shoot branching" outranks one that only says "branching".
    phrase_a = _alias_pattern({a for a in set_a if " " in a.strip()})
    phrase_b = _alias_pattern({b for b in set_b if " " in b.strip()})

    species_terms = [t for t in norm(claim.species).split() if len(t) > 3]
    best: Optional[Support] = None
    best_score = 0.0

    for source, label, base in ((abstract or "", "abstract", 0.60),
                                (fulltext or "", "full_text", 0.50)):
        if not source:
            continue
        for sent in split_sentences(source):
            n = norm(sent)
            if not (pat_a.search(n) and pat_b.search(n)):
                continue

            cue = _cue_sign(n)
            direction_ok = (claim.sign == 0) or (cue == 0) or (cue == claim.sign)
            score = base
            if claim.sign and cue == claim.sign:
                score += 0.20
            elif claim.sign and cue == -claim.sign:
                # Legitimate for mutant phrasing, so not disqualifying — but it is
                # weaker evidence and should lose to a cleanly-worded alternative.
                score += 0.02
            if species_terms and any(t in n for t in species_terms):
                score += 0.08
            for phrase in (phrase_a, phrase_b):
                if phrase is not None and phrase.search(n):
                    score += 0.04
            # Prefer a tight sentence: a 400-character one quotes a paragraph, and a
            # reader cannot see at a glance which part is the evidence.
            if len(sent) <= 300:
                score += 0.05

            if score <= best_score:
                continue

            located = locate(sent, abstract, fulltext)
            if located is None:
                continue          # sentence splitter reshaped it; skip rather than fake it

            # Demote second-hand sections only after locating, since that is where the
            # section name comes from.
            if located.locator.startswith("full_text:"):
                section = located.locator.split(":", 1)[1]
                if _SECONDHAND_SECTIONS.match(section.strip()):
                    score -= 0.18
                    if score <= best_score:
                        continue
            best_score = score
            best = Support(quote=located.text, locator=located.locator,
                           confidence=round(min(score, 0.95), 2),
                           direction_ok=direction_ok)

    return best


if __name__ == "__main__":
    abstract = (
        "Shoot branching in Arabidopsis thaliana is controlled by strigolactones. "
        "We show that MAX3 is required for strigolactone biosynthesis, and that max3 "
        "mutants showed increased shoot branching compared with wild type. "
        "Unrelated sentence about root architecture and nitrogen supply."
    )

    ok_all = True

    c = Claim(entity_a="AtMAX3", entity_b="Shoot_Branching", sign=-1,
              species="Arabidopsis thaliana",
              aliases_a=aliases_for("AtMAX3", "MORE AXILLARY GROWTH 3 (MAX3)"),
              aliases_b=aliases_for("Shoot_Branching", "Shoot branching"))
    s = best_support(c, abstract)
    print(f"prefix-stripped gene + underscore phenotype:\n  {s}")
    ok_all &= (s is not None and "max3 mutants showed increased shoot branching" in s.quote.lower())
    ok_all &= (s is not None and s.quote in abstract)   # exact-substring guarantee

    c2 = Claim(entity_a="MAX3", entity_b="Strigolactone", sign=1,
               aliases_a=aliases_for("MAX3"), aliases_b=aliases_for("Strigolactone"))
    s2 = best_support(c2, abstract)
    print(f"\npositive edge, cue agrees:\n  {s2}")
    ok_all &= (s2 is not None and s2.direction_ok and "required for" in s2.quote.lower())

    c3 = Claim(entity_a="MAX3", entity_b="Nitrate_Uptake",
               aliases_a=aliases_for("MAX3"), aliases_b=aliases_for("Nitrate_Uptake"))
    s3 = best_support(c3, abstract)
    print(f"\nentities never co-mentioned (must be None):\n  {s3}")
    ok_all &= (s3 is None)

    al = aliases_for("ZmEPF2", "Epidermal patterning factor 2 (EPF2)")
    print(f"\naliases_for('ZmEPF2', ...) -> {sorted(al)}")
    ok_all &= ({"ZmEPF2", "EPF2"} <= al)
    ok_all &= ("Ca" in aliases_for("Ca", "Calcium"))     # 2-letter node not prefix-stripped

    print(f"\nquery(): {c.query()!r}")
    print("sentence self-test:", "OK" if ok_all else "FAILED")

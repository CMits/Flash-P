"""
Paper-identity gate — is this candidate record actually the paper we meant?

Ported from AI-Writer's ``RefResolver.verifyMatch``
(``src/main/services/RefResolver.ts:54-118``), whose thresholds were set by a real
failure: a Crossref lookup for a Cooper 2002 reference returned a Messina 2009 paper
as its confident top hit. That candidate fails all three tests here — title overlap
0.38, year off by seven, wrong first author — which is why the gate is three
independent hard rejections *before* any score, rather than one blended similarity.

The rule that matters most: **a resolvable DOI is not a correct DOI.** A mistyped or
mis-OCR'd DOI usually resolves to a real paper — just the wrong one. Anything that
proposes an identity has to clear this gate before it is believed.

This module answers "is this the right paper?". Whether that paper actually supports
the claim is a separate question, answered by ``sentence.py`` + ``ground.py``.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Set

try:
    from .ground import norm
except ImportError:                                   # run directly as a script
    from ground import norm

__all__ = ["Verdict", "title_match", "tokens", "first_author_surname"]

# Title overlap below this is a different paper, not a near-miss.
MIN_RECALL = 0.70
# Blended confidence below this is not worth acting on.
MIN_CONFIDENCE = 0.55

# Words that carry no identifying signal in a scientific title; keeping them inflates
# recall for any two papers in the same field.
_STOP = {
    "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "by", "with",
    "is", "are", "as", "at", "from", "that", "this", "its", "via", "during",
    "role", "roles", "effect", "effects", "study", "studies", "analysis",
    "novel", "new", "using", "between", "through", "into",
}


class Verdict(NamedTuple):
    accept: bool
    confidence: float
    reason: str


def tokens(s: str) -> Set[str]:
    """Content tokens of a title, normalised and stop-worded."""
    n = norm(s or "")
    return {t for t in re.split(r"[^a-z0-9]+", n) if t and t not in _STOP and len(t) > 1}


def first_author_surname(authors: str) -> str:
    """Surname of the first author from a free-form author string.

    Handles both orders the APIs return: ``"Aguilar-Martinez JA, Cubas P"``
    (Europe PMC, surname first) and ``"Jose Antonio Aguilar-Martinez, Pilar Cubas"``
    (OpenAlex, given names first).
    """
    a = (authors or "").split(",")[0].strip()
    if not a:
        return ""
    parts = a.split()
    if len(parts) == 1:
        return parts[0]
    # Trailing all-caps initials ("Cubas P") mean surname-first.
    if re.fullmatch(r"[A-Z]{1,3}", parts[-1]):
        return parts[0]
    return parts[-1]


def title_match(expected_title: str,
                candidate_title: str,
                expected_year: Optional[int] = None,
                candidate_year: Optional[int] = None,
                expected_first_author: str = "",
                candidate_authors: str = "") -> Verdict:
    """Three hard rejections, then a score. Any one failing is enough.

    Only fields that are actually present are tested — a missing year or author list
    cannot reject a candidate, it just leaves less evidence to be confident with, and
    the score reflects that.
    """
    if not candidate_title:
        return Verdict(False, 0.0, "candidate has no title")
    if not expected_title:
        return Verdict(False, 0.0, "no expected title to compare")

    want = tokens(expected_title)
    got = tokens(candidate_title)
    if not want:
        return Verdict(False, 0.0, "expected title has no content words")

    recall = len(want & got) / len(want)
    if recall < MIN_RECALL:
        return Verdict(False, recall, f"title overlap {recall:.2f} < {MIN_RECALL:.2f}")

    if expected_year and candidate_year and abs(int(candidate_year) - int(expected_year)) > 1:
        return Verdict(False, 0.0, f"year {candidate_year} vs {expected_year}")

    has_author = False
    if expected_first_author and candidate_authors:
        if norm(expected_first_author) not in norm(candidate_authors):
            return Verdict(False, 0.0,
                           f'first author "{expected_first_author}" not among candidate authors')
        has_author = True

    exact_year = bool(expected_year and candidate_year and int(expected_year) == int(candidate_year))
    confidence = min(1.0, 0.5 * recall + (0.25 if exact_year else 0.15) + (0.15 if has_author else 0.0))
    return Verdict(confidence >= MIN_CONFIDENCE, confidence, "ok")


if __name__ == "__main__":
    # The case that set these thresholds (AI-Writer ADR 0003).
    cooper = "Breeding drought-tolerant maize hybrids for the US corn belt"
    messina = "Modelling crop improvement in a GxExM framework via gene-trait-phenotype relationships"

    checks = [
        ("same paper, punctuation differs",
         title_match(cooper, "Breeding drought tolerant maize hybrids for the US Corn Belt", 2002, 2002),
         True),
        ("Messina 2009 vs Cooper 2002 (must reject)",
         title_match(cooper, messina, 2002, 2009), False),
        ("right title, wrong year (must reject)",
         title_match(cooper, cooper, 2002, 2009), False),
        ("right title, wrong first author (must reject)",
         title_match(cooper, cooper, 2002, 2002, "Cooper", "Messina C, Habben J"), False),
        ("right title, right author",
         title_match(cooper, cooper, 2002, 2002, "Cooper", "Cooper M, Gho C, Leafgren R"), True),
    ]
    ok_all = True
    for name, v, want in checks:
        ok_all &= (v.accept == want)
        print(f"  {'ok ' if v.accept == want else 'BAD'} {name:44s} "
              f"accept={v.accept} conf={v.confidence:.2f} ({v.reason})")

    surnames = [("Cubas P", "Cubas"), ("Pilar Cubas", "Cubas"),
                ("Aguilar-Martinez JA, Cubas P", "Aguilar-Martinez"),
                ("Jose Antonio Aguilar-Martinez, Pilar Cubas", "Aguilar-Martinez")]
    for raw, want in surnames:
        got = first_author_surname(raw)
        ok_all &= (got == want)
        print(f"  {'ok ' if got == want else 'BAD'} first_author({raw!r}) -> {got!r}")

    print("match self-test:", "OK" if ok_all else "FAILED")

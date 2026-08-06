"""
FLASH-P provenance — verified DOIs, grounded quotes, retrievable sources.

Every edge and every perturbation test in a FLASH-P network claims that some paper
supports it. This package makes that claim checkable end to end:

  ``litapi``   OpenAlex + Europe PMC over polite stdlib HTTP. No keys, no email.
  ``ground``   strict quote location; returns the *exact* source substring.
  ``match``    paper-identity gate — a resolvable DOI is not a correct DOI.
  ``sentence`` claim modelling and supporting-sentence selection.
  ``store``    on-disk paper cache, schema-compatible with Flash-P_DataBase.
  ``resolve``  the bounded loop: verify, else repair, else quarantine with the trail.

All of it is free — HTTP requests and string matching, no model calls.

Entry point for a whole network: ``Agent/shared/verify_evidence.py``.
"""

from __future__ import annotations

from .ground import Grounded, ground, norm
from .litapi import PaperRecord, bare_doi, doi_slug
from .match import Verdict, title_match
from .resolve import (
    QUARANTINE,
    REPAIRED,
    VERIFIED,
    Attempt,
    Config,
    Resolution,
    fetch_paper,
    resolve_claim,
)
from .sentence import Claim, Support, aliases_for, best_support
from .store import Store

__all__ = [
    "Grounded", "ground", "norm",
    "PaperRecord", "bare_doi", "doi_slug",
    "Verdict", "title_match",
    "Claim", "Support", "aliases_for", "best_support",
    "Store",
    "Config", "Resolution", "Attempt", "resolve_claim", "fetch_paper",
    "VERIFIED", "REPAIRED", "QUARANTINE",
]

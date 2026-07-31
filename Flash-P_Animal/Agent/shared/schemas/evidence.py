"""
FLASH-P schema for ``data/evidence.json`` — the provenance record for a network.

Written by ``Agent/shared/verify_evidence.py`` and read by the Studio drawer and the
atlas exporter. One file answers, for every edge and every perturbation test: which
paper, which sentence, where in the paper, and how much to trust it.

This file is **not** in the Light short-key form. Light exists to keep the files an
agent reads and writes small; nothing here is agent-authored — it is machine-generated
from API responses and only ever read. Readable keys cost nothing and make the JSON
inspectable when someone is checking a citation by hand, which is the entire point.

Two conventions are shared with ``Flash-P_DataBase/atlas.db`` so a verified network can
be contributed to the public atlas as a straight upsert:

  * ``verification`` uses that database's vocabulary — ``verified`` / ``repaired`` /
    ``quarantine``.
  * ``source_locator`` is ``abstract`` or ``full_text:<Section>``.

The invariant the Studio depends on: for any record with a non-empty ``evidence``, that
string is a **verbatim substring** of the paper's abstract or full text. It is taken
from the source, not from a model, so the drawer's highlighter cannot fail to find it.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import DoiStr, FlashPMetadata

VerificationStatus = Literal["verified", "repaired", "quarantine"]


class EvidenceModel(BaseModel):
    """Readable-key base. ``extra="ignore"`` matches the rest of the package."""
    model_config = ConfigDict(extra="ignore")


class PaperRecord(EvidenceModel):
    """One cited paper. Field names mirror ``atlas.db``'s ``paper`` table."""
    title: str = ""
    authors: str = ""
    year: Optional[int] = None
    journal: str = ""
    abstract: str = ""
    oa_status: str = ""
    has_fulltext: bool = False
    fulltext_file: str = Field(
        default="",
        description="Path relative to data/, e.g. 'fulltext/10_1105_tpc_106_048934.txt'",
    )


class Attempt(EvidenceModel):
    """One lookup we made, and what came of it — the audit trail behind a verdict.

    Kept even for successful records: a reader who wants to know *why* a DOI was
    replaced needs to see what was tried and rejected, not just the answer.
    """
    source: str = ""      # openalex | europepmc | cache
    action: str = ""      # by_doi | search | fulltext
    outcome: str = ""     # hit | miss | ungrounded | rejected
    note: str = ""


class EvidenceRecord(EvidenceModel):
    """Provenance common to an edge and a perturbation test."""
    doi: DoiStr = ""
    evidence: str = Field(
        default="",
        description="Verbatim supporting sentence, copied from the source text",
    )
    source_locator: str = Field(
        default="", description="'abstract' or 'full_text:<Section>'")
    confidence: float = 0.0
    verification: VerificationStatus = "quarantine"
    verification_reason: str = ""
    previous_doi: DoiStr = Field(
        default="", description="The DOI this record used to carry, if it was repaired")
    tried: List[Attempt] = Field(default_factory=list)


class EdgeEvidence(EvidenceRecord):
    """Evidence for one edge. Joined to ``network.json`` on ``(s, t, x)``.

    Not on ``eid``: the BUILDER renumbers edge ids from ``E###`` to ``N###`` between
    ``curated_edges.json`` and ``network.json``, so the id does not survive the trip.
    """
    eid: str = ""
    s: str
    t: str
    x: int = Field(description="1 = activation, -1 = inhibition")


class PerturbationEvidence(EvidenceRecord):
    """Evidence for one perturbation test. Joined on ``id`` (the test_id)."""
    id: str
    g: str = Field(description="Gene or perturbagen, as written in the literature")
    pt: str = ""
    ed: str = ""
    sp: str = ""


class StatusCounts(EvidenceModel):
    verified: int = 0
    repaired: int = 0
    quarantine: int = 0


class EvidenceSummary(EvidenceModel):
    edges: StatusCounts = Field(default_factory=StatusCounts)
    perturbations: StatusCounts = Field(default_factory=StatusCounts)
    papers: int = 0
    papers_with_fulltext: int = 0


class EvidenceMetadata(FlashPMetadata):
    verified_at: str = ""
    sources: List[str] = Field(default_factory=list)
    grounding: str = ""


class EvidenceFile(EvidenceModel):
    metadata: EvidenceMetadata
    summary: EvidenceSummary = Field(default_factory=EvidenceSummary)
    papers: Dict[str, PaperRecord] = Field(default_factory=dict)
    edges: List[EdgeEvidence] = Field(default_factory=list)
    perturbations: List[PerturbationEvidence] = Field(default_factory=list)

"""
FLASH-P **Light** schemas for Step 1 (LITERATURE REVIEW) output files.

- curated_edges.json     -> {metadata, nodes:{name:type}, edges:[{eid,s,t,x,d}]}
- perturbation_dataset.json -> {metadata, perturbations:[{id,g,pt,ed,sp,d}]}

Short keys via aliases; readable long keys still accepted (populate_by_name).
Provenance collapses to a single ``doi`` string. ``source_type``/``target_type``
move off every edge into the file-level ``nodes`` map. See ../LEXICON.md.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import Field

from .common import Direction, DoiStr, FlashPMetadata, NodeType, SlimModel


# ============================================================================
# curated_edges.json  (full literature repository)
# ============================================================================

class CuratedEdge(SlimModel):
    edge_id: str = Field(alias="eid", description="Unique edge id, e.g. E001")
    source: str = Field(alias="s")
    target: str = Field(alias="t")
    sign: int = Field(alias="x", description="1 = activation, -1 = inhibition")
    doi: DoiStr = Field(default="", alias="d", description="Primary supporting DOI")
    # Written by Step 1.6, and only when the claim could NOT be grounded ("q"). Absent
    # means grounded — which is the common case, so the Light file stays lean and the
    # field's mere presence is the signal the BUILDER acts on. Never hand-edit it.
    verification: str = Field(
        default="", alias="v",
        description="'q' = quarantine: Step 1.6 found no paper stating this claim")


class CuratedEdgesMetadata(FlashPMetadata):
    total_edges: Optional[int] = None


class CuratedEdgesFile(SlimModel):
    metadata: CuratedEdgesMetadata
    # node -> type, stored ONCE (replaces per-edge source_type/target_type)
    nodes: Dict[str, NodeType] = Field(default_factory=dict)
    edges: List[CuratedEdge]


# ============================================================================
# perturbation_dataset.json  (raw, before reconciliation)
# ============================================================================

class RawPerturbation(SlimModel):
    test_id: str = Field(alias="id", description="Sequential ID: T001, ...")
    gene: str = Field(alias="g")
    # free string (short codes in LEXICON.md, e.g. ko/oe/kd); long form also fine
    perturbation_type: str = Field(alias="pt")
    expected_direction: Direction = Field(alias="ed")
    species: str = Field(default="", alias="sp")
    doi: DoiStr = Field(default="", alias="d")
    # See CuratedEdge.verification — same meaning, same "absent = grounded" convention.
    verification: str = Field(
        default="", alias="v",
        description="'q' = quarantine: Step 1.6 found no paper stating this claim")


class PerturbationDatasetMetadata(FlashPMetadata):
    total_perturbations: Optional[int] = None


class PerturbationDatasetFile(SlimModel):
    metadata: PerturbationDatasetMetadata
    perturbations: List[RawPerturbation]

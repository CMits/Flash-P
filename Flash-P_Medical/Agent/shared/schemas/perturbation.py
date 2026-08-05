"""
FLASH-P **Light** schema for Step 3 (PERTURBATION RECONCILIATION).

- reconciled_perturbation_dataset.json
  -> {metadata (incl. phenotype_node, total_tested, total_found),
      perturbations:[{id,g,pt,ed,ng,m,exo,cb,rt}]}

Light reconciled holds ONLY the **testable** tests (in_network == true). It is pure
encoding + the test_id join key + provenance:
  * dropped: in_network, condition, perturbations[] (duplicate of m/exo), notes,
    reconciliation_note, expected_magnitude, species, per-record phenotype_node.
  * kept: ``doi`` (``d``). It was dropped in the original Light cut, which left
    ``evidence_doi`` plumbed all the way through ``validation_common`` to the exported
    CSVs and empty in every row. A validated test that cannot name its source is not
    much of a validation, so the DOI rides along; the evidence *sentence* lives in
    ``data/evidence.json``, keyed by ``test_id``.
  * phenotype_node lives once in metadata.

The three critical-type guarantees are preserved:
  network_gene -> List[str] | gene_modifiers -> Dict[str,float] | exogenous_supply -> Dict[str,float]
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import Field, field_validator

from .common import Direction, DoiStr, FlashPMetadata, ReconciliationType, SlimModel


class PerturbationModification(SlimModel):
    """Legacy atomic modification — kept for import-compat; not used in Light."""
    node: str = Field(alias="n")
    modifier_type: Literal["gene_modifier", "exogenous_supply"]
    value: float


class ReconciledPerturbation(SlimModel):
    """One TESTABLE perturbation mapped to network nodes (encoding only)."""
    test_id: str = Field(alias="id", description="Sequential ID: T001, ...")
    gene: str = Field(alias="g", description="Original gene name from literature")
    perturbation_type: str = Field(alias="pt")
    expected_direction: Direction = Field(alias="ed")
    network_gene: List[str] = Field(
        default_factory=list, alias="ng",
        description="Network node(s) perturbed. ALWAYS a list.",
    )
    gene_modifiers: Dict[str, float] = Field(
        default_factory=dict, alias="m",
        description="node -> modifier (0.0=KO,0.5=KD,1.0=WT,2.0=OE). ALWAYS a dict.",
    )
    exogenous_supply: Dict[str, float] = Field(
        default_factory=dict, alias="exo",
        description="node -> supply value. ALWAYS a flat dict.",
    )
    comparison_baseline: str = Field(
        default="WT", alias="cb",
        description="WT for most tests, mutant_alone for rescue experiments",
    )
    reconciliation_type: Optional[ReconciliationType] = Field(default=None, alias="rt")
    doi: DoiStr = Field(
        default="", alias="d",
        description="Supporting DOI, carried through from perturbation_dataset.json",
    )

    @field_validator("network_gene", mode="before")
    @classmethod
    def _coerce_network_gene(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("gene_modifiers", mode="before")
    @classmethod
    def _coerce_gene_modifiers(cls, v):
        if v is None:
            return {}
        if isinstance(v, (int, float)):
            raise ValueError(
                f"gene_modifiers must be a dict, got scalar {v}. Use {{'NODE': {v}}}."
            )
        return v

    @field_validator("exogenous_supply", mode="before")
    @classmethod
    def _coerce_exogenous_supply(cls, v):
        if v is None:
            return {}
        if isinstance(v, dict) and "node" in v:
            raise ValueError(
                f"exogenous_supply must be a flat dict like {{'ABI5': 1.0}}, not {v}"
            )
        return v


class ReconciledPerturbationMetadata(FlashPMetadata):
    phenotype_node: str = ""
    total_tested: Optional[int] = Field(
        default=None, description="Number of testable (in-network) tests included"
    )
    total_found: Optional[int] = Field(
        default=None, description="Number of tests in perturbation_dataset.json"
    )


class ReconciledPerturbationFile(SlimModel):
    metadata: ReconciledPerturbationMetadata
    direction_threshold: float = 0.05
    perturbations: List[ReconciledPerturbation]

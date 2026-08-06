"""
Common types for FLASH-P **Light** schemas.

Light differs from the full pipeline (see ``../LEXICON.md`` for the full legend):

  * **Short field keys** via Pydantic aliases. Every slim model sets
    ``populate_by_name=True``, so BOTH the short alias (``m``) and the readable
    Python name (``gene_modifiers``) are accepted on input. Dump with
    ``model_dump(by_alias=True)`` to emit the short form.
  * **Short enum VALUES** (``NodeType.GENE == "G"``, ``Direction.INCREASED == "up"``),
    but ``LexEnum._missing_`` also accepts the long/readable form ("GENE",
    "increased") so an agent that emits the readable value still validates.
  * **Provenance is a single ``doi`` string** (alias ``d``); the fat
    ``EvidenceEntry`` object is gone from the Light data files.

The legacy enums/models (``PerturbationType``, ``EdgeEffect``, ``Confidence``,
``Verification``, ``EvidenceEntry``) are kept defined for import-compatibility with
the rest of the package but are **not** used by the slim Light schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Base classes
# ============================================================================

class SlimModel(BaseModel):
    """Base for Light record models.

    - ``populate_by_name=True`` -> accept short alias OR readable name on input.
    - ``use_enum_values=True``  -> store/serialize the (short) enum value.
    - ``extra="ignore"``        -> tolerate stray fields rather than reject.
    Serialize the short form with ``model.model_dump(by_alias=True)``.
    """
    model_config = ConfigDict(
        populate_by_name=True, use_enum_values=True, extra="ignore"
    )


class LexEnum(str, Enum):
    """str-enum whose values are short codes but that also accepts the long form.

    Resolution order in ``_missing_``: exact short value (handled by Enum), then
    case-insensitive match against the member name OR the long value. So both
    ``"ko"`` and ``"knockout"``/``"KNOCKOUT"`` resolve to the same member.
    """

    @classmethod
    def _missing_(cls, value):
        v = str(value).strip().lower()
        for member in cls:
            if member.name.lower() == v or str(member.value).lower() == v:
                return member
        return None


# ============================================================================
# Enums — SHORT values (long form accepted on input via LexEnum._missing_)
# ============================================================================

class NodeType(LexEnum):
    GENE = "G"
    HORMONE = "H"
    METABOLITE = "M"
    ENVIRONMENT = "E"
    PROTEIN_COMPLEX = "PC"
    REGULATORY_RNA = "R"
    PHENOTYPE = "P"
    PROCESS = "PR"


class Direction(LexEnum):
    INCREASED = "up"
    DECREASED = "dn"
    UNCHANGED = "nc"


class ReconciliationType(LexEnum):
    EXACT_MATCH = "em"
    CASE_INSENSITIVE = "ci"
    FAMILY_MEMBER = "fm"
    COMPOSITE_COLLAPSE = "cc"
    COMPOSITE_MEMBER = "cm"
    TREATMENT_ANALOG = "ta"
    MECHANISM_MAPPING = "mm"
    NOT_IN_NETWORK = "nin"
    CONTROL = "ctl"


# ============================================================================
# Shared metadata (one block per file -> kept READABLE, not abbreviated)
# ============================================================================

class FlashPMetadata(BaseModel):
    """Metadata block present in every pipeline output file (readable keys)."""
    model_config = ConfigDict(extra="ignore")
    flash_p_version: str = "light-1.0-debiasing"
    build_variant: str = ""          # e.g. "debiasing" — which spec variant produced this file
    phenotype: str = ""
    species: str = ""
    created: str = ""


# ============================================================================
# Legacy definitions — kept ONLY for package import-compatibility.
# Not used by the slim Light data files.
# ============================================================================

class PerturbationType(str, Enum):
    KNOCKOUT = "knockout"
    OVEREXPRESSION = "overexpression"
    KNOCKDOWN = "knockdown"
    GAIN_OF_FUNCTION = "gain_of_function"
    LOSS_OF_FUNCTION = "loss_of_function"
    DOUBLE_KNOCKOUT = "double_knockout"
    DOUBLE_MUTANT = "double_mutant"
    TRIPLE_KNOCKOUT = "triple_knockout"
    RESCUE = "rescue"
    TREATMENT = "treatment"
    COMBINED = "combined"
    EPISTASIS = "epistasis"
    CONTROL = "control"


class EdgeEffect(str, Enum):
    ACTIVATION = "activation"
    INHIBITION = "inhibition"
    REPRESSION = "repression"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Verification(str, Enum):
    FULL_TEXT_READ = "full_text_read"
    ABSTRACT_READ = "abstract_read"
    PUBMED_CROSSCHECKED = "pubmed_crosschecked"


class EvidenceEntry(BaseModel):
    """Legacy fat evidence object. Light files use a single ``doi`` string instead."""
    doi: str = ""
    title: str = ""
    authors: str = ""
    year: Optional[int] = None
    journal: str = ""
    evidence_sentence: str = ""
    claim: str = ""
    verification: Optional[Verification] = None
    full_text_read: Optional[bool] = None

"""
Common types for FLASH-P **Light** schemas (Animal / Cattle edition).

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

Animal edition: standard 8 node types (no DRUG). ``PerturbationType`` carries the
livestock drug/treatment vocabulary (kept for reference/import-compat; ``pt`` is a
free string in the slim schemas, with preferred short codes in ``../LEXICON.md``).

The legacy enums/models (``PerturbationType``, ``EdgeEffect``, ``Confidence``,
``Verification``, ``EvidenceEntry``) are kept defined for import-compatibility with
the rest of the package but are **not** used by the slim Light schemas.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


# ============================================================================
# Provenance field types
# ============================================================================

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _check_doi(v):
    """Accept a real DOI or the empty string; reject anything else.

    Empty is legitimate — during Step 1 an edge may not have found its paper yet, and
    ``verify_evidence.py`` treats a blank DOI as "go and find one". What is never
    legitimate is a *malformed* DOI: it cannot resolve, so it can only ever be a typo
    or an invention, and catching it at write time is far cheaper than at review time.

    Note this checks shape only. A well-formed DOI can still point at the wrong paper —
    that is what ``Agent/shared/provenance/`` exists to catch, and no regex can.
    """
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"doi must be a string, got {type(v).__name__}")
    s = v.strip()
    if s and not _DOI_RE.match(s):
        raise ValueError(
            f"malformed DOI {s!r} — expected the bare form '10.xxxx/suffix' "
            f"(no 'doi:' prefix, no https://doi.org/ URL)"
        )
    return s


DoiStr = Annotated[str, BeforeValidator(_check_doi)]


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
    ENVIRONMENT = "E"        # Nutrition, Heat_Stress, Cold_Stress, Photoperiod, etc.
    PROTEIN_COMPLEX = "PC"
    REGULATORY_RNA = "R"
    PHENOTYPE = "P"          # cattle trait: Height, Muscle_Mass, Milk_Yield, Coat_Colour, etc.
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
    flash_p_version: str = "light-animal-1.0"
    phenotype: str = ""
    species: str = ""
    created: str = ""


# ============================================================================
# Legacy definitions — kept ONLY for package import-compatibility and as the
# cattle perturbation-vocabulary reference. Not enforced by the slim Light data
# files (``pt`` is a free string; short codes live in ../LEXICON.md).
# ============================================================================

class PerturbationType(str, Enum):
    # --- Single-gene perturbations ---
    KNOCKOUT = "knockout"
    OVEREXPRESSION = "overexpression"
    KNOCKDOWN = "knockdown"
    GAIN_OF_FUNCTION = "gain_of_function"
    LOSS_OF_FUNCTION = "loss_of_function"
    HETEROZYGOUS = "heterozygous"
    KNOCKOUT_CRISPR = "knockout_CRISPR"
    PHOSPHO_DEAD_MUTANT = "phospho_dead_mutant"
    NATURAL_LOF_ALLELE = "natural_LoF_allele"   # e.g., MSTN nt821del (Belgian Blue), MC1R e allele
    # --- Multi-gene ---
    DOUBLE_KNOCKOUT = "double_knockout"
    DOUBLE_MUTANT = "double_mutant"
    TRIPLE_KNOCKOUT = "triple_knockout"
    QUADRUPLE_KNOCKOUT = "quadruple_knockout"
    QUINTUPLE_KNOCKOUT = "quintuple_knockout"
    # --- Gene + treatment combos ---
    KNOCKOUT_PLUS_TREATMENT = "knockout_plus_treatment"
    KNOCKOUT_PLUS_GH = "knockout_plus_GH"
    KNOCKOUT_PLUS_TESTOSTERONE = "knockout_plus_testosterone"
    KNOCKOUT_PLUS_IGF1 = "knockout_plus_IGF1"
    GAIN_OF_FUNCTION_PLUS_TREATMENT = "gain_of_function_plus_treatment"
    GAIN_OF_FUNCTION_PLUS_GH = "gain_of_function_plus_GH"
    LOSS_OF_FUNCTION_PLUS_GH = "loss_of_function_plus_GH"
    # --- Rescue ---
    RESCUE = "rescue"
    RESCUE_EXPERIMENT = "rescue_experiment"
    # --- Background epistasis ---
    KNOCKDOWN_IN_MSTN_BACKGROUND = "knockdown_in_MSTN_background"
    KNOCKDOWN_IN_GHR_BACKGROUND = "knockdown_in_GHR_background"
    KNOCKDOWN_IN_MC1R_BACKGROUND = "knockdown_in_MC1R_background"
    OE_IN_MSTN_BACKGROUND = "OE_in_MSTN_background"
    # --- Combined / epistasis ---
    COMBINED = "combined"
    COMBINED_TRANSGENIC = "combined_transgenic"
    EPISTASIS = "epistasis"
    # --- Pure treatments (WT + something) ---
    TREATMENT = "treatment"
    EXOGENOUS_TREATMENT = "exogenous_treatment"
    EXOGENOUS_GH = "exogenous_GH"                   # bovine somatotropin / bST
    EXOGENOUS_TESTOSTERONE = "exogenous_testosterone"
    EXOGENOUS_IGF1 = "exogenous_IGF1"
    EXOGENOUS_CORTISOL = "exogenous_cortisol"       # dexamethasone analogues
    EXOGENOUS_ALPHA_MSH = "exogenous_alpha_MSH"
    CHEMICAL_TREATMENT = "chemical_treatment"
    BETA_AGONIST_TREATMENT = "beta_agonist_treatment"   # ractopamine, zilpaterol
    INHIBITOR_MSTN = "inhibitor_MSTN"                # ACE-031 / bimagrumab analogues
    INHIBITOR_AR = "inhibitor_AR"                    # flutamide
    GNRH_AGONIST = "GnRH_agonist"
    GNRH_ANTAGONIST = "GnRH_antagonist"
    # --- Nutritional / environmental ---
    HEAT_STRESS = "heat_stress"
    COLD_STRESS = "cold_stress"
    FEED_RESTRICTION = "feed_restriction"
    HIGH_ENERGY_DIET = "high_energy_diet"
    PROTEIN_RESTRICTION = "protein_restriction"
    LPS_CHALLENGE = "LPS_challenge"
    ENVIRONMENTAL = "environmental"
    # --- Controls ---
    CONTROL = "control"
    NEGATIVE_CONTROL = "negative_control"


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

"""
Schemas for Step 4 (VALIDATOR) output files.

- script_validation_results.json (Algebraic)
- ode_validation_results.json
- rwr_validation_results.json
- accuracy_metrics.json
- failure_analysis.json
- method_comparison.json
- sensitivity results
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .common import Direction, FlashPMetadata, PerturbationType


# ============================================================================
# Shared validation types
# ============================================================================

class DetailedResult(BaseModel):
    """One perturbation test result."""
    test_id: str
    gene: str
    perturbation_type: str  # kept as str for flexibility in display
    gene_modifier: float
    wt_value: float = 1.0
    perturbed_value: float
    comparison_baseline: str = "WT"
    comparison_baseline_value: float = 1.0
    ratio: float
    log2_fold_change: float
    direction_threshold: float = 0.01
    predicted_direction: Direction
    expected_direction: Direction
    correct: bool
    phenotype_node: str
    converged: bool = True
    iterations: int = 0
    complexity_score: int
    complexity_label: str
    path_length: Optional[int] = None
    path: List[str] = Field(default_factory=list)
    evidence_doi: str = ""
    exogenous_node: Optional[str] = None
    exogenous_value: Optional[float] = None


class PerClassMetrics(BaseModel):
    precision: float
    recall: float
    f1: float
    support: int


class PerturbationTypeMetrics(BaseModel):
    correct: int
    total: int
    accuracy: float


class ValidationMetrics(BaseModel):
    overall_accuracy: float
    correct: int
    total: int
    cohens_kappa: float = 0.0
    mcc: float = 0.0
    per_class: Dict[str, PerClassMetrics] = Field(default_factory=dict)
    confusion_matrix: Optional[Dict[str, Any]] = None
    by_perturbation_type: Optional[Dict[str, PerturbationTypeMetrics]] = None


class ValidationSummary(BaseModel):
    total_equations: Optional[int] = None
    total_perturbations: int
    tested: int
    skipped: int = 0


# ============================================================================
# Validation results files (Algebraic, ODE, RWR — same structure)
# ============================================================================

class ValidationResultsFile(BaseModel):
    """
    Schema for script_validation_results.json, ode_validation_results.json,
    and rwr_validation_results.json. All three share this structure.
    """
    flash_p_version: str
    network: str
    phenotype: str
    species: str
    method: str = Field(
        default="",
        description="Algebraic, ODE (Hill Functions), or RWR"
    )
    equation_mode: Optional[str] = None
    equation_spec: Optional[Dict[str, Any]] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    summary: ValidationSummary
    metrics: ValidationMetrics
    detailed_results: List[DetailedResult]
    # ODE-specific
    hill_formulas: Optional[Dict[str, str]] = None
    # RWR-specific
    best_alpha: Optional[float] = None


# ============================================================================
# Sensitivity results
# ============================================================================

class ODESensitivityResult(BaseModel):
    K: float
    n: int
    accuracy: float
    kappa: float = 0.0
    mcc: float = 0.0


class ODESensitivityFile(BaseModel):
    network: str
    method: str = "ODE (Hill Functions) - Sensitivity Analysis"
    parameter_ranges: Dict[str, Any]
    results_grid: List[ODESensitivityResult]


class RWRSensitivityResult(BaseModel):
    alpha: float
    accuracy: float
    kappa: float = 0.0
    mcc: float = 0.0


class RWRSensitivityFile(BaseModel):
    network: str
    method: str = "Random Walk with Restart - Sensitivity Analysis"
    parameter_ranges: Dict[str, Any]
    results: List[RWRSensitivityResult]


# ============================================================================
# accuracy_metrics.json
# ============================================================================

class MethodAccuracy(BaseModel):
    accuracy: float
    correct: int
    total_tested: int
    kappa: float = 0.0
    mcc: float = 0.0
    convergence_rate: Optional[float] = None
    failures: List[str] = Field(default_factory=list)
    ci_95: Optional[List[float]] = None
    best_K: Optional[float] = None
    best_n: Optional[int] = None
    best_alpha: Optional[float] = None


class AccuracyMetricsFile(BaseModel):
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tests: Dict[str, int] = Field(default_factory=dict)
    algebraic: MethodAccuracy
    ode: MethodAccuracy
    rwr: MethodAccuracy


# ============================================================================
# failure_analysis.json
# ============================================================================

class FailureEntry(BaseModel):
    test_id: str
    gene: str
    perturbation_type: str
    expected_direction: Direction
    predicted_direction: Direction
    category: str = Field(
        description="framework_limitation, epistasis_complexity, "
                    "combined_perturbation, edge_case"
    )
    explanation: str
    evidence: str = ""
    fixable: bool = False
    fix_strategy: str = ""


class FailureAnalysisFile(BaseModel):
    metadata: FlashPMetadata
    failures: List[FailureEntry]
    summary: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# method_comparison.json
# ============================================================================

class MethodComparisonEntry(BaseModel):
    method: str
    accuracy: float
    kappa: float = 0.0
    mcc: float = 0.0
    convergence_rate: Optional[float] = None
    best_params: str = ""
    strengths: str = ""
    weaknesses: str = ""
    failures: List[str] = Field(default_factory=list)


class MethodComparisonFile(BaseModel):
    metadata: FlashPMetadata
    summary: Dict[str, Any] = Field(default_factory=dict)
    comparison: List[MethodComparisonEntry]

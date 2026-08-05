"""
FLASH-P v1.0 Pydantic Schemas

Strict type definitions for every JSON file in the pipeline.
Import from here for validation and type checking.
"""

from .common import (
    Confidence,
    Direction,
    DoiStr,
    EdgeEffect,
    EvidenceEntry,
    FlashPMetadata,
    NodeType,
    PerturbationType,
    ReconciliationType,
    Verification,
)
from .evidence import (
    Attempt,
    EdgeEvidence,
    EvidenceFile,
    EvidenceRecord,
    PaperRecord,
    PerturbationEvidence,
)
from .literature import (
    CuratedEdge,
    CuratedEdgesFile,
    PerturbationDatasetFile,
    RawPerturbation,
)
from .network import (
    AlgebraicEquation,
    AlgebraicEquationsFile,
    NetworkEdge,
    NetworkFile,
    NetworkNode,
    NodeAnnotation,
    NodeAnnotationsFile,
    ODEEquationsFile,
)
from .perturbation import (
    PerturbationModification,
    ReconciledPerturbation,
    ReconciledPerturbationFile,
)
from .provenance import (
    FileRecord,
    PipelineManifest,
    StepRecord,
)
from .refinement import (
    FixApplied,
    IterationFixesFile,
    IterationRecord,
    RefinementReportFile,
)
from .validation import (
    AccuracyMetricsFile,
    DetailedResult,
    FailureAnalysisFile,
    FailureEntry,
    MethodComparisonFile,
    ODESensitivityFile,
    RWRSensitivityFile,
    ValidationMetrics,
    ValidationResultsFile,
)

__all__ = [
    # Common
    "Confidence", "Direction", "DoiStr", "EdgeEffect", "EvidenceEntry",
    "FlashPMetadata", "NodeType", "PerturbationType",
    "ReconciliationType", "Verification",
    # Evidence (provenance)
    "PaperRecord", "Attempt", "EvidenceRecord", "EdgeEvidence",
    "PerturbationEvidence", "EvidenceFile",
    # Literature
    "CuratedEdge", "CuratedEdgesFile",
    "RawPerturbation", "PerturbationDatasetFile",
    # Network
    "NetworkNode", "NetworkEdge", "NetworkFile",
    "AlgebraicEquation", "AlgebraicEquationsFile",
    "ODEEquationsFile",
    "NodeAnnotation", "NodeAnnotationsFile",
    # Perturbation
    "PerturbationModification", "ReconciledPerturbation",
    "ReconciledPerturbationFile",
    # Validation
    "DetailedResult", "ValidationMetrics", "ValidationResultsFile",
    "AccuracyMetricsFile", "FailureEntry", "FailureAnalysisFile",
    "MethodComparisonFile", "ODESensitivityFile", "RWRSensitivityFile",
    # Provenance
    "FileRecord", "PipelineManifest", "StepRecord",
    # Refinement
    "FixApplied", "IterationRecord", "RefinementReportFile",
    "IterationFixesFile",
]

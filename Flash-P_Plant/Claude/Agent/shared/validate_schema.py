#!/usr/bin/env python3
"""
FLASH-P v1.0 Schema Validator

Validates JSON files against Pydantic schemas.
Used standalone and by Claude Code hooks.

Usage:
    python validate_schema.py <file_path>           # Validate one file
    python validate_schema.py --network <dir_path>  # Validate all JSONs in a network
    python validate_schema.py --all <base_dir>      # Validate all networks

Exit codes:
    0 = all valid
    1 = validation errors found
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for schema imports
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

import light_io
import toon_codec
from schemas import (
    AccuracyMetricsFile,
    AlgebraicEquationsFile,
    CuratedEdgesFile,
    EvidenceFile,
    FailureAnalysisFile,
    MethodComparisonFile,
    NetworkFile,
    NodeAnnotationsFile,
    ODEEquationsFile,
    ODESensitivityFile,
    PerturbationDatasetFile,
    ReconciledPerturbationFile,
    RefinementReportFile,
    RWRSensitivityFile,
    ValidationResultsFile,
)

# Map file name patterns to Pydantic models
FILE_SCHEMA_MAP = {
    "curated_edges.json": CuratedEdgesFile,
    "evidence.json": EvidenceFile,
    "perturbation_dataset.json": PerturbationDatasetFile,
    "reconciled_perturbation_dataset.json": ReconciledPerturbationFile,
    "network.json": NetworkFile,
    "algebraic_equations.json": AlgebraicEquationsFile,
    "ode_equations.json": ODEEquationsFile,
    "node_annotations.json": NodeAnnotationsFile,
    "script_validation_results.json": ValidationResultsFile,
    "ode_validation_results.json": ValidationResultsFile,
    "rwr_validation_results.json": ValidationResultsFile,
    "ode_sensitivity_results.json": ODESensitivityFile,
    "rwr_sensitivity_results.json": RWRSensitivityFile,
    "accuracy_metrics.json": AccuracyMetricsFile,
    "failure_analysis.json": FailureAnalysisFile,
    "method_comparison.json": MethodComparisonFile,
    "refinement_report.json": RefinementReportFile,
}


def detect_schema(file_path: Path):
    """Detect the Pydantic model for a given file path."""
    name = file_path.name
    if name in FILE_SCHEMA_MAP:
        return FILE_SCHEMA_MAP[name]

    # Check for iteration snapshot files
    if "iteration" in str(file_path):
        if "validation" in name or name.startswith("script_"):
            return ValidationResultsFile
        if "algebraic_equations" in name or "equations_snapshot" in name:
            return AlgebraicEquationsFile
        if "network_snapshot" in name:
            return NetworkFile
        if "fixes_applied" in name:
            from schemas import IterationFixesFile
            return IterationFixesFile

    return None


def validate_file(file_path: Path, quiet: bool = False) -> list:
    """
    Validate a single JSON file against its schema.

    Returns list of error strings. Empty list = valid.
    """
    errors = []

    if not file_path.exists():
        return [f"File not found: {file_path}"]

    schema_cls = detect_schema(file_path)
    if schema_cls is None:
        if not quiet:
            print(f"  SKIP: {file_path.name} (no schema defined)")
        return []

    text = file_path.read_text(encoding="utf-8")
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    if first_line.startswith("#meta") or toon_codec.is_toon(text):
        # Light TOON whole-file format ("#meta {json}" + a TOON table) — decode via
        # light_io, which raw-reads the table into the same short-alias dicts the
        # SlimModel schemas already accept (populate_by_name=True, extra="ignore").
        try:
            data = light_io._read_raw(str(file_path), light_io.kind_of(str(file_path)))
        except Exception as e:
            return [f"Invalid TOON: {e}"]
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return [f"Invalid JSON: {e}"]

    try:
        schema_cls.model_validate(data)
    except Exception as e:
        error_str = str(e)
        # Truncate long error messages
        if len(error_str) > 500:
            error_str = error_str[:500] + "..."
        errors.append(error_str)

    return errors


def validate_network(network_dir: Path, quiet: bool = False) -> dict:
    """Validate all JSON files in a network directory."""
    results = {}

    # Check standard locations
    json_dirs = [
        network_dir / "data",
        network_dir / "network",
        network_dir / "validation",
        network_dir / "refinement",
    ]

    for d in json_dirs:
        if not d.exists():
            continue
        for json_file in sorted(d.rglob("*.json")):
            rel = json_file.relative_to(network_dir)
            errs = validate_file(json_file, quiet=quiet)
            if errs:
                results[str(rel)] = errs

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="FLASH-P Schema Validator")
    parser.add_argument("path", nargs="?", help="File or directory to validate")
    parser.add_argument("--network", help="Validate all JSONs in a network directory")
    parser.add_argument("--all", help="Validate all networks under a base directory")
    parser.add_argument("--quiet", action="store_true", help="Only show errors")
    args = parser.parse_args()

    total_errors = 0

    if args.all:
        base = Path(args.all)
        network_dirs = sorted(
            p.parent for p in base.rglob("network/network.json")
        )
        for net_dir in network_dirs:
            net_name = net_dir.name
            print(f"\n{'='*60}")
            print(f"Network: {net_name}")
            print(f"{'='*60}")
            results = validate_network(net_dir, quiet=args.quiet)
            if results:
                for rel_path, errs in results.items():
                    print(f"\n  FAIL: {rel_path}")
                    for e in errs:
                        print(f"    {e}")
                    total_errors += len(errs)
            else:
                print("  ALL PASS")

    elif args.network:
        net_dir = Path(args.network)
        print(f"Validating network: {net_dir.name}")
        results = validate_network(net_dir, quiet=args.quiet)
        if results:
            for rel_path, errs in results.items():
                print(f"\n  FAIL: {rel_path}")
                for e in errs:
                    print(f"    {e}")
                total_errors += len(errs)
        else:
            print("  ALL PASS")

    elif args.path:
        file_path = Path(args.path)
        if file_path.is_file():
            errs = validate_file(file_path, quiet=args.quiet)
            if errs:
                print(f"FAIL: {file_path}")
                for e in errs:
                    print(f"  {e}")
                total_errors = len(errs)
            else:
                print(f"PASS: {file_path}")
        else:
            print(f"Not a file: {file_path}")
            total_errors = 1
    else:
        parser.print_help()
        sys.exit(0)

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()

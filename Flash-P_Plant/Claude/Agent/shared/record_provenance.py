#!/usr/bin/env python3
"""
FLASH-P v1.0 — Provenance Recorder

Records what was done at each pipeline step: files produced, checksums, timestamps.

Usage:
    python record_provenance.py <network_dir> --step <1-5> --model <model_name>

Creates/updates pipeline_manifest.json in the network directory.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from schemas.provenance import FileRecord, PipelineManifest, StepRecord
from schemas.common import FlashPMetadata
from flashp_version import get_version

STEP_NAMES = {
    1: "Literature Review",
    2: "Builder",
    3: "Perturbation Reconciliation",
    4: "Validator",
    5: "Refinement",
    6: "Export",
}

STEP_OUTPUTS = {
    1: ["data/curated_edges.json", "data/perturbation_dataset.json"],
    2: ["network/network.json", "network/algebraic_equations.json",
        "network/node_annotations.json"],
    3: ["data/reconciled_perturbation_dataset.json"],
    4: ["validation/script_validation_results.json",
        "validation/ode_validation_results.json",
        "validation/rwr_validation_results.json",
        "validation/accuracy_metrics.json",
        "validation/failure_analysis.json",
        "validation/method_comparison.json"],
    5: ["refinement/refinement_report.json"],
    6: ["supplementary/Table_S1_edges.csv",
        "supplementary/Table_S2_perturbations.csv",
        "supplementary/Table_S3_reconciled_perturbations.csv",
        "supplementary/Table_S4_algebraic_equations.csv",
        "supplementary/Table_S5_ode_equations.csv",
        "supplementary/Table_S7a_algebraic_results.csv",
        "supplementary/Table_S7b_ode_results.csv",
        "supplementary/Table_S7c_rwr_results.csv",
        "supplementary/Table_S8_method_comparison.csv",
        "supplementary/Table_S9_stratified_results.csv",
        "supplementary/master_test_level.csv",
        "supplementary/Fig_Data/network_summary.csv",
        "network/cytoscape/network.graphml",
        "network/cytoscape/network.sif"],
}

STEP_INPUTS = {
    1: [],
    2: ["data/curated_edges.json"],
    3: ["network/network.json", "data/perturbation_dataset.json"],
    4: ["network/network.json", "network/algebraic_equations.json",
        "data/reconciled_perturbation_dataset.json"],
    5: ["validation/script_validation_results.json",
        "validation/ode_validation_results.json",
        "validation/rwr_validation_results.json"],
    6: ["network/network.json", "network/algebraic_equations.json",
        "validation/script_validation_results.json",
        "validation/ode_validation_results.json",
        "validation/rwr_validation_results.json",
        "validation/method_comparison.json"],
}


def sha256(path):
    if not path.exists():
        return ""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def make_file_record(net_dir, rel_path):
    full = net_dir / rel_path
    if not full.exists():
        return None
    return FileRecord(
        path=rel_path,
        checksum_sha256=sha256(full),
        size_bytes=full.stat().st_size,
        created=datetime.fromtimestamp(
            full.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    )


def record_step(net_dir, step, model="unknown"):
    manifest_path = net_dir / "pipeline_manifest.json"
    now = datetime.now(timezone.utc).isoformat()

    # Load or create manifest
    if manifest_path.exists():
        manifest = PipelineManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    else:
        # Try to get phenotype/species from network.json or metadata
        phenotype = net_dir.name.replace("_network", "")
        species = ""
        net_json = net_dir / "network" / "network.json"
        if net_json.exists():
            d = json.loads(net_json.read_text(encoding="utf-8"))
            meta = d.get("metadata", {})
            phenotype = meta.get("phenotype", phenotype)
            species = meta.get("species", "")

        manifest = PipelineManifest(
            metadata=FlashPMetadata(
                flash_p_version=get_version(),
                phenotype=phenotype,
                species=species,
                created=now[:10],
            ),
            network_directory=str(net_dir),
        )

    # Build input records
    inputs = []
    for rel in STEP_INPUTS.get(step, []):
        rec = make_file_record(net_dir, rel)
        if rec:
            inputs.append(rec)

    # Build output records
    outputs = []
    for rel in STEP_OUTPUTS.get(step, []):
        rec = make_file_record(net_dir, rel)
        if rec:
            outputs.append(rec)

    # Also scan for any extra outputs in refinement iterations
    if step == 5:
        ref_dir = net_dir / "refinement"
        if ref_dir.exists():
            for json_file in sorted(ref_dir.rglob("*.json")):
                rel = str(json_file.relative_to(net_dir)).replace("\\", "/")
                rec = make_file_record(net_dir, rel)
                if rec:
                    outputs.append(rec)

    step_rec = StepRecord(
        step=step,
        step_name=STEP_NAMES[step],
        started=now,
        completed=now,
        model=model,
        flash_p_version=get_version(),
        inputs=inputs,
        outputs=outputs,
    )

    # Replace existing step record if present, else append
    manifest.steps = [s for s in manifest.steps if s.step != step]
    manifest.steps.append(step_rec)
    manifest.steps.sort(key=lambda s: s.step)
    manifest.current_step = max(s.step for s in manifest.steps)

    # Write manifest
    manifest_path.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    print(f"Recorded Step {step} ({STEP_NAMES[step]}): "
          f"{len(inputs)} inputs, {len(outputs)} outputs")
    print(f"Manifest: {manifest_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Record pipeline provenance")
    parser.add_argument("network_dir")
    parser.add_argument("--step", type=int, required=True, choices=[1, 2, 3, 4, 5, 6])
    parser.add_argument("--model", default="unknown")
    args = parser.parse_args()

    record_step(Path(args.network_dir), args.step, args.model)


if __name__ == "__main__":
    main()

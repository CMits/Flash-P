#!/usr/bin/env python3
"""
FLASH-P v1.0 — Master CSV Export

Produces a single comprehensive CSV with one row per test x method x network.
Includes: simulation values, complexity, path length, reconciliation metadata,
gene modifiers, exogenous supply, evidence DOIs — everything needed for figures.

Usage:
    python export_master_csv.py <base_dir> [--output <output_dir>]

Output:
    <output_dir>/master_test_level.csv
"""

import csv
import json
import sys
from collections import deque
from pathlib import Path

NETWORKS = [
    ("Arabidopsis/flowering_time_network", "Flowering Time", "Arabidopsis thaliana"),
    ("Arabidopsis/hypocotyl_length_network", "Hypocotyl Length", "Arabidopsis thaliana"),
    ("Arabidopsis/lateral_root_density_network", "Lateral Root", "Arabidopsis thaliana"),
    ("Arabidopsis/plant_height_network", "Plant Height", "Arabidopsis thaliana"),
    ("Arabidopsis/seed_size_network", "Seed Size", "Arabidopsis thaliana"),
    ("Arabidopsis/shoot_branching_network", "Shoot Branching", "Arabidopsis thaliana"),
    ("OtherSpecies/kernel_row_number_network", "Kernel Row Number", "Zea mays"),
    ("OtherSpecies/lycopene_production_network", "Lycopene", "Solanum lycopersicum"),
    ("OtherSpecies/rice_tillering_network", "Rice Tillering", "Oryza sativa"),
    ("OtherSpecies/sg_lignin_ratio_network", "Lignin S/G", "Populus trichocarpa"),
    ("OtherSpecies/sorghum_flowering_time_network", "Sorghum FT", "Sorghum bicolor"),
    ("OtherSpecies/wheat_plant_height_network", "Wheat Height", "Triticum aestivum"),
    ("strawberry_flowering_network", "Strawberry FT", "Fragaria vesca / Fragaria x ananassa"),
    ("networks/Drought_Tolerance", "Drought Tolerance", "Sorghum bicolor"),
    ("networks/Relative_Electrolyte_Leakage", "Relative Electrolyte Leakage", "Sorghum bicolor"),
]

METHODS = [
    ("script_validation_results.json", "Algebraic"),
    ("ode_validation_results.json", "ODE"),
    ("rwr_validation_results.json", "RWR"),
]

FIELDNAMES = [
    # Identity
    "network", "species", "method",
    "test_id", "gene", "perturbation_type",
    # Reconciliation
    "in_network", "network_gene", "reconciliation_type",
    "gene_modifiers", "n_gene_mods",
    "exogenous_supply", "n_exogenous",
    "comparison_baseline",
    "expected_magnitude", "notes",
    # Classification
    "modelled_as", "complexity_score", "complexity_label",
    # Biological expectation
    "expected_direction",
    # Simulation results (numerical)
    "wt_value", "perturbed_value", "comparison_baseline_value",
    "ratio", "log2_fold_change",
    "direction_threshold",
    # Simulation prediction
    "predicted_direction", "correct",
    # Convergence
    "converged", "iterations",
    # Graph topology
    "path_length", "path",
    "phenotype_node",
    # Evidence
    "evidence_doi",
]


def load_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_reconciled(net_path):
    """Load reconciled perturbation dataset into a dict keyed by test_id."""
    d = load_json(net_path / "data" / "reconciled_perturbation_dataset.json")
    if not d:
        return {}
    mapping = {}
    for p in d.get("perturbations", []):
        tid = p.get("test_id", p.get("id", ""))

        # network_gene: always list
        net_gene = p.get("network_gene", p.get("ng")) or []
        if isinstance(net_gene, str):
            net_gene = [net_gene]

        # gene_modifiers: always dict
        gm = p.get("gene_modifiers", p.get("m")) or {}
        if not isinstance(gm, dict):
            gm = {}

        # exogenous_supply: always flat dict {node: value}
        exo = p.get("exogenous_supply", p.get("exo")) or {}
        if isinstance(exo, dict) and "node" in exo:
            exo = {exo["node"]: exo.get("value", exo.get("amount", 1.0))}

        # Count modified nodes (modifier != 1.0)
        mod_nodes = [n for n, m in gm.items() if m != 1.0]
        n_exo = len(exo) if isinstance(exo, dict) and "node" not in exo else (1 if exo else 0)

        mapping[tid] = {
            "in_network": p.get("in_network", True),
            "network_gene": net_gene,
            "gene_modifiers": gm,
            "mod_nodes": mod_nodes,
            "n_gene_mods": len(mod_nodes),
            "exogenous_supply": exo,
            "n_exo": n_exo,
            "reconciliation_type": p.get("reconciliation_type", p.get("rt", "")),
            "expected_magnitude": p.get("expected_magnitude", ""),
            "notes": p.get("notes", ""),
        }
    return mapping


def classify_perturbation(n_gm, n_exo):
    """Classify perturbation by complexity into a human-readable label."""
    if n_gm == 0 and n_exo == 0:
        return "control"
    elif n_gm == 0:
        return "treatment_only"
    elif n_gm == 1 and n_exo == 0:
        return "single_mutation"
    elif n_gm == 1:
        return "mutation_plus_treatment"
    elif n_gm == 2 and n_exo == 0:
        return "double_mutation"
    elif n_gm == 2:
        return "double_mutation_plus_treatment"
    elif n_exo == 0:
        return f"multi_mutation_{n_gm}"
    else:
        return f"multi_mutation_{n_gm}_plus_treatment"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="FLASH-P v1.0 Master CSV Export")
    parser.add_argument("base_dir", nargs="?",
                        default=str(Path(__file__).parent.parent.parent))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    base = Path(args.base_dir)
    out_dir = Path(args.output) if args.output else base / "Fig_Data"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("FLASH-P v1.0 — Master CSV Export")
    print(f"Base: {base}")
    print(f"Output: {out_dir}")
    print()

    rows = []

    for net_str, net_label, default_species in NETWORKS:
        net_path = base / net_str

        # Load reconciled perturbation metadata
        recon = load_reconciled(net_path)

        # Try to get species from network.json metadata
        net_data = load_json(net_path / "network" / "network.json")
        species = default_species
        if net_data:
            species = net_data.get("metadata", {}).get("species", default_species)

        for mfile, mname in METHODS:
            d = load_json(net_path / "validation" / mfile)
            if not d:
                continue

            for r in d.get("detailed_results", []):
                tid = r.get("test_id", "")
                m = recon.get(tid, {})

                net_genes = m.get("network_gene", [])
                gm = m.get("gene_modifiers", {})
                mod_nodes = m.get("mod_nodes", [])
                exo = m.get("exogenous_supply", {})
                n_gm = m.get("n_gene_mods", 0)
                n_exo = m.get("n_exo", 0)

                # If reconciled data is missing, try to infer from detailed_results
                if not m:
                    gene = r.get("gene", "")
                    gm_val = r.get("gene_modifier")
                    exo_node = r.get("exogenous_node")
                    exo_val = r.get("exogenous_value")
                    if gene and gm_val is not None and gm_val != 1.0:
                        gm = {gene: gm_val}
                        mod_nodes = [gene]
                        n_gm = 1
                    if exo_node:
                        exo = {exo_node: exo_val or 1.0}
                        n_exo = 1

                modelled_as = classify_perturbation(n_gm, n_exo)
                complexity_score = r.get("complexity_score", max(n_gm + n_exo, 1))
                complexity_label = r.get("complexity_label", "")
                if not complexity_label:
                    total = n_gm + n_exo
                    if total <= 0:
                        total = 1
                    complexity_label = "easy" if total == 1 else ("medium" if total == 2 else "hard")

                # Format gene_modifiers and exogenous for CSV
                gm_str = "; ".join(f"{k}={v}" for k, v in gm.items()) if gm else ""
                exo_str = "; ".join(f"{k}={v}" for k, v in exo.items()) if exo else ""

                # Path as comma-separated string
                path_list = r.get("path") or []
                path_str = " > ".join(path_list) if path_list else ""

                rows.append({
                    "network": net_label,
                    "species": species,
                    "method": mname,
                    "test_id": tid,
                    "gene": r.get("gene", ""),
                    "perturbation_type": r.get("perturbation_type", ""),
                    "in_network": m.get("in_network", True),
                    "network_gene": ",".join(net_genes) if net_genes else r.get("gene", ""),
                    "reconciliation_type": m.get("reconciliation_type", ""),
                    "gene_modifiers": gm_str,
                    "n_gene_mods": n_gm,
                    "exogenous_supply": exo_str,
                    "n_exogenous": n_exo,
                    "comparison_baseline": r.get("comparison_baseline", "WT"),
                    "expected_magnitude": m.get("expected_magnitude", ""),
                    "notes": m.get("notes", ""),
                    "modelled_as": modelled_as,
                    "complexity_score": complexity_score,
                    "complexity_label": complexity_label,
                    "expected_direction": r.get("expected_direction", ""),
                    "wt_value": r.get("wt_value", ""),
                    "perturbed_value": r.get("perturbed_value", ""),
                    "comparison_baseline_value": r.get("comparison_baseline_value", ""),
                    "ratio": r.get("ratio", ""),
                    "log2_fold_change": r.get("log2_fold_change", ""),
                    "direction_threshold": r.get("direction_threshold", ""),
                    "predicted_direction": r.get("predicted_direction", ""),
                    "correct": 1 if r.get("correct") else 0,
                    "converged": 1 if r.get("converged") else 0,
                    "iterations": r.get("iterations", ""),
                    "path_length": r.get("path_length", ""),
                    "path": path_str,
                    "phenotype_node": r.get("phenotype_node", ""),
                    "evidence_doi": r.get("evidence_doi", ""),
                })

    if not rows:
        print("No data found across any networks.")
        sys.exit(1)

    # Write master CSV
    out_path = out_dir / "master_test_level.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # Summary stats
    networks_found = len(set(r["network"] for r in rows))
    methods_found = len(set(r["method"] for r in rows))
    tests_total = len(rows)
    correct = sum(r["correct"] for r in rows)

    print(f"  master_test_level.csv: {tests_total} rows")
    print(f"    Networks: {networks_found}")
    print(f"    Methods:  {methods_found}")
    print(f"    Correct:  {correct}/{tests_total} ({round(correct/tests_total*100, 1)}%)")
    print()

    # Also print per-network x method breakdown
    print("  Per-network accuracy:")
    from collections import defaultdict
    acc = defaultdict(lambda: {"c": 0, "t": 0})
    for r in rows:
        k = (r["network"], r["method"])
        acc[k]["t"] += 1
        acc[k]["c"] += r["correct"]

    current_net = None
    for (net, meth), g in sorted(acc.items()):
        if net != current_net:
            print(f"    {net}:")
            current_net = net
        a = round(g["c"] / g["t"] * 100, 1) if g["t"] else 0
        print(f"      {meth:12s}: {g['c']:3d}/{g['t']:3d} = {a}%")

    print(f"\nMaster CSV saved to: {out_path}")


if __name__ == "__main__":
    main()

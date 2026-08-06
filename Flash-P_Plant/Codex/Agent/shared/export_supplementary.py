#!/usr/bin/env python3
"""
================================================================================
EXPORT SUPPLEMENTARY - Generate publication-ready CSV tables
================================================================================

Reads all JSON outputs from a network directory and produces CSV files
suitable for supplementary material in a publication.

USAGE:
    python export_supplementary.py <network_dir>
    python export_supplementary.py shoot_branching_network

OUTPUT:
    <network_dir>/supplementary/
        Table_S1_edges.csv
        Table_S2_perturbations.csv
        Table_S3_reconciled_perturbations.csv
        Table_S4_algebraic_equations.csv
        Table_S5_ode_equations.csv
        Table_S7a-c_*_results.csv
        Table_S8_method_comparison.csv
        master_test_level.csv          (comprehensive: values, complexity, paths)
        Fig_Data/
            accuracy_summary.csv
            complexity_accuracy.csv
            pathlength_accuracy.csv
            edge_list.csv
            evidence_per_edge.csv

================================================================================
"""

import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from flashp_version import get_version


def safe_get(d: Any, *keys, default=""):
    """Safely traverse nested dicts."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d if d is not None else default


def export_edges(data: Dict, output_path: Path):
    """Export curated_edges.json to CSV with properly split evidence columns."""
    edges = data.get('edges', [])
    if not edges:
        return

    fieldnames = ['source', 'source_type', 'target', 'target_type',
                  'effect', 'edge_type', 'confidence', 'mechanism',
                  'doi', 'paper_title', 'authors', 'year', 'journal',
                  'evidence_sentence', 'verification', 'species_validated']

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for edge in edges:
            # Extract evidence from the first evidence entry
            ev_list = edge.get('evidence', [])
            ev = ev_list[0] if isinstance(ev_list, list) and ev_list else (ev_list if isinstance(ev_list, dict) else {})
            src = ev.get('source', {}) if isinstance(ev, dict) else {}
            doi = edge.get('doi', '') or (ev.get('doi', '') if isinstance(ev, dict) else '') or src.get('doi', '')
            species_validated = edge.get('species_validated', [])
            if isinstance(species_validated, list):
                species_validated = ','.join(species_validated)

            writer.writerow({
                'source': edge.get('source', ''),
                'source_type': edge.get('source_type', ''),
                'target': edge.get('target', ''),
                'target_type': edge.get('target_type', ''),
                'effect': edge.get('effect', ''),
                'edge_type': edge.get('edge_type', ''),
                'confidence': edge.get('confidence', ''),
                'mechanism': edge.get('mechanism', ''),
                'doi': doi,
                'paper_title': src.get('title', ''),
                'authors': src.get('authors', ''),
                'year': src.get('year', ''),
                'journal': src.get('journal', ''),
                'evidence_sentence': ev.get('evidence_sentence', ''),
                'verification': src.get('verification', ''),
                'species_validated': species_validated,
            })

    print(f"  Exported {len(edges)} edges to {output_path.name}")


def export_perturbations(data: Dict, output_path: Path):
    """Export perturbation_dataset.json to CSV with properly split evidence columns."""
    perts = data.get('perturbations', [])
    if not perts:
        return

    fieldnames = ['test_id', 'gene', 'perturbation_type', 'network_gene',
                  'gene_modifier', 'gene_modifiers', 'exogenous_node',
                  'exogenous_value', 'exogenous_supply', 'comparison_baseline',
                  'reconciliation_type',
                  'phenotype_node', 'expected_direction', 'expected_magnitude',
                  'doi', 'paper_title', 'authors', 'year', 'journal',
                  'evidence_sentence', 'verification',
                  'evidence_quality', 'species', 'notes']

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in perts:
            exo = p.get('exogenous_supply', {}) or {}
            gene_modifiers = p.get('gene_modifiers', {}) or {}
            if isinstance(exo, dict) and 'node' in exo:
                exogenous_node = exo.get('node', '')
                exogenous_value = exo.get('value', '')
            elif isinstance(exo, dict):
                exogenous_node = ';'.join(str(k) for k in exo.keys())
                exogenous_value = ';'.join(str(v) for v in exo.values())
            else:
                exogenous_node = ''
                exogenous_value = ''
            # Extract evidence from first entry
            ev_list = p.get('evidence', [])
            ev = ev_list[0] if isinstance(ev_list, list) and ev_list else {}
            src = ev.get('source', {}) if isinstance(ev, dict) else {}
            if isinstance(ev, dict) and not src:
                src = ev
            doi = p.get('doi', '') or (ev.get('doi', '') if isinstance(ev, dict) else '') or src.get('doi', '')
            metadata = data.get('metadata', {}) if isinstance(data.get('metadata'), dict) else {}

            writer.writerow({
                'test_id': p.get('test_id', ''),
                'gene': p.get('gene', ''),
                'perturbation_type': p.get('perturbation_type', ''),
                'network_gene': ';'.join(p.get('network_gene', [])) if isinstance(p.get('network_gene'), list) else p.get('network_gene', ''),
                'gene_modifier': p.get('gene_modifier', ''),
                'gene_modifiers': json.dumps(gene_modifiers, sort_keys=True) if gene_modifiers else '',
                'exogenous_node': exogenous_node,
                'exogenous_value': exogenous_value,
                'exogenous_supply': json.dumps(exo, sort_keys=True) if exo else '',
                'comparison_baseline': p.get('comparison_baseline', ''),
                'reconciliation_type': p.get('reconciliation_type', ''),
                'phenotype_node': p.get('phenotype_node', '') or metadata.get('phenotype_node', ''),
                'expected_direction': p.get('expected_direction', ''),
                'expected_magnitude': p.get('expected_magnitude', ''),
                'doi': doi,
                'paper_title': src.get('title', ''),
                'authors': src.get('authors', ''),
                'year': src.get('year', ''),
                'journal': src.get('journal', ''),
                'evidence_sentence': ev.get('evidence_sentence', ''),
                'verification': src.get('verification', ''),
                'evidence_quality': p.get('evidence_quality', ''),
                'species': p.get('species', '') or metadata.get('species', ''),
                'notes': p.get('notes', ''),
            })

    print(f"  Exported {len(perts)} perturbations to {output_path.name}")


def export_equations(data: Dict, output_path: Path, kind: str = "algebraic"):
    """Export algebraic_equations.json or ode_equations.json to CSV."""
    equations = data.get('equations', [])
    if not equations:
        return

    fieldnames = ['node', 'node_type', 'activators', 'inhibitors', 'formula']

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for eq in equations:
            writer.writerow({
                'node': eq.get('node', ''),
                'node_type': eq.get('node_type', '') or eq.get('type', ''),
                'activators': ','.join(eq.get('activators', [])),
                'inhibitors': ','.join(eq.get('inhibitors', [])),
                'formula': eq.get('formula', ''),
            })

    print(f"  Exported {len(equations)} {kind} equations to {output_path.name}")


def export_method_comparison(network_dir: Path, output_path: Path):
    """
    Export per-method comparison CSV with accuracy, kappa+CI, MCC, and FRS.

    Reads directly from the three validation_results.json files (which contain
    the rigor_score and tier2_scope fields added in v1.0 metric upgrade).
    Falls back to method_comparison.json for parameters if available.
    """
    method_files = [
        ("script_validation_results.json", "Algebraic"),
        ("ode_validation_results.json", "ODE"),
        ("rwr_validation_results.json", "RWR"),
    ]

    # Pull parameter strings from method_comparison.json if present
    mc_path = network_dir / "validation" / "method_comparison.json"
    param_lookup: Dict[str, str] = {}
    if mc_path.exists():
        with open(mc_path, "r", encoding="utf-8") as f:
            mc_data = json.load(f)
        for entry in mc_data.get("comparison", []):
            name = entry.get("method", "").lower()
            short = "ODE" if "ode" in name else ("RWR" if "rwr" in name else "Algebraic")
            param_lookup[short] = entry.get("best_params", "")

    fieldnames = [
        "method", "accuracy", "kappa", "kappa_ci_lower", "kappa_ci_upper",
        "kappa_band", "mcc",
        "rigor_score", "rigor_band",
        "dars", "dars_band",
        "n_nodes", "n_edges", "n_tests", "t_effective", "mean_path_length",
        "easy_n", "easy_acc", "easy_kappa",
        "medium_n", "medium_acc", "medium_kappa",
        "hard_n", "hard_acc", "hard_kappa",
        "convergence_rate", "parameters",
    ]

    rows = []
    for vfile, mname in method_files:
        vpath = network_dir / "validation" / vfile
        if not vpath.exists():
            continue
        with open(vpath, "r", encoding="utf-8") as f:
            d = json.load(f)
        m = d.get("metrics", {})
        scope = m.get("tier2_scope", {}) or {}
        strat = m.get("stratified", {}) or {}
        easy = strat.get("easy", {}) or {}
        med = strat.get("medium", {}) or {}
        hard = strat.get("hard", {}) or {}
        rows.append({
            "method": mname,
            "accuracy": m.get("overall_accuracy", ""),
            "kappa": m.get("cohens_kappa", ""),
            "kappa_ci_lower": m.get("kappa_ci_lower", ""),
            "kappa_ci_upper": m.get("kappa_ci_upper", ""),
            "kappa_band": m.get("kappa_band", ""),
            "mcc": m.get("mcc", ""),
            "rigor_score": m.get("rigor_score", ""),
            "rigor_band": m.get("rigor_band", ""),
            "dars": m.get("dars", ""),
            "dars_band": m.get("dars_band", ""),
            "n_nodes": scope.get("n_nodes", ""),
            "n_edges": scope.get("n_edges", ""),
            "n_tests": scope.get("n_tests", ""),
            "t_effective": scope.get("t_effective", ""),
            "mean_path_length": scope.get("mean_path_length", ""),
            "easy_n": easy.get("n", ""),
            "easy_acc": easy.get("accuracy_pct", ""),
            "easy_kappa": easy.get("cohens_kappa", ""),
            "medium_n": med.get("n", ""),
            "medium_acc": med.get("accuracy_pct", ""),
            "medium_kappa": med.get("cohens_kappa", ""),
            "hard_n": hard.get("n", ""),
            "hard_acc": hard.get("accuracy_pct", ""),
            "hard_kappa": hard.get("cohens_kappa", ""),
            "convergence_rate": m.get("convergence_rate", ""),
            "parameters": param_lookup.get(mname, ""),
        })

    if not rows:
        return

    # Append BEST row (method with highest DARS — falls back to FRS if DARS absent)
    def _score_for_ranking(r):
        v = r.get("dars") or r.get("rigor_score")
        return v if isinstance(v, (int, float)) else float("-inf")

    if rows and any(isinstance(r.get("rigor_score"), (int, float)) for r in rows):
        best_row = max(rows, key=_score_for_ranking)
        best_copy = dict(best_row)
        best_copy["method"] = "BEST"
        best_copy["convergence_rate"] = ""
        best_copy["parameters"] = f"best method: {best_row['method']}"
        rows.append(best_copy)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Exported {len(rows)} rows to {output_path.name}")


def export_stratified_results(network_dir: Path, output_path: Path):
    """
    Export Table_S9 — per-method stratified results by difficulty.

    One row per (method × stratum): accuracy, kappa, n, correct.
    Reads stratified data from the three validation_results.json files.
    """
    method_files = [
        ("script_validation_results.json", "Algebraic"),
        ("ode_validation_results.json", "ODE"),
        ("rwr_validation_results.json", "RWR"),
    ]

    fieldnames = [
        "method", "stratum", "n", "correct", "accuracy_pct",
        "cohens_kappa", "kappa_note",
    ]

    rows = []
    for vfile, mname in method_files:
        vpath = network_dir / "validation" / vfile
        if not vpath.exists():
            continue
        with open(vpath, "r", encoding="utf-8") as f:
            d = json.load(f)
        strat = d.get("metrics", {}).get("stratified", {}) or {}
        for stratum in ["easy", "medium", "hard"]:
            data = strat.get(stratum, {}) or {}
            rows.append({
                "method": mname,
                "stratum": stratum,
                "n": data.get("n", ""),
                "correct": data.get("correct", ""),
                "accuracy_pct": data.get("accuracy_pct", ""),
                "cohens_kappa": data.get("cohens_kappa", ""),
                "kappa_note": data.get("kappa_note", "") or "",
            })

    if not rows:
        return

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Exported {len(rows)} rows to {output_path.name}")


# ---------------------------------------------------------------------------
# Master test-level CSV (per-network, comprehensive)
# ---------------------------------------------------------------------------

MASTER_FIELDNAMES = [
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

VALIDATION_FILES = [
    ("script_validation_results.json", "Algebraic"),
    ("ode_validation_results.json", "ODE"),
    ("rwr_validation_results.json", "RWR"),
]


def _load_reconciled(network_dir: Path) -> Dict:
    """Load reconciled perturbation dataset into a dict keyed by test_id."""
    recon_path = network_dir / "data" / "reconciled_perturbation_dataset.json"
    if not recon_path.exists():
        return {}
    import light_io  # Light: expand short-key/TOON reconciled -> rich
    d = light_io.load(str(recon_path))
    raw_dois: Dict[str, str] = {}
    raw_path = network_dir / "data" / "perturbation_dataset.json"
    if raw_path.exists():
        raw = light_io.load(str(raw_path))
        raw_dois = {
            p.get("test_id", ""): p.get("doi", "")
            for p in raw.get("perturbations", [])
            if p.get("test_id")
        }
    mapping: Dict[str, Dict] = {}
    for p in d.get("perturbations", []):
        tid = p.get("test_id", "")
        net_gene = p.get("network_gene") or []
        if isinstance(net_gene, str):
            net_gene = [net_gene]
        gm = p.get("gene_modifiers") or {}
        if not isinstance(gm, dict):
            gm = {}
        exo = p.get("exogenous_supply") or {}
        if isinstance(exo, dict) and "node" in exo:
            exo = {exo["node"]: exo.get("value", exo.get("amount", 1.0))}
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
            "reconciliation_type": p.get("reconciliation_type", ""),
            "expected_magnitude": p.get("expected_magnitude", ""),
            "notes": p.get("notes", ""),
            "doi": p.get("doi", "") or raw_dois.get(tid, ""),
        }
    return mapping


def _classify_perturbation(n_gm: int, n_exo: int) -> str:
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


def export_master_test_level(network_dir: Path, output_path: Path):
    """Export comprehensive per-test CSV with simulation values, complexity, paths."""
    # Determine network label and species from network.json metadata
    net_json_path = network_dir / "network" / "network.json"
    net_label = network_dir.name.replace("_network", "").replace("_", " ").title()
    species = ""
    if net_json_path.exists():
        import light_io  # Light
        nd = light_io.load(str(net_json_path))
        meta = nd.get("metadata", {})
        species = meta.get("species", "")
        if meta.get("phenotype"):
            net_label = meta["phenotype"].replace("_", " ").title()

    recon = _load_reconciled(network_dir)
    rows: List[Dict] = []

    for mfile, mname in VALIDATION_FILES:
        vpath = network_dir / "validation" / mfile
        if not vpath.exists():
            continue
        with open(vpath, "r", encoding="utf-8") as f:
            d = json.load(f)

        for r in d.get("detailed_results", []):
            tid = r.get("test_id", "")
            m = recon.get(tid, {})

            net_genes = m.get("network_gene", [])
            gm = m.get("gene_modifiers", {})
            mod_nodes = m.get("mod_nodes", [])
            exo = m.get("exogenous_supply", {})
            n_gm = m.get("n_gene_mods", 0)
            n_exo = m.get("n_exo", 0)

            # Fallback: infer from detailed_results when reconciled data is missing
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

            modelled_as = _classify_perturbation(n_gm, n_exo)
            complexity_score = r.get("complexity_score", max(n_gm + n_exo, 1))
            complexity_label = r.get("complexity_label", "")
            if not complexity_label:
                total = max(n_gm + n_exo, 1)
                complexity_label = "easy" if total == 1 else ("medium" if total == 2 else "hard")

            gm_str = "; ".join(f"{k}={v}" for k, v in gm.items()) if gm else ""
            exo_str = "; ".join(f"{k}={v}" for k, v in exo.items()) if exo else ""
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
                "evidence_doi": r.get("evidence_doi", "") or m.get("doi", ""),
            })

    if not rows:
        print("  WARNING: No validation results found for master CSV")
        return

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    n_methods = len(set(r["method"] for r in rows))
    correct = sum(r["correct"] for r in rows)
    total = len(rows)
    print(f"  Exported {total} rows ({total // n_methods} tests x {n_methods} methods) "
          f"to {output_path.name}  [{correct}/{total} correct = "
          f"{round(correct / total * 100, 1)}%]")
    return rows


# ---------------------------------------------------------------------------
# Fig_Data CSVs (per-network analysis summaries)
# ---------------------------------------------------------------------------

def export_fig_data(network_dir: Path, master_rows: List[Dict], output_dir: Path):
    """Generate Fig_Data analysis CSVs from master_test_level rows."""
    from collections import defaultdict

    output_dir.mkdir(parents=True, exist_ok=True)

    if not master_rows:
        return

    net_label = master_rows[0]["network"]

    # --- accuracy_summary.csv ---
    acc = defaultdict(lambda: {"c": 0, "t": 0})
    for r in master_rows:
        acc[r["method"]]["t"] += 1
        acc[r["method"]]["c"] += r["correct"]

    p = output_dir / "accuracy_summary.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["network", "method", "n_correct", "n_total", "accuracy_pct"])
        for meth in sorted(acc):
            g = acc[meth]
            a = round(g["c"] / g["t"] * 100, 1) if g["t"] else 0
            w.writerow([net_label, meth, g["c"], g["t"], a])
    print(f"  Fig_Data: accuracy_summary.csv ({len(acc)} methods)")

    # --- complexity_accuracy.csv ---
    groups = defaultdict(lambda: {"c": 0, "t": 0})
    for r in master_rows:
        k = (r["method"], r["complexity_label"], r["complexity_score"])
        groups[k]["t"] += 1
        groups[k]["c"] += r["correct"]

    p = output_dir / "complexity_accuracy.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["network", "method", "complexity_label", "complexity_score",
                     "n_correct", "n_total", "accuracy_pct"])
        for (meth, label, score), g in sorted(groups.items()):
            a = round(g["c"] / g["t"] * 100, 2) if g["t"] else 0
            w.writerow([net_label, meth, label, score, g["c"], g["t"], a])
    print(f"  Fig_Data: complexity_accuracy.csv ({len(groups)} groups)")

    # --- pathlength_accuracy.csv ---
    pl_groups = defaultdict(lambda: {"c": 0, "t": 0})
    for r in master_rows:
        sp = r.get("path_length")
        if not isinstance(sp, int) or sp < 0:
            continue
        pl = min(sp, 7)
        k = (r["method"], pl)
        pl_groups[k]["t"] += 1
        pl_groups[k]["c"] += r["correct"]

    p = output_dir / "pathlength_accuracy.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["network", "method", "path_length", "n_correct", "n_total", "accuracy_pct"])
        for (meth, pl), g in sorted(pl_groups.items()):
            a = round(g["c"] / g["t"] * 100, 2) if g["t"] else 0
            w.writerow([net_label, meth, pl, g["c"], g["t"], a])
    print(f"  Fig_Data: pathlength_accuracy.csv ({len(pl_groups)} groups)")

    # --- edge_list.csv ---
    net_json = network_dir / "network" / "network.json"
    if net_json.exists():
        import light_io  # Light
        nd = light_io.load(str(net_json))
        node_types = {n["id"]: n.get("type", "") for n in nd.get("nodes", [])}
        edge_rows = []
        for e in nd.get("edges", []):
            ev = e.get("evidence") or []
            dois = []
            for x in ev:
                if isinstance(x, dict):
                    doi = x.get("doi", "")
                    if not doi and "source" in x:
                        doi = x["source"].get("doi", "")
                    if doi:
                        dois.append(doi)
            edge_rows.append({
                "network": net_label,
                "edge_id": e.get("edge_id", ""),
                "source": e["source"],
                "source_type": node_types.get(e["source"], ""),
                "target": e["target"],
                "target_type": node_types.get(e["target"], ""),
                "sign": e.get("sign", ""),
                "effect": e.get("effect", ""),
                "mechanism": e.get("mechanism", ""),
                "n_evidence": len(ev),
                "doi_list": "; ".join(dois),
            })

        p = output_dir / "edge_list.csv"
        if edge_rows:
            fields = list(edge_rows[0].keys())
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()
                csv.DictWriter(f, fieldnames=fields).writerows(edge_rows)
            print(f"  Fig_Data: edge_list.csv ({len(edge_rows)} edges)")

    # --- evidence_per_edge.csv ---
    curated_path = network_dir / "data" / "curated_edges.json"
    if curated_path.exists():
        import light_io  # Light
        cd = light_io.load(str(curated_path))
        ev_rows = []
        for e in cd.get("edges", []):
            ev = e.get("evidence", [])
            dois = set()
            for x in ev:
                if isinstance(x, dict):
                    doi = x.get("doi", "")
                    if not doi and "source" in x:
                        doi = x["source"].get("doi", "")
                    if doi:
                        dois.add(doi)
            ev_rows.append({
                "network": net_label,
                "edge_id": e.get("edge_id", ""),
                "source": e.get("source", ""),
                "target": e.get("target", ""),
                "sign": e.get("sign", ""),
                "confidence": e.get("confidence", ""),
                "in_model": e.get("in_model", ""),
                "n_papers": len(dois),
                "dois": "; ".join(sorted(dois)),
            })

        p = output_dir / "evidence_per_edge.csv"
        if ev_rows:
            fields = list(ev_rows[0].keys())
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()
                csv.DictWriter(f, fieldnames=fields).writerows(ev_rows)
            print(f"  Fig_Data: evidence_per_edge.csv ({len(ev_rows)} edges)")

    # --- master_test_level.csv (copy into Fig_Data) ---
    p = output_dir / "master_test_level.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_FIELDNAMES)
        writer.writeheader()
        writer.writerows(master_rows)
    print(f"  Fig_Data: master_test_level.csv ({len(master_rows)} rows)")

    # --- all_networks_test_level.csv (simpler version matching export_all_csvs.py format) ---
    simple_fields = [
        "network", "method", "test_id", "gene", "mapped_node",
        "perturbation_type", "modelled_as", "treatment",
        "predicted_direction", "expected_direction", "correct",
        "phenotype_node", "shortest_path_length",
        "complexity_score", "complexity_label",
    ]
    simple_rows = []
    for r in master_rows:
        simple_rows.append({
            "network": r["network"],
            "method": r["method"],
            "test_id": r["test_id"],
            "gene": r["gene"],
            "mapped_node": r["network_gene"],
            "perturbation_type": r["perturbation_type"],
            "modelled_as": r["modelled_as"],
            "treatment": r["exogenous_supply"],
            "predicted_direction": r["predicted_direction"],
            "expected_direction": r["expected_direction"],
            "correct": r["correct"],
            "phenotype_node": r["phenotype_node"],
            "shortest_path_length": r["path_length"],
            "complexity_score": r["complexity_score"],
            "complexity_label": r["complexity_label"],
        })

    p = output_dir / "all_networks_test_level.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=simple_fields)
        writer.writeheader()
        writer.writerows(simple_rows)
    print(f"  Fig_Data: all_networks_test_level.csv ({len(simple_rows)} rows)")

    # --- network_summary.csv (one-row summary with accuracy + FRS per method) ---
    acc_by_method = defaultdict(lambda: {"c": 0, "t": 0})
    for r in master_rows:
        acc_by_method[r["method"]]["t"] += 1
        acc_by_method[r["method"]]["c"] += r["correct"]

    species = master_rows[0].get("species", "") if master_rows else ""
    n_nodes = 0
    n_edges = 0
    if net_json.exists():
        import light_io  # Light
        nd = light_io.load(str(net_json))
        n_nodes = nd.get("metadata", {}).get("total_nodes", len(nd.get("nodes", [])))
        n_edges = nd.get("metadata", {}).get("total_edges", len(nd.get("edges", [])))

    alg = acc_by_method.get("Algebraic", {"c": 0, "t": 0})
    ode = acc_by_method.get("ODE", {"c": 0, "t": 0})
    rwr = acc_by_method.get("RWR", {"c": 0, "t": 0})

    # Pull per-method kappa, FRS, DARS, t_effective from validation results files
    val_files = [
        ("script_validation_results.json", "alg"),
        ("ode_validation_results.json", "ode"),
        ("rwr_validation_results.json", "rwr"),
    ]
    method_stats: Dict[str, Dict] = {}
    t_effective = ""
    for vfile, key in val_files:
        vpath = network_dir / "validation" / vfile
        if vpath.exists():
            with open(vpath, "r", encoding="utf-8") as f:
                vd = json.load(f)
            m = vd.get("metrics", {})
            scope = m.get("tier2_scope", {}) or {}
            if t_effective == "" and scope.get("t_effective") is not None:
                t_effective = scope.get("t_effective", "")
            method_stats[key] = {
                "kappa": m.get("cohens_kappa", ""),
                "mcc": m.get("mcc", ""),
                "frs": m.get("rigor_score", ""),
                "frs_band": m.get("rigor_band", ""),
                "dars": m.get("dars", ""),
                "dars_band": m.get("dars_band", ""),
            }
        else:
            method_stats[key] = {
                "kappa": "", "mcc": "", "frs": "", "frs_band": "",
                "dars": "", "dars_band": "",
            }

    # FRS_best and DARS_best = max across methods
    numeric_frs = [s["frs"] for s in method_stats.values() if isinstance(s["frs"], (int, float))]
    frs_best = max(numeric_frs) if numeric_frs else ""
    numeric_dars = [s["dars"] for s in method_stats.values() if isinstance(s["dars"], (int, float))]
    dars_best = max(numeric_dars) if numeric_dars else ""

    frs_best_band = ""
    dars_best_band = ""
    try:
        from rigor_score import band_label
        if numeric_frs:
            frs_best_band = band_label(frs_best)
        if numeric_dars:
            dars_best_band = band_label(dars_best)
    except Exception:
        pass

    p = output_dir / "network_summary.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "network", "species", "n_nodes", "n_edges", "n_tests", "t_effective",
            "algebraic_acc", "ode_acc", "rwr_acc",
            "algebraic_kappa", "ode_kappa", "rwr_kappa",
            "algebraic_frs", "ode_frs", "rwr_frs",
            "algebraic_dars", "ode_dars", "rwr_dars",
            "frs_best", "frs_best_band",
            "dars_best", "dars_best_band",
        ])
        w.writerow([
            net_label, species, n_nodes, n_edges, alg["t"], t_effective,
            round(alg["c"] / alg["t"] * 100, 1) if alg["t"] else 0,
            round(ode["c"] / ode["t"] * 100, 1) if ode["t"] else 0,
            round(rwr["c"] / rwr["t"] * 100, 1) if rwr["t"] else 0,
            method_stats["alg"]["kappa"],
            method_stats["ode"]["kappa"],
            method_stats["rwr"]["kappa"],
            method_stats["alg"]["frs"],
            method_stats["ode"]["frs"],
            method_stats["rwr"]["frs"],
            method_stats["alg"]["dars"],
            method_stats["ode"]["dars"],
            method_stats["rwr"]["dars"],
            frs_best, frs_best_band,
            dars_best, dars_best_band,
        ])
    print(f"  Fig_Data: network_summary.csv (1 row, FRS_best={frs_best} {frs_best_band}, "
          f"DARS_best={dars_best} {dars_best_band})")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    network_dir = Path(sys.argv[1])
    if not network_dir.is_absolute():
        network_dir = Path.cwd() / network_dir

    if not network_dir.exists():
        print(f"Error: Directory not found: {network_dir}")
        sys.exit(1)

    output_dir = network_dir / 'supplementary'
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nExporting supplementary tables from: {network_dir.name}")
    print(f"Flash-P version: {get_version()}")
    print(f"Output directory: {output_dir}")

    # Table S1: Curated edges
    edges_path = network_dir / 'data' / 'curated_edges.json'
    if edges_path.exists():
        import light_io  # Light: expand short-key/TOON -> rich (rehydrates evidence from doi)
        export_edges(light_io.load(str(edges_path)), output_dir / 'Table_S1_edges.csv')

    # Table S2: Perturbation dataset
    pert_path = network_dir / 'data' / 'perturbation_dataset.json'
    if pert_path.exists():
        import light_io
        export_perturbations(light_io.load(str(pert_path)), output_dir / 'Table_S2_perturbations.csv')

    # Table S3: Reconciled perturbations
    recon_path = network_dir / 'data' / 'reconciled_perturbation_dataset.json'
    if recon_path.exists():
        import light_io
        recon_data = light_io.load(str(recon_path))
        raw_dois: Dict[str, str] = {}
        if pert_path.exists():
            raw_data = light_io.load(str(pert_path))
            raw_dois = {
                p.get('test_id', ''): p.get('doi', '')
                for p in raw_data.get('perturbations', [])
                if p.get('test_id')
            }
        for p in recon_data.get('perturbations', []):
            doi = p.get('doi', '') or raw_dois.get(p.get('test_id', ''), '')
            if doi:
                p['doi'] = doi
                p.setdefault('evidence', [{'doi': doi}])
        export_perturbations(recon_data, output_dir / 'Table_S3_reconciled_perturbations.csv')

    # Table S4: Algebraic equations
    alg_path = network_dir / 'network' / 'algebraic_equations.json'
    if alg_path.exists():
        import light_io
        export_equations(light_io.load(str(alg_path)), output_dir / 'Table_S4_algebraic_equations.csv', 'algebraic')

    # Table S5: ODE equations
    ode_path = network_dir / 'network' / 'ode_equations.json'
    if ode_path.exists():
        import light_io
        export_equations(light_io.load(str(ode_path)), output_dir / 'Table_S5_ode_equations.csv', 'ODE')

    # Table S7: Validation results for ALL THREE methods
    import shutil
    val_csvs = [
        ('validation_results.csv', 'Table_S7a_algebraic_results.csv'),
        ('ode_validation_results.csv', 'Table_S7b_ode_results.csv'),
        ('rwr_validation_results.csv', 'Table_S7c_rwr_results.csv'),
    ]
    for src_name, dst_name in val_csvs:
        src_path = network_dir / 'validation' / src_name
        if src_path.exists():
            shutil.copy2(src_path, output_dir / dst_name)
            print(f"  Copied {src_name} to {dst_name}")
        else:
            print(f"  WARNING: {src_name} not found")

    # Table S8: Method comparison (reads directly from validation_results.json files)
    export_method_comparison(network_dir, output_dir / 'Table_S8_method_comparison.csv')

    # Table S9: Stratified results (accuracy + kappa per difficulty stratum)
    export_stratified_results(network_dir, output_dir / 'Table_S9_stratified_results.csv')

    # Master test-level CSV (comprehensive: values, complexity, paths, evidence)
    master_rows = export_master_test_level(network_dir, output_dir / 'master_test_level.csv')

    # Fig_Data analysis CSVs (per-network)
    fig_dir = output_dir / 'Fig_Data'
    if master_rows:
        export_fig_data(network_dir, master_rows, fig_dir)

    print(f"\nDone. All supplementary tables saved to: {output_dir}")


if __name__ == '__main__':
    main()

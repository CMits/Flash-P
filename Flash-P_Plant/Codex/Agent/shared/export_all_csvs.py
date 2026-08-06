#!/usr/bin/env python3
"""
FLASH-P v1.0 — Comprehensive CSV Export

Generates all analysis-ready CSVs from validated JSON files.
Run after validation completes.

Usage:
    python export_all_csvs.py <base_dir> [--output <output_dir>]

Outputs:
    1. all_networks_test_level.csv — one row per test per method per network
    2. accuracy_summary.csv — accuracy per network per method
    3. edge_list.csv — all edges across all networks
    4. perturbation_summary.csv — all perturbations with reconciliation info
    5. network_summary.csv — one row per network
    6. complexity_accuracy.csv — accuracy by complexity group per method
    7. pathlength_accuracy.csv — accuracy by path length per method
    8. evidence_per_edge.csv — evidence strength for each edge
"""

import csv
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

NETWORKS = [
    ("Arabidopsis/flowering_time_network", "Flowering Time"),
    ("Arabidopsis/hypocotyl_length_network", "Hypocotyl Length"),
    ("Arabidopsis/lateral_root_density_network", "Lateral Root"),
    ("Arabidopsis/plant_height_network", "Plant Height"),
    ("Arabidopsis/seed_size_network", "Seed Size"),
    ("Arabidopsis/shoot_branching_network", "Shoot Branching"),
    ("OtherSpecies/kernel_row_number_network", "Kernel Row Number"),
    ("OtherSpecies/lycopene_production_network", "Lycopene"),
    ("OtherSpecies/rice_tillering_network", "Rice Tillering"),
    ("OtherSpecies/sg_lignin_ratio_network", "Lignin S/G"),
    ("OtherSpecies/sorghum_flowering_time_network", "Sorghum FT"),
    ("OtherSpecies/wheat_plant_height_network", "Wheat Height"),
    ("strawberry_flowering_network", "Strawberry FT"),
    ("networks/Drought_Tolerance", "Drought Tolerance"),
    ("networks/Relative_Electrolyte_Leakage", "Relative Electrolyte Leakage"),
]

METHODS = [
    ("script_validation_results.json", "Algebraic"),
    ("ode_validation_results.json", "ODE"),
    ("rwr_validation_results.json", "RWR"),
]


def load_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_network_graph(net_path):
    d = load_json(net_path / "network" / "network.json")
    if not d:
        return {}, set(), None
    node_ids = {n["id"] for n in d.get("nodes", [])}
    adj = {}
    for e in d.get("edges", []):
        source = e.get("source", e.get("s"))
        target = e.get("target", e.get("t"))
        if source and target:
            adj.setdefault(source, []).append(target)
    pheno = next(
        (n["id"] for n in d["nodes"] if n.get("type", n.get("ty")) in ("PHENOTYPE", "P")),
        None,
    )
    return adj, node_ids, pheno


def bfs_shortest(adj, start, target):
    if start == target:
        return 0
    visited = {start}
    queue = deque([(start, 0)])
    while queue:
        node, dist = queue.popleft()
        for nb in adj.get(node, []):
            if nb == target:
                return dist + 1
            if nb not in visited:
                visited.add(nb)
                queue.append((nb, dist + 1))
    return -1


def load_reconciled(net_path):
    d = load_json(net_path / "data" / "reconciled_perturbation_dataset.json")
    if not d:
        return {}
    mapping = {}
    for p in d.get("perturbations", []):
        tid = p.get("test_id", p.get("id", ""))
        exo = p.get("exogenous_supply", p.get("exo")) or {}
        if isinstance(exo, dict) and "node" in exo:
            exo = {exo["node"]: exo.get("value", exo.get("amount", 1.0))}

        net_gene = p.get("network_gene", p.get("ng")) or []
        if isinstance(net_gene, str):
            net_gene = [net_gene]

        gm_dict = p.get("gene_modifiers", p.get("m"))
        gm_singular = p.get("gene_modifier")
        if gm_dict and isinstance(gm_dict, dict) and len(gm_dict) > 0:
            mod_nodes = [n for n, m in gm_dict.items() if m != 1.0]
        elif gm_singular is not None and gm_singular != 1.0 and net_gene:
            mod_nodes = list(net_gene)
        else:
            mod_nodes = []

        n_exo = len(exo) if isinstance(exo, dict) and "node" not in exo else (1 if exo else 0)

        mapping[tid] = {
            "network_gene": net_gene,
            "mod_nodes": mod_nodes,
            "exogenous_supply": exo,
            "has_exogenous": n_exo > 0,
            "has_gene_mod": len(mod_nodes) > 0,
            "n_gene_mods": len(mod_nodes),
            "n_exo": n_exo,
        }
    return mapping


def compute_complexity(n_gene_mods, n_exo):
    total = n_gene_mods + n_exo
    if total <= 0:
        total = 1
    label = "easy" if total == 1 else ("medium" if total == 2 else "hard")
    return total, label


def export_test_level(base, out_dir):
    """CSV 1: all_networks_test_level.csv"""
    rows = []
    for net_str, net_label in NETWORKS:
        net_path = base / net_str
        adj, node_ids, pheno = load_network_graph(net_path)
        recon = load_reconciled(net_path)

        for mfile, mname in METHODS:
            d = load_json(net_path / "validation" / mfile)
            if not d:
                continue
            for r in d.get("detailed_results", []):
                tid = r.get("test_id", "")
                gene = r.get("gene") or ""
                m = recon.get(tid, {})
                net_genes = m.get("network_gene", [])
                mod_nodes = m.get("mod_nodes", [])
                exo = m.get("exogenous_supply", {})
                has_exo = m.get("has_exogenous", False)
                has_gm = m.get("has_gene_mod", False)
                n_gm = m.get("n_gene_mods", 0)
                n_exo = m.get("n_exo", 0)

                mapped = ",".join(net_genes) if net_genes else ""
                treatment = ";".join(f"{k}={v}" for k, v in exo.items()) if has_exo else ""
                score, label = compute_complexity(n_gm, n_exo)

                # Modelled type
                if n_gm == 0 and n_exo == 0:
                    modelled = "control"
                elif n_gm == 0:
                    modelled = "treatment"
                elif n_gm == 1 and n_exo == 0:
                    modelled = "single_mutation"
                elif n_gm == 1:
                    modelled = "mutation_plus_treatment"
                elif n_gm == 2 and n_exo == 0:
                    modelled = "double_mutation"
                elif n_gm == 2:
                    modelled = "double_mutation_plus_treatment"
                elif n_exo == 0:
                    modelled = f"multi_mutation_{n_gm}"
                else:
                    modelled = f"multi_mutation_{n_gm}_plus_treatment"

                # Shortest path
                sp = ""
                if score == 1 and has_gm and not has_exo and len(mod_nodes) == 1:
                    s = mod_nodes[0]
                    if s in node_ids:
                        v = bfs_shortest(adj, s, pheno)
                        sp = v if v >= 0 else "no_path"

                rows.append({
                    "network": net_label, "method": mname, "test_id": tid,
                    "gene": gene, "mapped_node": mapped,
                    "perturbation_type": r.get("perturbation_type", ""),
                    "modelled_as": modelled, "treatment": treatment,
                    "predicted_direction": r.get("predicted_direction", ""),
                    "expected_direction": r.get("expected_direction", ""),
                    "correct": 1 if r.get("correct") else 0,
                    "phenotype_node": r.get("phenotype_node", ""),
                    "shortest_path_length": sp,
                    "complexity_score": score, "complexity_label": label,
                })

    p = out_dir / "all_networks_test_level.csv"
    fields = list(rows[0].keys()) if rows else []
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    print(f"  {p.name}: {len(rows)} rows")
    return rows


def export_accuracy(rows, out_dir):
    """CSV 2: accuracy_summary.csv"""
    acc = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in rows:
        k = (r["network"], r["method"])
        acc[k]["total"] += 1
        acc[k]["correct"] += r["correct"]

    p = out_dir / "accuracy_summary.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["network", "method", "n_correct", "n_total", "accuracy_pct"])
        for (net, meth), g in sorted(acc.items()):
            a = round(g["correct"] / g["total"] * 100, 1) if g["total"] else 0
            w.writerow([net, meth, g["correct"], g["total"], a])
    print(f"  {p.name}: {len(acc)} rows")


def export_edges(base, out_dir):
    """CSV 3: edge_list.csv"""
    rows = []
    for net_str, net_label in NETWORKS:
        d = load_json(base / net_str / "network" / "network.json")
        if not d:
            continue
        node_types = {n["id"]: n.get("type", n.get("ty", "")) for n in d.get("nodes", [])}
        for e in d.get("edges", []):
            ev = e.get("evidence") or []
            dois = [x.get("doi", x.get("source", {}).get("doi", ""))
                    for x in ev if isinstance(x, dict)]
            doi = e.get("doi", e.get("d", ""))
            if doi:
                dois.append(doi)
            source = e.get("source", e.get("s", ""))
            target = e.get("target", e.get("t", ""))
            rows.append({
                "network": net_label,
                "edge_id": e.get("edge_id", e.get("eid", "")),
                "source": source,
                "source_type": node_types.get(source, ""),
                "target": target,
                "target_type": node_types.get(target, ""),
                "sign": e.get("sign", e.get("x", "")),
                "effect": e.get("effect", ""),
                "mechanism": e.get("mechanism", ""),
                "n_evidence": len(ev),
                "doi_list": "; ".join(d for d in dois if d),
            })

    p = out_dir / "edge_list.csv"
    fields = list(rows[0].keys()) if rows else []
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    print(f"  {p.name}: {len(rows)} rows")


def export_network_summary(base, test_rows, out_dir):
    """CSV 5: network_summary.csv"""
    # Aggregate from test_rows
    acc_by_net = defaultdict(lambda: defaultdict(lambda: {"c": 0, "t": 0}))
    for r in test_rows:
        acc_by_net[r["network"]][r["method"]]["t"] += 1
        acc_by_net[r["network"]][r["method"]]["c"] += r["correct"]

    rows = []
    for net_str, net_label in NETWORKS:
        d = load_json(base / net_str / "network" / "network.json")
        meta = d.get("metadata", {}) if d else {}
        species = meta.get("species", "")
        n_nodes = meta.get("total_nodes", 0)
        n_edges = meta.get("total_edges", 0)

        alg = acc_by_net[net_label].get("Algebraic", {"c": 0, "t": 0})
        ode = acc_by_net[net_label].get("ODE", {"c": 0, "t": 0})
        rwr = acc_by_net[net_label].get("RWR", {"c": 0, "t": 0})

        rows.append({
            "network": net_label,
            "species": species,
            "n_nodes": n_nodes,
            "n_edges": n_edges,
            "n_tests": alg["t"],
            "algebraic_acc": round(alg["c"] / alg["t"] * 100, 1) if alg["t"] else 0,
            "ode_acc": round(ode["c"] / ode["t"] * 100, 1) if ode["t"] else 0,
            "rwr_acc": round(rwr["c"] / rwr["t"] * 100, 1) if rwr["t"] else 0,
        })

    p = out_dir / "network_summary.csv"
    fields = list(rows[0].keys()) if rows else []
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    print(f"  {p.name}: {len(rows)} rows")


def export_complexity_accuracy(test_rows, out_dir):
    """CSV 6: complexity_accuracy.csv"""
    groups = defaultdict(lambda: {"c": 0, "t": 0})
    for r in test_rows:
        k = (r["method"], r["complexity_label"], r["complexity_score"])
        groups[k]["t"] += 1
        groups[k]["c"] += r["correct"]

    p = out_dir / "complexity_accuracy.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["method", "complexity_label", "complexity_score",
                     "n_correct", "n_total", "accuracy_pct"])
        for (meth, label, score), g in sorted(groups.items()):
            a = round(g["c"] / g["t"] * 100, 2) if g["t"] else 0
            w.writerow([meth, label, score, g["c"], g["t"], a])
    print(f"  {p.name}: {len(groups)} rows")


def export_pathlength_accuracy(test_rows, out_dir):
    """CSV 7: pathlength_accuracy.csv"""
    groups = defaultdict(lambda: {"c": 0, "t": 0})
    for r in test_rows:
        sp = r["shortest_path_length"]
        if not isinstance(sp, int):
            continue
        pl = min(sp, 7)
        k = (r["method"], pl)
        groups[k]["t"] += 1
        groups[k]["c"] += r["correct"]

    p = out_dir / "pathlength_accuracy.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["method", "path_length", "n_correct", "n_total", "accuracy_pct"])
        for (meth, pl), g in sorted(groups.items()):
            a = round(g["c"] / g["t"] * 100, 2) if g["t"] else 0
            w.writerow([meth, pl, g["c"], g["t"], a])
    print(f"  {p.name}: {len(groups)} rows")


def export_evidence_per_edge(base, out_dir):
    """CSV 8: evidence_per_edge.csv"""
    rows = []
    for net_str, net_label in NETWORKS:
        d = load_json(base / net_str / "data" / "curated_edges.json")
        if not d:
            continue
        for e in d.get("edges", []):
            ev = e.get("evidence", [])
            dois = set()
            for x in ev:
                if isinstance(x, dict):
                    doi = x.get("doi", "")
                    if not doi and "source" in x:
                        doi = x["source"].get("doi", "")
                    if doi:
                        dois.add(doi)
            doi = e.get("doi", e.get("d", ""))
            if doi:
                dois.add(doi)
            rows.append({
                "network": net_label,
                "edge_id": e.get("edge_id", e.get("eid", "")),
                "source": e.get("source", e.get("s", "")),
                "target": e.get("target", e.get("t", "")),
                "sign": e.get("sign", e.get("x", "")),
                "confidence": e.get("confidence", ""),
                "in_model": e.get("in_model", ""),
                "n_papers": len(dois),
                "dois": "; ".join(sorted(dois)),
            })

    p = out_dir / "evidence_per_edge.csv"
    fields = list(rows[0].keys()) if rows else []
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    print(f"  {p.name}: {len(rows)} rows")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="FLASH-P v1.0 CSV Export")
    parser.add_argument("base_dir", nargs="?",
                        default=str(Path(__file__).parent.parent.parent))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    base = Path(args.base_dir)
    out_dir = Path(args.output) if args.output else base / "Fig_Data"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"FLASH-P v1.0 CSV Export")
    print(f"Base: {base}")
    print(f"Output: {out_dir}")
    print()

    test_rows = export_test_level(base, out_dir)
    export_accuracy(test_rows, out_dir)
    export_edges(base, out_dir)
    export_network_summary(base, test_rows, out_dir)
    export_complexity_accuracy(test_rows, out_dir)
    export_pathlength_accuracy(test_rows, out_dir)
    export_evidence_per_edge(base, out_dir)

    print(f"\nAll CSVs exported to {out_dir}")


if __name__ == "__main__":
    main()

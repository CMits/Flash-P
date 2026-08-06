#!/usr/bin/env python3
"""
Run ad hoc FLASH-P simulations for a network.

Examples:
  python Agent/shared/simulate_custom.py networks/Days_To_Flowering --method ode --ko SBPHYB
  python Agent/shared/simulate_custom.py networks/Days_To_Flowering --method all --ko SBPHYB --condition Long_Day=1
  python Agent/shared/simulate_custom.py networks/Days_To_Flowering --find temp

Use --condition for a background condition applied to both baseline and
perturbed runs. Use --baseline-condition and --perturbed-condition when the
two simulations should use different environments. Use --treatment for an
exogenous supply added only to the perturbed run.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from difflib import get_close_matches
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from flashp_validator import FlashPSimulator, SimulationConfig
from ode_validator import ODEConfig, ODESimulator
from rwr_validator import RWRConfig, RWRSimulator, load_network
from validation_common import load_equations


METHODS = ("algebraic", "ode", "rwr")


def resolve_network_dir(raw: str) -> Path:
    path = Path(raw)
    candidates = [path]
    if not path.is_absolute():
        script_dir = Path(__file__).resolve().parent
        claude_root = script_dir.parents[1]
        candidates.extend([
            Path.cwd() / path,
            Path.cwd() / "networks" / path,
            claude_root / path,
            claude_root / "networks" / path,
        ])

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    raise SystemExit(f"Network directory not found: {raw}")


def load_node_names(network_dir: Path) -> List[str]:
    equations_path = network_dir / "network" / "algebraic_equations.json"
    if not equations_path.exists():
        raise SystemExit(f"Missing equations file: {equations_path}")
    network = load_equations(str(equations_path))
    return sorted(network.equations)


def find_nodes(nodes: Sequence[str], terms: Sequence[str]) -> List[str]:
    if not terms:
        return list(nodes)
    lowered_terms = [term.lower() for term in terms]
    return [
        node for node in nodes
        if any(term in node.lower() for term in lowered_terms)
    ]


def resolve_node(name: str, nodes: Sequence[str]) -> str:
    if name in nodes:
        return name

    lower_to_nodes: Dict[str, List[str]] = {}
    for node in nodes:
        lower_to_nodes.setdefault(node.lower(), []).append(node)
    matches = lower_to_nodes.get(name.lower(), [])
    if len(matches) == 1:
        return matches[0]

    cleaned = name.replace(" ", "_")
    if cleaned in nodes:
        return cleaned
    matches = lower_to_nodes.get(cleaned.lower(), [])
    if len(matches) == 1:
        return matches[0]

    substring = [node for node in nodes if name.lower() in node.lower()]
    close = get_close_matches(name, nodes, n=8, cutoff=0.45)
    suggestions = substring[:8] or close
    suffix = ""
    if suggestions:
        suffix = "\nClosest nodes:\n  - " + "\n  - ".join(suggestions)
    raise SystemExit(f"Node not found in network: {name}{suffix}")


def parse_node_value(raw: str, nodes: Sequence[str], default: float) -> Tuple[str, float]:
    if "=" in raw:
        node, value = raw.split("=", 1)
        try:
            parsed_value = float(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid numeric value in {raw!r}") from exc
    else:
        node = raw
        parsed_value = default
    return resolve_node(node.strip(), nodes), parsed_value


def parse_many_node_values(
    values: Optional[Sequence[str]],
    nodes: Sequence[str],
    default: float,
) -> Dict[str, float]:
    parsed: Dict[str, float] = {}
    for raw in values or []:
        node, value = parse_node_value(raw, nodes, default)
        parsed[node] = value
    return parsed


def merge_dicts(*items: Dict[str, float]) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    for item in items:
        merged.update(item)
    return merged


def direction_from_ratio(ratio: float) -> str:
    if ratio > 1.05:
        return "increased"
    if ratio < 0.95:
        return "decreased"
    return "unchanged"


def run_method(
    method: str,
    network_dir: Path,
    baseline_modifiers: Dict[str, float],
    baseline_exogenous: Dict[str, float],
    perturbed_modifiers: Dict[str, float],
    perturbed_exogenous: Dict[str, float],
) -> Dict[str, object]:
    if method in ("algebraic", "ode"):
        equations_path = network_dir / "network" / "algebraic_equations.json"
        network = load_equations(str(equations_path))
        phenotype = network.phenotype_node

        if method == "algebraic":
            simulator = FlashPSimulator(network, SimulationConfig())
        else:
            simulator = ODESimulator(network, ODEConfig())

        baseline, baseline_converged, baseline_iterations = simulator.simulate(
            baseline_modifiers,
            baseline_exogenous,
        )
        perturbed, perturbed_converged, perturbed_iterations = simulator.simulate(
            perturbed_modifiers,
            perturbed_exogenous,
        )
        baseline_value = float(baseline.get(phenotype, 0.0))
        perturbed_value = float(perturbed.get(phenotype, 0.0))
        if baseline_value:
            ratio = perturbed_value / baseline_value
        elif perturbed_value > 0:
            ratio = math.inf
        else:
            ratio = 1.0
        log2_fc = math.log2(max(ratio, 1e-12)) if math.isfinite(ratio) else math.inf
        top_changes = top_changed_nodes(baseline, perturbed, phenotype)
        return {
            "method": method,
            "phenotype": phenotype,
            "baseline_value": baseline_value,
            "perturbed_value": perturbed_value,
            "ratio": ratio,
            "log2_fold_change": log2_fc,
            "predicted_direction": direction_from_ratio(ratio),
            "baseline_converged": baseline_converged,
            "perturbed_converged": perturbed_converged,
            "baseline_iterations": baseline_iterations,
            "perturbed_iterations": perturbed_iterations,
            "top_changed_nodes": top_changes,
        }

    network_path = network_dir / "network" / "network.json"
    if not network_path.exists():
        raise SystemExit(f"Missing network file for RWR: {network_path}")
    network = load_network(str(network_path))
    simulator = RWRSimulator(network, RWRConfig())
    phenotype = network.phenotype_node
    baseline, baseline_converged, baseline_iterations = simulator.simulate(
        baseline_modifiers,
        baseline_exogenous,
    )
    perturbed, perturbed_converged, perturbed_iterations = simulator.simulate(
        perturbed_modifiers,
        perturbed_exogenous,
    )
    baseline_signal = float(baseline.get(phenotype, 0.0))
    perturbed_signal = float(perturbed.get(phenotype, 0.0))
    delta = perturbed_signal - baseline_signal
    if delta > 1e-5:
        direction = "increased"
    elif delta < -1e-5:
        direction = "decreased"
    else:
        direction = "unchanged"
    top_changes = top_changed_nodes(baseline, perturbed, phenotype)
    return {
        "method": method,
        "phenotype": phenotype,
        "baseline_signal": baseline_signal,
        "perturbed_signal": perturbed_signal,
        "delta": delta,
        "predicted_direction": direction,
        "baseline_converged": baseline_converged,
        "perturbed_converged": perturbed_converged,
        "baseline_iterations": baseline_iterations,
        "perturbed_iterations": perturbed_iterations,
        "top_changed_nodes": top_changes,
    }


def top_changed_nodes(
    baseline: Dict[str, float],
    perturbed: Dict[str, float],
    phenotype: str,
    limit: int = 10,
) -> List[Dict[str, object]]:
    rows = []
    for node in sorted(set(baseline) | set(perturbed)):
        before = float(baseline.get(node, 0.0))
        after = float(perturbed.get(node, 0.0))
        rows.append({
            "node": node,
            "baseline": before,
            "perturbed": after,
            "delta": after - before,
            "abs_delta": abs(after - before),
        })
    rows.sort(key=lambda row: (row["node"] != phenotype, -row["abs_delta"], row["node"]))
    return rows[:limit]


def print_human(result: Dict[str, object]) -> None:
    print(f"\n=== {str(result['method']).upper()} ===")
    print(f"Phenotype: {result['phenotype']}")
    if "ratio" in result:
        ratio = float(result["ratio"])
        ratio_text = "inf" if math.isinf(ratio) else f"{ratio:.6g}"
        print(f"Baseline phenotype:  {float(result['baseline_value']):.6g}")
        print(f"Perturbed phenotype: {float(result['perturbed_value']):.6g}")
        print(f"Fold vs baseline:    {ratio_text}")
        print(f"log2 fold change:    {float(result['log2_fold_change']):.6g}")
    else:
        print(f"Baseline signal:     {float(result['baseline_signal']):.6g}")
        print(f"Perturbed signal:    {float(result['perturbed_signal']):.6g}")
        print(f"Delta:               {float(result['delta']):.6g}")
    print(f"Predicted direction: {result['predicted_direction']}")
    print(
        "Converged:           "
        f"baseline={result['baseline_converged']} "
        f"perturbed={result['perturbed_converged']}"
    )
    print(
        "Iterations:          "
        f"baseline={result['baseline_iterations']} "
        f"perturbed={result['perturbed_iterations']}"
    )
    print("\nTop changed nodes:")
    for row in result["top_changed_nodes"]:
        print(
            f"  {row['node']:<24} "
            f"{row['baseline']:.6g} -> {row['perturbed']:.6g} "
            f"(delta {row['delta']:+.6g})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run custom FLASH-P simulations on a network.",
    )
    parser.add_argument("network_dir", help="Network directory, e.g. networks/Days_To_Flowering")
    parser.add_argument(
        "--method",
        choices=("algebraic", "ode", "rwr", "all"),
        default="ode",
        help="Simulation method to run. Default: ode",
    )
    parser.add_argument("--ko", action="append", default=[], help="Knock out node/gene, modifier 0.0")
    parser.add_argument("--kd", action="append", default=[], help="Knock down node/gene, modifier 0.5")
    parser.add_argument("--oe", action="append", default=[], help="Overexpress node/gene, modifier 2.0")
    parser.add_argument(
        "--set",
        dest="set_modifiers",
        action="append",
        default=[],
        metavar="NODE=VALUE",
        help="Set an arbitrary gene modifier in the perturbed run.",
    )
    parser.add_argument(
        "--condition",
        action="append",
        default=[],
        metavar="NODE[=VALUE]",
        help="Exogenous/environment condition applied to both baseline and perturbed runs.",
    )
    parser.add_argument(
        "--baseline-condition",
        action="append",
        default=[],
        metavar="NODE[=VALUE]",
        help="Exogenous/environment condition applied only to the baseline run.",
    )
    parser.add_argument(
        "--perturbed-condition",
        action="append",
        default=[],
        metavar="NODE[=VALUE]",
        help="Exogenous/environment condition applied only to the perturbed run.",
    )
    parser.add_argument(
        "--treatment",
        action="append",
        default=[],
        metavar="NODE[=VALUE]",
        help="Exogenous supply added only to the perturbed run.",
    )
    parser.add_argument(
        "--baseline-ko",
        action="append",
        default=[],
        help="Knockout included in the baseline and perturbed runs.",
    )
    parser.add_argument(
        "--baseline-kd",
        action="append",
        default=[],
        help="Knockdown included in the baseline and perturbed runs.",
    )
    parser.add_argument(
        "--baseline-oe",
        action="append",
        default=[],
        help="Overexpression included in the baseline and perturbed runs.",
    )
    parser.add_argument(
        "--find",
        nargs="+",
        help="List network nodes containing one or more terms, then exit.",
    )
    parser.add_argument("--list-nodes", action="store_true", help="List all node names, then exit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    network_dir = resolve_network_dir(args.network_dir)
    nodes = load_node_names(network_dir)

    if args.list_nodes:
        print("\n".join(nodes))
        return 0
    if args.find:
        matches = find_nodes(nodes, args.find)
        if matches:
            print("\n".join(matches))
        else:
            print("No matching nodes found.")
        return 0

    baseline_modifiers = merge_dicts(
        parse_many_node_values(args.baseline_ko, nodes, 0.0),
        parse_many_node_values(args.baseline_kd, nodes, 0.5),
        parse_many_node_values(args.baseline_oe, nodes, 2.0),
    )
    perturbation_modifiers = merge_dicts(
        baseline_modifiers,
        parse_many_node_values(args.ko, nodes, 0.0),
        parse_many_node_values(args.kd, nodes, 0.5),
        parse_many_node_values(args.oe, nodes, 2.0),
        parse_many_node_values(args.set_modifiers, nodes, 1.0),
    )
    shared_condition = parse_many_node_values(args.condition, nodes, 1.0)
    baseline_exogenous = merge_dicts(
        shared_condition,
        parse_many_node_values(args.baseline_condition, nodes, 1.0),
    )
    perturbed_exogenous = merge_dicts(
        shared_condition,
        parse_many_node_values(args.perturbed_condition, nodes, 1.0),
        parse_many_node_values(args.treatment, nodes, 1.0),
    )

    if perturbation_modifiers == baseline_modifiers and perturbed_exogenous == baseline_exogenous:
        raise SystemExit(
            "No perturbation specified. Add --ko, --kd, --oe, --set, "
            "--treatment, --baseline-condition, or --perturbed-condition."
        )

    methods: Iterable[str] = METHODS if args.method == "all" else (args.method,)
    results = [
        run_method(
            method,
            network_dir,
            baseline_modifiers,
            baseline_exogenous,
            perturbation_modifiers,
            perturbed_exogenous,
        )
        for method in methods
    ]

    envelope = {
        "network_dir": str(network_dir),
        "baseline_gene_modifiers": baseline_modifiers,
        "baseline_exogenous": baseline_exogenous,
        "perturbed_gene_modifiers": perturbation_modifiers,
        "perturbed_exogenous": perturbed_exogenous,
        "results": results,
    }
    if args.json:
        print(json.dumps(envelope, indent=2))
    else:
        print(f"Network: {network_dir}")
        print(f"Baseline modifiers:  {baseline_modifiers or '{}'}")
        print(f"Baseline condition:  {baseline_exogenous or '{}'}")
        print(f"Perturbed modifiers: {perturbation_modifiers or '{}'}")
        print(f"Perturbed condition: {perturbed_exogenous or '{}'}")
        for result in results:
            print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

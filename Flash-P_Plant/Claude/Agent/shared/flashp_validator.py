#!/usr/bin/env python3
"""
================================================================================
FLASH-P DETERMINISTIC NETWORK VALIDATOR
================================================================================

Validates network accuracy by simulating perturbations and comparing to expected
outcomes using the algebraic simulation rules.

This script provides DETERMINISTIC validation - same inputs always produce same
outputs. The agent MUST use this script for validation instead of doing calculations
itself.

USAGE:
    python flashp_validator.py <network_dir>
    python flashp_validator.py <network_dir> --no-damping
    python flashp_validator.py <network_dir> --csv
    python flashp_validator.py <network_dir> --full-state
    python flashp_validator.py --all

INPUT FILES REQUIRED:
    <network_dir>/network/algebraic_equations.json
    <network_dir>/data/reconciled_perturbation_dataset.json

OUTPUT FILES CREATED:
    <network_dir>/validation/script_validation_results.json
    <network_dir>/validation/validation_results.csv          (with --csv)
    <network_dir>/validation/steady_state_dump.json          (with --full-state)

================================================================================
ALGEBRAIC RULES (Configurable via equation_spec.json)
================================================================================

Node_Value = Activation x Inhibition x Gene_Modifier + Exogenous_Supply

DEFAULT RULES (when no equation_spec.json exists):
- Activation = geometric_mean(activators) = product(activators)^(1/n)
- Inhibition = soft_bounded_inverse(inhibitors) = 2 / (1 + product(inhibitors))
- Gene_Modifier: KO=0.0, KD=0.5, WT=1.0, OE=2.0
- Exogenous_Supply: external input (default 0)

DISCOVERED EQUATIONS:
If network/equation_spec.json exists, the script uses agent-discovered equations
instead of the defaults. These may include:
- Custom aggregation functions (weighted_mean, arithmetic_mean, etc.)
- Custom transforms (Hill functions, sigmoids, thresholds, etc.)
- Per-node equation overrides for better accuracy

Default Fixed Parameters (when no equation_spec.json):
- direction_threshold = 0.01  (+-1% determines increased/decreased/unchanged)
- max_iterations = 50   (iteration limit for convergence)
- convergence_tolerance = 0.0001  (steady-state criterion)
- activator_floor = 0.01  (basal transcription when all activators zero)
- damping = 0.7         (stabilizes feedback loops)

================================================================================
"""

import json
import sys
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass
from pathlib import Path

# Import shared infrastructure
from validation_common import (
    Equation, AlgebraicNetwork, Perturbation, TestResult,
    load_equations, load_perturbations,
    classify_direction, safe_log2,
    calculate_metrics, bootstrap_confidence_intervals,
    score_perturbation_complexity, measure_path_length,
    export_results_csv, export_steady_state_dump,
    format_detailed_result, build_validation_report,
)
from flashp_version import get_version

# Import equation executor for discovered equations (optional)
try:
    from equation_executor import execute_equation, load_equation_spec, get_default_spec, validate_equation_spec
    EQUATION_EXECUTOR_AVAILABLE = True
except ImportError:
    EQUATION_EXECUTOR_AVAILABLE = False


# ============================================================================
# CONFIGURATION - Fixed parameters (do not tune)
# ============================================================================
@dataclass
class SimulationConfig:
    """Fixed simulation parameters - validated across 10 networks."""
    direction_threshold: float = 0.01  # +-1% for unchanged
    max_iterations: int = 100      # Maximum simulation iterations
    convergence_tolerance: float = 0.0001  # Steady-state convergence criterion
    activator_floor: float = 0.01  # Basal transcription when no activators
    damping: float = 0.7           # Damping factor for oscillation control


# ============================================================================
# SIMULATION ENGINE
# ============================================================================
class FlashPSimulator:
    """Flash-P algebraic simulation engine with deterministic parameters.

    Supports both:
    1. Default algebraic rules (backward compatible)
    2. Agent-discovered equations from equation_spec.json
    """

    def __init__(self, network: AlgebraicNetwork, config: SimulationConfig, equation_spec: Optional[Dict] = None):
        self.network = network
        self.config = config
        self.node_order = self._topological_sort()

        # Store equation specification (None means use default rules)
        self.equation_spec = equation_spec
        self.use_discovered_equations = equation_spec is not None and EQUATION_EXECUTOR_AVAILABLE

    def _topological_sort(self) -> List[str]:
        """Sort nodes in dependency order for iterative solving."""
        self.source_nodes = set()
        sources = []
        others = []

        for node, eq in self.network.equations.items():
            if not eq.activators and not eq.inhibitors:
                sources.append(node)
                self.source_nodes.add(node)
            else:
                others.append(node)

        return sources + others

    def compute_activation(self, activator_values: List[float]) -> float:
        """Compute geometric mean of activators."""
        if not activator_values:
            return 1.0

        floored_values = [max(v, self.config.activator_floor) for v in activator_values]

        product = 1.0
        for v in floored_values:
            product *= v

        return product ** (1.0 / len(floored_values))

    def compute_inhibition(self, inhibitor_values: List[float]) -> float:
        """Compute the soft-bounded inverse inhibitory term.

        g(x1,...,xn) = 2 / (1 + product(xi))

        PSoup's single-inhibitor rule 2/(1+A) with the product of inhibitors in
        place of the lone inhibitor; identical to PSoup when there is one
        inhibitor. Multiplying rather than summing keeps each inhibitor's
        leverage independent of how many co-inhibitors the node has.

        Self-normalising: all inhibitors at 1.0 gives 2/2 = 1.0, so no floor or
        ceiling parameter is needed. Bounded above at 2.0, and smooth (no clip),
        which keeps the damped iteration stable.
        """
        if not inhibitor_values:
            return 1.0

        product = 1.0
        for v in inhibitor_values:
            product *= v

        return 2.0 / (1.0 + product)

    def compute_node(
        self,
        eq: Equation,
        node_values: Dict[str, float],
        gene_modifiers: Dict[str, float],
        exogenous_supply: Dict[str, float]
    ) -> float:
        """Compute a single node's value.

        All nodes (including ENVIRONMENT) are 1.0 at WT baseline.
        Exogenous supply is ADDITIVE on top of the node value (default 0 = no treatment).
        """
        gene_mod = gene_modifiers.get(eq.node, 1.0)

        # Calculate exogenous supply for this node
        exo_supply = 0.0
        for exo_node in eq.exogenous_positive:
            exo_supply += exogenous_supply.get(exo_node, 0.0)
        for exo_node in eq.exogenous_negative:
            exo_supply -= exogenous_supply.get(exo_node, 0.0)
        if eq.node in exogenous_supply:
            exo_supply += exogenous_supply[eq.node]

        # All inhibitors are active (no special ENVIRONMENT filtering)
        active_inhibitors = list(eq.inhibitors)

        # USE DISCOVERED EQUATIONS IF AVAILABLE
        if self.use_discovered_equations:
            return execute_equation(
                spec=self.equation_spec,
                node=eq.node,
                node_values=node_values,
                activators=eq.activators,
                inhibitors=active_inhibitors,
                gene_modifier=gene_mod,
                exogenous=exo_supply,
                node_type=eq.node_type
            )

        # DEFAULT ALGEBRAIC RULES (fallback)
        activator_values = [node_values.get(a, 1.0) for a in eq.activators]
        activation = self.compute_activation(activator_values)

        inhibitor_values = [node_values.get(i, 1.0) for i in active_inhibitors]
        inhibition = self.compute_inhibition(inhibitor_values)

        # gene_modifier applies to ALL node types (KO=0, KD=0.5, WT=1, OE=2)
        base_value = activation * inhibition * gene_mod

        return max(base_value + exo_supply, 0.0)

    def simulate(
        self,
        gene_modifiers: Dict[str, float],
        exogenous_supply: Dict[str, float],
        debug: bool = False
    ) -> Tuple[Dict[str, float], bool, int]:
        """
        Run iterative simulation until convergence.
        Uses damping to stabilize oscillating feedback loops.

        Returns:
            node_values: Final steady-state values
            converged: Whether simulation converged
            iterations: Number of iterations taken
        """
        # All nodes start at 1.0 (WT baseline). No special cases for ENVIRONMENT.
        # Exogenous supply is additive on top (default 0 = no treatment).
        node_values = {node: 1.0 for node in self.network.equations}

        # Source nodes: set immediately (no feedback)
        for node in self.source_nodes:
            eq = self.network.equations[node]
            node_values[node] = self.compute_node(eq, node_values, gene_modifiers, exogenous_supply)

        converged = False
        iterations = 0

        for i in range(self.config.max_iterations):
            iterations = i + 1
            max_change = 0.0
            new_values = {}

            for node in self.node_order:
                eq = self.network.equations[node]
                computed_val = self.compute_node(eq, node_values, gene_modifiers, exogenous_supply)

                if node in self.source_nodes:
                    new_values[node] = computed_val
                    change = abs(computed_val - node_values[node])
                else:
                    old_val = node_values[node]
                    damping = self.config.damping
                    new_val = (1 - damping) * computed_val + damping * old_val
                    new_values[node] = new_val
                    change = abs(new_val - old_val)

                max_change = max(max_change, change)

            node_values = new_values

            if debug and i < 5:
                phenotype = self.network.phenotype_node
                print(f"  Iter {i+1}: phenotype={node_values.get(phenotype, 0):.6f}, max_change={max_change:.8f}")

            if max_change < self.config.convergence_tolerance:
                converged = True
                break

        return node_values, converged, iterations

    def get_wt_baseline(self) -> Dict[str, float]:
        """Simulate wild-type (all gene modifiers = 1.0, no exogenous supply)."""
        values, _, _ = self.simulate({}, {})
        return values


# ============================================================================
# VALIDATION ENGINE
# ============================================================================
class Validator:
    """Validates network against perturbation tests."""

    def __init__(self, simulator: FlashPSimulator, config: SimulationConfig, full_state: bool = False):
        self.simulator = simulator
        self.config = config
        self.wt_values = simulator.get_wt_baseline()
        self.full_state = full_state

    def run_perturbation(self, pert: Perturbation) -> TestResult:
        """Run a single perturbation test and return a fully transparent TestResult."""
        phenotype = pert.phenotype_node or self.simulator.network.phenotype_node

        # Determine baseline
        if pert.comparison_baseline in ('WT', 'wt'):
            baseline_values = self.wt_values
        elif pert.comparison_baseline == 'epistasis' and pert.baseline_modifiers:
            baseline_values, _, _ = self.simulator.simulate(
                pert.baseline_modifiers, pert.baseline_exogenous)
        else:
            # Rescue: mutant alone as baseline
            baseline_values, _, _ = self.simulator.simulate(pert.gene_modifiers, {})

        baseline_phenotype = baseline_values.get(phenotype, 1.0)

        # Run perturbed simulation
        perturbed_values, converged, iterations = self.simulator.simulate(
            pert.gene_modifiers, pert.exogenous_supply)
        perturbed_phenotype = perturbed_values.get(phenotype, 1.0)

        # Compute ratio and direction
        if baseline_phenotype == 0:
            ratio = float('inf') if perturbed_phenotype > 0 else 1.0
        else:
            ratio = perturbed_phenotype / baseline_phenotype

        predicted = classify_direction(ratio, self.config.direction_threshold)
        log2fc = safe_log2(ratio) if ratio != float('inf') else float('inf')

        # Primary gene modifier for reporting
        gene_mod = 1.0
        if pert.gene_modifiers:
            gene_mod = list(pert.gene_modifiers.values())[0]

        # Extract exogenous supply info for reporting
        exo_node = ""
        exo_value = 0.0
        if pert.exogenous_supply:
            exo_node = ", ".join(pert.exogenous_supply.keys())
            exo_value = list(pert.exogenous_supply.values())[0]

        return TestResult(
            test_id=pert.test_id,
            gene=pert.gene,
            perturbation_type=pert.perturbation_type,
            gene_modifier=gene_mod,
            exogenous_node=exo_node,
            exogenous_value=exo_value,
            wt_value=self.wt_values.get(phenotype, 1.0),
            perturbed_value=perturbed_phenotype,
            comparison_baseline=pert.comparison_baseline,
            comparison_baseline_value=baseline_phenotype,
            ratio=ratio,
            log2_fold_change=log2fc,
            direction_threshold=self.config.direction_threshold,
            predicted_direction=predicted,
            expected_direction=pert.expected_direction,
            correct=(predicted == pert.expected_direction),
            phenotype_node=phenotype,
            converged=converged,
            iterations=iterations,
            evidence_sentence=pert.evidence_sentence,
            evidence_doi=pert.evidence_doi,
            steady_state_wt=dict(self.wt_values) if self.full_state else None,
            steady_state_perturbed=dict(perturbed_values) if self.full_state else None,
        )

    def validate_all(
        self,
        perturbations: List[Perturbation],
        edges: Optional[List[Tuple[str, str]]] = None
    ) -> Tuple[List[TestResult], List[Perturbation]]:
        """Run all perturbation tests with complexity/path scoring."""
        results = []
        skipped = []

        network_nodes = set(self.simulator.network.equations.keys())
        phenotype_node = self.simulator.network.phenotype_node

        for pert in perturbations:
            if not pert.in_network:
                skipped.append(pert)
                continue

            if not pert.gene_modifiers and not pert.exogenous_supply:
                skipped.append(pert)
                continue

            # Check if perturbed genes exist in the network
            genes_in_network = False
            for gene in pert.gene_modifiers:
                if gene in network_nodes:
                    genes_in_network = True
                    break
            for node in pert.exogenous_supply:
                if node in network_nodes:
                    genes_in_network = True
                    break

            if not genes_in_network:
                skipped.append(pert)
                continue

            result = self.run_perturbation(pert)

            # Add complexity scoring
            score, label = score_perturbation_complexity(pert)
            result.complexity_score = score
            result.complexity_label = label

            # Add path length measurement (use this perturbation's own phenotype)
            if edges:
                pert_phenotype = pert.phenotype_node or phenotype_node
                perturbed_nodes = [g for g in pert.gene_modifiers if g in network_nodes]
                if not perturbed_nodes:
                    perturbed_nodes = [n for n in pert.exogenous_supply if n in network_nodes]
                if perturbed_nodes and pert_phenotype:
                    path_len, path = measure_path_length(edges, perturbed_nodes, pert_phenotype)
                    result.path_length = path_len
                    result.path = path

            results.append(result)

        return results, skipped


# ============================================================================
# HELPER: Extract edges from equations
# ============================================================================
def extract_edges_from_equations(network: AlgebraicNetwork) -> List[Tuple[str, str]]:
    """Build edge list from equation activators/inhibitors for path measurement."""
    edges = []
    for node, eq in network.equations.items():
        for a in eq.activators:
            edges.append((a, node))
        for i in eq.inhibitors:
            edges.append((i, node))
    return edges


# ============================================================================
# MAIN VALIDATION FUNCTION
# ============================================================================
def validate_network(
    network_dir: str,
    no_damping: bool = False,
    csv_export: bool = False,
    full_state: bool = False
) -> Dict[str, Any]:
    """Validate a single network directory."""
    base_path = Path(network_dir)

    # Find equations file
    equations_path = base_path / 'network' / 'algebraic_equations.json'
    if not equations_path.exists():
        return {"error": f"Equations file not found: {equations_path}"}

    # Find perturbations file (prefer reconciled)
    perturbations_path = base_path / 'data' / 'reconciled_perturbation_dataset.json'
    if not perturbations_path.exists():
        perturbations_path = base_path / 'data' / 'perturbation_dataset.json'
        if not perturbations_path.exists():
            return {"error": f"Perturbations file not found in {base_path / 'data'}"}

    # Check for discovered equations
    equation_spec_path = base_path / 'network' / 'equation_spec.json'
    equation_spec = None
    using_discovered_equations = False

    if equation_spec_path.exists() and EQUATION_EXECUTOR_AVAILABLE:
        try:
            equation_spec = load_equation_spec(str(equation_spec_path))
            errors = validate_equation_spec(equation_spec)
            if errors:
                print(f"WARNING: equation_spec.json has validation errors:")
                for e in errors:
                    print(f"  - {e}")
                print("Falling back to default algebraic rules.")
                equation_spec = None
            else:
                using_discovered_equations = True
        except Exception as e:
            print(f"WARNING: Could not load equation_spec.json: {e}")
            print("Falling back to default algebraic rules.")

    print(f"\n{'='*70}")
    print(f"VALIDATING: {base_path.name}")
    print(f"{'='*70}")

    # Load network using shared loader
    print(f"Loading network from: {equations_path}")
    network = load_equations(str(equations_path))
    print(f"  Loaded {len(network.equations)} equations")
    print(f"  Phenotype node: {network.phenotype_node}")

    # Load perturbations using shared loader
    print(f"Loading perturbations from: {perturbations_path}")
    perturbations = load_perturbations(str(perturbations_path))
    print(f"  Loaded {len(perturbations)} perturbation tests")

    # Create config
    config = SimulationConfig()
    if no_damping:
        config.damping = 0.0
        print("\n*** NO DAMPING MODE (damping=0.0) ***")

    # Override config from equation_spec if available
    if using_discovered_equations and equation_spec:
        print(f"\n*** USING DISCOVERED EQUATIONS from {equation_spec_path.name} ***")
        prop_params = equation_spec.get("propagation_params", {})
        if "damping" in prop_params and not no_damping:
            config.damping = prop_params["damping"]
        if "max_iterations" in prop_params:
            config.max_iterations = prop_params["max_iterations"]
        if "convergence_tolerance" in prop_params:
            config.convergence_tolerance = prop_params["convergence_tolerance"]
        if "activator_floor" in prop_params:
            config.activator_floor = prop_params["activator_floor"]

        disc_meta = equation_spec.get("discovery_metadata", {})
        if disc_meta.get("discovered_by"):
            print(f"  Discovered by: {disc_meta.get('discovered_by')}")
        if disc_meta.get("accuracy_achieved"):
            print(f"  Discovery accuracy: {disc_meta.get('accuracy_achieved')}%")

        node_overrides = equation_spec.get("node_equations", {})
        if node_overrides:
            print(f"  Custom equations for {len(node_overrides)} nodes: {list(node_overrides.keys())[:5]}{'...' if len(node_overrides) > 5 else ''}")
    else:
        print(f"\n*** USING DEFAULT ALGEBRAIC RULES ***")

    print(f"\nSimulation parameters:")
    print(f"  direction_threshold: {config.direction_threshold}")
    print(f"  max_iterations: {config.max_iterations}")
    print(f"  convergence_tolerance: {config.convergence_tolerance}")
    print(f"  activator_floor: {config.activator_floor}")
    print(f"  damping: {config.damping}")

    # Create simulator and validator
    simulator = FlashPSimulator(network, config, equation_spec)
    validator = Validator(simulator, config, full_state=full_state)

    # Extract edges for path length measurement
    edges = extract_edges_from_equations(network)

    # Run validation
    print(f"\nRunning validation...")
    results, skipped = validator.validate_all(perturbations, edges=edges)

    print(f"  Tested: {len(results)}")
    print(f"  Skipped (not in network): {len(skipped)}")

    if len(results) == 0:
        return {
            "error": "No perturbation tests could be executed",
            "network": base_path.name,
            "skipped": len(skipped)
        }

    # Calculate metrics using shared function (includes FRS when counts provided)
    n_nodes = len(network.equations)
    n_edges = len(edges)
    metrics = calculate_metrics(results, n_nodes=n_nodes, n_edges=n_edges)

    # Bootstrap confidence intervals
    ci = bootstrap_confidence_intervals(results, n_bootstrap=1000)

    # Print results
    print(f"\n{'='*50}")
    print("RESULTS SUMMARY")
    print(f"{'='*50}")
    print(f"Overall Accuracy: {metrics['overall_accuracy']}% ({metrics['correct']}/{metrics['total']})")
    print(f"Cohen's Kappa: {metrics['cohens_kappa']}")
    print(f"MCC: {metrics['mcc']}")
    print(f"Convergence Rate: {metrics['convergence_rate']}%")
    if ci.get('accuracy_ci'):
        lo, hi = ci['accuracy_ci']
        print(f"95% CI (Accuracy): [{lo}%, {hi}%]")

    print(f"\nPer-class metrics:")
    for label, m in metrics['per_class'].items():
        print(f"  {label}: P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} (n={m['support']})")

    print(f"\nBy perturbation type:")
    for ptype, data in sorted(metrics['by_perturbation_type'].items()):
        print(f"  {ptype}: {data['accuracy']}% ({data['correct']}/{data['total']})")

    # Show failures
    failures = [r for r in results if not r.correct]
    if failures:
        print(f"\n{'='*50}")
        print(f"FAILURES ({len(failures)} tests)")
        print(f"{'='*50}")
        for f in failures[:20]:
            print(f"  {f.test_id}: expected={f.expected_direction}, predicted={f.predicted_direction}, ratio={f.ratio:.4f}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more failures")

    # Build v1.0 schema-compliant report
    report = build_validation_report(
        network_name=base_path.name,
        phenotype=network.metadata.get('phenotype', 'unknown'),
        species=network.metadata.get('species', 'unknown'),
        method='Algebraic',
        parameters={
            'inhibition_form': 'soft_bounded_inverse: 2/(1+product(inhibitors))',
            'direction_threshold': config.direction_threshold,
            'max_iterations': config.max_iterations,
            'convergence_tolerance': config.convergence_tolerance,
            'activator_floor': config.activator_floor,
            'damping': config.damping,
        },
        results=results,
        total_perturbations=len(perturbations),
        skipped=len(skipped),
        total_equations=len(network.equations),
        n_nodes=n_nodes,
        n_edges=n_edges,
        equation_mode='discovered' if using_discovered_equations else 'default',
        equation_spec={
            'used': using_discovered_equations,
            'path': str(equation_spec_path) if using_discovered_equations else None,
            'discovery_metadata': equation_spec.get('discovery_metadata') if equation_spec else None,
            'node_overrides': list(equation_spec.get('node_equations', {}).keys()) if equation_spec else [],
        },
    )

    # Export CSV if requested
    validation_dir = base_path / 'validation'
    validation_dir.mkdir(parents=True, exist_ok=True)

    if csv_export:
        csv_path = str(validation_dir / 'validation_results.csv')
        export_results_csv(results, csv_path)
        print(f"\nCSV exported to: {csv_path}")

    # Export steady-state dump if requested
    if full_state:
        dump_path = str(validation_dir / 'steady_state_dump.json')
        export_steady_state_dump(results, dump_path)
        print(f"Steady-state dump saved to: {dump_path}")

    return report


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage: python flashp_validator.py <network_dir> [--no-damping] [--csv] [--full-state]")
        print("       python flashp_validator.py --all [--no-damping] [--csv] [--full-state]")
        sys.exit(1)

    script_dir = Path(__file__).parent

    # Parse flags
    no_damping = '--no-damping' in sys.argv
    csv_export = '--csv' in sys.argv
    full_state = '--full-state' in sys.argv

    # Remove flags from argv
    args = [a for a in sys.argv[1:] if a not in ('--no-damping', '--csv', '--full-state')]

    if not args:
        print("Error: No network directory specified.")
        sys.exit(1)

    if args[0] == '--all':
        network_dirs = [
            d for d in script_dir.iterdir()
            if d.is_dir() and (d / 'network' / 'algebraic_equations.json').exists()
        ]
    else:
        network_dirs = [Path(d) for d in args]

    all_reports = []

    for network_dir in network_dirs:
        if not network_dir.is_absolute():
            network_dir = script_dir / network_dir

        if not network_dir.exists():
            print(f"Warning: Directory not found: {network_dir}")
            continue

        report = validate_network(
            str(network_dir),
            no_damping=no_damping,
            csv_export=csv_export,
            full_state=full_state
        )
        all_reports.append(report)

        # Save individual report
        output_path = network_dir / 'validation' / 'script_validation_results.json'
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved to: {output_path}")

    # Print comparison summary
    if len(all_reports) > 1:
        print(f"\n{'='*70}")
        print("CROSS-NETWORK COMPARISON")
        print(f"{'='*70}")
        print(f"{'Network':<45} {'Accuracy':>10} {'Kappa':>10} {'MCC':>10}")
        print("-" * 75)
        for r in all_reports:
            if 'error' not in r:
                print(f"{r['network']:<45} {r['metrics']['overall_accuracy']:>9.1f}% {r['metrics']['cohens_kappa']:>10.4f} {r['metrics']['mcc']:>10.4f}")

    return all_reports


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
================================================================================
ODE VALIDATOR - Normalized Hill Function Simulation
================================================================================

Validates network accuracy by simulating perturbations using ODE dynamics with
normalized Hill functions. This is a PLUGIN validator that can be used alongside
the main flashp_validator.py (algebraic rules).

USAGE:
    python ode_validator.py <network_dir>
    python ode_validator.py <network_dir> --K 0.5 --n 3
    python ode_validator.py <network_dir> --csv
    python ode_validator.py <network_dir> --full-state
    python ode_validator.py --all
    python ode_validator.py --all --sensitivity --csv --full-state

EXAMPLES:
    python ode_validator.py flowering_time_arabidopsis_network
    python ode_validator.py shoot_branching_rice_network --K 2.0 --n 2
    python ode_validator.py --all

INPUT FILES REQUIRED:
    <network_dir>/network/algebraic_equations.json
    <network_dir>/data/reconciled_perturbation_dataset.json

OUTPUT FILES CREATED:
    <network_dir>/validation/ode_validation_results.json
    <network_dir>/validation/ode_sensitivity_results.json  (with --sensitivity)
    <network_dir>/validation/ode_validation_results.csv    (with --csv)
    <network_dir>/validation/ode_steady_state_dump.json    (with --full-state)
    <network_dir>/network/ode_equations.json               (Hill formula docs)

================================================================================
HILL FUNCTION FORMULAS (Normalized so f(1) = 1)
================================================================================

Activation (normalized Hill):
    f(x) = x^n * (K^n + 1) / (K^n + x^n)
    Satisfies: f(1) = 1.0

Inhibition (normalized Hill):
    g(x) = (K^n + 1) / (K^n + x^n)
    Satisfies: g(1) = 1.0

ODE dynamics:
    dx/dt = production - decay
    production = product(activation) * product(inhibition) * modifier + exogenous
    decay = x (linear decay with rate 1)

At steady state: x = production (when decay rate = 1)

Default Parameters:
- K = 1.0               (Hill constant - higher = more switch-like)
- n = 2                 (Hill coefficient - cooperativity)
- dt = 0.1              (Euler integration step)
- max_time = 50         (Maximum simulation time)
- convergence_tol = 0.0001  (Steady-state criterion)
- direction_threshold = 0.01  (+-1% for increased/decreased/unchanged)

================================================================================
COMPARISON RULES (Same as algebraic method)
================================================================================

| Perturbation Type      | Compare To      | Example              |
|------------------------|-----------------|----------------------|
| Mutant + Exogenous     | Mutant alone    | max1+SL vs max1      |
| All other perturbations| Wild-type (WT)  | max1 vs WT           |

================================================================================
"""

import json
import sys
from typing import Dict, List, Tuple, Any
from dataclasses import dataclass
from pathlib import Path

from validation_common import (
    Equation,
    AlgebraicNetwork,
    Perturbation,
    TestResult,
    load_equations,
    load_perturbations,
    find_equations_path,
    find_perturbations_path,
    classify_direction,
    safe_log2,
    calculate_metrics,
    bootstrap_confidence_intervals,
    score_perturbation_complexity,
    measure_path_length,
    export_results_csv,
    export_steady_state_dump,
    format_detailed_result,
    build_validation_report,
)
from flashp_version import get_version


# ============================================================================
# CONFIGURATION - ODE Parameters
# ============================================================================
@dataclass
class ODEConfig:
    """ODE simulation parameters."""
    K: float = 1.0                 # Hill constant
    n: int = 2                     # Hill coefficient (cooperativity)
    dt: float = 0.1                # Euler integration step
    max_time: float = 50.0         # Maximum simulation time
    convergence_tolerance: float = 0.0001  # Steady-state criterion
    direction_threshold: float = 0.01  # +-1% for direction classification
    activator_floor: float = 0.01  # Basal expression when activators zero


# ============================================================================
# HILL FUNCTIONS (Normalized so f(1) = 1)
# ============================================================================
def hill_activation(x: float, K: float = 1.0, n: int = 2) -> float:
    """
    Normalized Hill activation function.
    f(x) = x^n * (K^n + 1) / (K^n + x^n)

    Properties:
    - f(0) = 0
    - f(1) = 1 (normalized)
    - f(inf) = (K^n + 1) -> increases for x >> K
    - Sigmoidal shape controlled by n (cooperativity)
    """
    if x <= 0:
        return 0.0
    K_n = K ** n
    x_n = x ** n
    return x_n * (K_n + 1) / (K_n + x_n)


def hill_inhibition(x: float, K: float = 1.0, n: int = 2) -> float:
    """
    Normalized Hill inhibition function.
    g(x) = (K^n + 1) / (K^n + x^n)

    Properties:
    - g(0) = (K^n + 1) / K^n = 1 + 1/K^n -> de-repression
    - g(1) = 1 (normalized)
    - g(inf) -> 0 (full inhibition)
    - Sigmoidal shape controlled by n
    """
    if x <= 0:
        # When inhibitor is 0, use de-repression
        K_n = K ** n
        return (K_n + 1) / K_n
    K_n = K ** n
    x_n = x ** n
    return (K_n + 1) / (K_n + x_n)


# ============================================================================
# ODE SIMULATION ENGINE
# ============================================================================
class ODESimulator:
    """ODE simulation engine using normalized Hill functions."""

    def __init__(self, network: AlgebraicNetwork, config: ODEConfig):
        self.network = network
        self.config = config
        # Identify source nodes (no activators, no inhibitors) -- their value
        # is fully determined by gene_modifier/exogenous with no dynamics
        self.source_nodes = set()
        for node, eq in network.equations.items():
            if not eq.activators and not eq.inhibitors:
                self.source_nodes.add(node)

    def compute_production(
        self,
        eq: Equation,
        node_values: Dict[str, float],
        gene_modifiers: Dict[str, float],
        exogenous_supply: Dict[str, float]
    ) -> float:
        """
        Compute production rate for a node.
        production = activation * inhibition * modifier + exogenous
        """
        # All nodes (including ENVIRONMENT) are 1.0 at WT baseline.
        # Exogenous supply is additive on top (default 0 = no treatment).

        # Gene modifier (default 1.0 for WT)
        gene_mod = gene_modifiers.get(eq.node, 1.0)

        # Compute activation (product of Hill activations)
        activation = 1.0
        for a in eq.activators:
            val = node_values.get(a, 1.0)
            # Apply activator floor for basal expression
            val = max(val, self.config.activator_floor)
            activation *= hill_activation(val, self.config.K, self.config.n)

        # If no activators, default activation is 1.0
        if not eq.activators:
            activation = 1.0

        # Compute inhibition (product of Hill inhibitions)
        inhibition = 1.0
        for inh in eq.inhibitors:
            val = node_values.get(inh, 1.0)
            # All inhibitors are active (no special ENVIRONMENT filtering)
            inhibition *= hill_inhibition(val, self.config.K, self.config.n)

        # gene_modifier applies to ALL node types (KO=0, KD=0.5, WT=1, OE=2)
        base_prod = activation * inhibition * gene_mod

        # Add exogenous supply
        exo_supply = 0.0
        for exo_node in eq.exogenous_positive:
            exo_supply += exogenous_supply.get(exo_node, 0.0)
        for exo_node in eq.exogenous_negative:
            exo_supply -= exogenous_supply.get(exo_node, 0.0)
        if eq.node in exogenous_supply:
            exo_supply += exogenous_supply[eq.node]

        return max(base_prod + exo_supply, 0.0)

    def simulate(
        self,
        gene_modifiers: Dict[str, float],
        exogenous_supply: Dict[str, float],
        debug: bool = False
    ) -> Tuple[Dict[str, float], bool, int]:
        """
        Run ODE simulation using Euler integration.

        dx/dt = production - decay
        where decay = x (linear decay with rate 1)

        At steady state: x = production

        Returns:
            node_values: Final steady-state values
            converged: Whether simulation converged
            time_steps: Number of time steps taken
        """
        # Initialize all nodes to 1.0 (WT baseline)
        values = {node: 1.0 for node in self.network.equations}

        # All nodes start at 1.0 (WT baseline). No special ENVIRONMENT handling.

        # Source nodes (no activators, no inhibitors): set to steady state
        # immediately. At ODE steady state x = production, and for source
        # nodes production is constant (gene_mod + exo), so no dynamics.
        for node in self.source_nodes:
            eq = self.network.equations[node]
            values[node] = self.compute_production(eq, values, gene_modifiers, exogenous_supply)

        converged = False
        num_steps = int(self.config.max_time / self.config.dt)

        for t_step in range(num_steps):
            max_change = 0.0
            new_values = {}

            for node, eq in self.network.equations.items():
                if node in self.source_nodes:
                    # Source nodes: no dynamics, value is constant
                    production = self.compute_production(eq, values, gene_modifiers, exogenous_supply)
                    new_values[node] = production
                    change = abs(production - values[node])
                else:
                    x = values[node]

                    # Compute production rate
                    production = self.compute_production(eq, values, gene_modifiers, exogenous_supply)

                    # Decay rate (linear decay with rate 1)
                    decay = x

                    # Euler integration: dx/dt = production - decay
                    dx = (production - decay) * self.config.dt
                    new_x = max(0.0, x + dx)  # Ensure non-negative
                    new_values[node] = new_x
                    change = abs(new_x - x)

                max_change = max(max_change, change)

            values = new_values

            if debug and t_step < 10:
                phenotype = self.network.phenotype_node
                print(f"  t={t_step*self.config.dt:.1f}: phenotype={values.get(phenotype, 0):.6f}, max_change={max_change:.8f}")

            if max_change < self.config.convergence_tolerance:
                converged = True
                return values, converged, t_step + 1

        return values, converged, num_steps

    def get_wt_baseline(self) -> Dict[str, float]:
        """Simulate wild-type (all gene modifiers = 1.0, no exogenous supply)."""
        values, _, _ = self.simulate({}, {})
        return values


# ============================================================================
# HELPER: Extract edges from equations for path length measurement
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
# HELPER: Write ode_equations.json
# ============================================================================
def write_ode_equations(network: AlgebraicNetwork, config: ODEConfig,
                        accuracy: float, output_path: str) -> None:
    """
    Write ode_equations.json documenting the Hill formulas used for each node.

    Output format:
    {
      "metadata": { "method": "ODE_Hill", "K": ..., "n": ..., "accuracy": ... },
      "equations": [ { "node": ..., "activators": [...], "inhibitors": [...], "formula": ... }, ... ]
    }
    """
    equations_list = []
    for node, eq in network.equations.items():
        act_part = f"prod(f({', '.join(eq.activators)}))" if eq.activators else "1.0"
        inh_part = f"prod(g({', '.join(eq.inhibitors)}))" if eq.inhibitors else "1.0"
        formula = f"{node} = {act_part} * {inh_part} * gene_modifier + exogenous"

        equations_list.append({
            "node": node,
            "activators": eq.activators,
            "inhibitors": eq.inhibitors,
            "formula": formula
        })

    doc = {
        "metadata": {
            "method": "ODE_Hill",
            "K": config.K,
            "n": config.n,
            "accuracy": round(accuracy, 1),
            "hill_activation_formula": f"f(x) = x^{config.n} * ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
            "hill_inhibition_formula": f"g(x) = ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
            "ode_dynamics": "dx/dt = production - x; at steady state x = production",
            "dt": config.dt,
            "max_time": config.max_time,
            "convergence_tolerance": config.convergence_tolerance,
            "direction_threshold": config.direction_threshold
        },
        "equations": equations_list
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2)


# ============================================================================
# VALIDATION ENGINE
# ============================================================================
class Validator:
    """Validates network against perturbation tests using ODE simulation."""

    def __init__(self, simulator: ODESimulator, config: ODEConfig,
                 full_state: bool = False):
        self.simulator = simulator
        self.config = config
        self.full_state = full_state
        self.wt_values = simulator.get_wt_baseline()

    def run_perturbation(self, pert: Perturbation) -> TestResult:
        """Run a single perturbation test and return the result."""
        phenotype = pert.phenotype_node or self.simulator.network.phenotype_node

        # WT baseline value
        wt_phenotype = self.wt_values.get(phenotype, 1.0)

        # Determine baseline based on comparison type
        if pert.comparison_baseline == 'WT' or pert.comparison_baseline == 'wt':
            baseline_values = self.wt_values
        elif pert.comparison_baseline == 'epistasis' and pert.baseline_modifiers:
            # Epistasis: baseline is the simpler perturbation (e.g., single mutant)
            baseline_values, _, _ = self.simulator.simulate(
                pert.baseline_modifiers, pert.baseline_exogenous)
        else:
            # For rescue tests, compute mutant baseline (without exogenous)
            baseline_values, _, _ = self.simulator.simulate(pert.gene_modifiers, {})

        baseline_phenotype = baseline_values.get(phenotype, 1.0)

        # Run perturbed simulation
        perturbed_values, converged, time_steps = self.simulator.simulate(
            pert.gene_modifiers,
            pert.exogenous_supply
        )
        perturbed_phenotype = perturbed_values.get(phenotype, 1.0)

        # Calculate ratio and classify direction using shared function
        if baseline_phenotype != 0:
            ratio = perturbed_phenotype / baseline_phenotype
        else:
            ratio = float('inf') if perturbed_phenotype > 0 else 1.0

        predicted = classify_direction(ratio, self.config.direction_threshold)
        log2fc = safe_log2(ratio) if ratio != float('inf') else float('inf')

        # Determine the primary gene modifier value
        gene_modifier_val = 1.0
        if pert.gene_modifiers:
            gene_modifier_val = list(pert.gene_modifiers.values())[0]

        # Build steady-state dumps if requested
        ss_wt = None
        ss_perturbed = None
        if self.full_state:
            ss_wt = dict(self.wt_values)
            ss_perturbed = dict(perturbed_values)

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
            gene_modifier=gene_modifier_val,
            exogenous_node=exo_node,
            exogenous_value=exo_value,
            wt_value=wt_phenotype,
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
            iterations=time_steps,
            evidence_sentence=pert.evidence_sentence,
            evidence_doi=pert.evidence_doi,
            steady_state_wt=ss_wt,
            steady_state_perturbed=ss_perturbed,
        )

    def validate_all(
        self,
        perturbations: List[Perturbation],
        edges: List[Tuple[str, str]] = None
    ) -> Tuple[List[TestResult], List[Perturbation]]:
        """Run all perturbation tests, returning results and skipped tests."""
        results = []
        skipped = []

        for pert in perturbations:
            if not pert.in_network:
                skipped.append(pert)
                continue

            if not pert.gene_modifiers and not pert.exogenous_supply:
                skipped.append(pert)
                continue

            # Check if perturbed genes/nodes actually exist in the network
            # This ensures fair comparison across all validation methods
            network_nodes = set(self.simulator.network.equations.keys())
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
            complexity_score, complexity_label = score_perturbation_complexity(pert)
            result.complexity_score = complexity_score
            result.complexity_label = complexity_label

            # Add path length measurement
            if edges is not None:
                perturbed_nodes = [g for g in pert.gene_modifiers if g in network_nodes]
                if not perturbed_nodes:
                    perturbed_nodes = [n for n in pert.exogenous_supply if n in network_nodes]
                phenotype = pert.phenotype_node or self.simulator.network.phenotype_node
                path_len, path = measure_path_length(edges, perturbed_nodes, phenotype)
                result.path_length = path_len
                result.path = path

            results.append(result)

        return results, skipped


# ============================================================================
# MAIN VALIDATION FUNCTION
# ============================================================================
def validate_network(network_dir: str, K: float = 1.0, n: int = 2,
                     csv_export: bool = False, full_state: bool = False) -> Dict[str, Any]:
    """
    Validate a single network directory using ODE simulation.

    Args:
        network_dir: Path to network directory
        K: Hill constant (default 1.0)
        n: Hill coefficient (default 2)
        csv_export: If True, export results as CSV
        full_state: If True, capture full steady-state node values

    Returns:
        Validation report dictionary
    """
    base_path = Path(network_dir)

    # Find equations file
    equations_path = find_equations_path(network_dir)
    if not equations_path:
        return {"error": f"Equations file not found in: {base_path / 'network'}"}

    # Find perturbations file (prefer reconciled)
    perturbations_path = find_perturbations_path(network_dir)
    if not perturbations_path:
        return {"error": f"Perturbations file not found in {base_path / 'data'}"}

    print(f"\n{'='*70}")
    print(f"ODE VALIDATION: {base_path.name}")
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

    # Extract edges for path length measurement
    edges = extract_edges_from_equations(network)

    # Create simulator and validator
    config = ODEConfig(K=K, n=n)

    print(f"\nODE parameters:")
    print(f"  K (Hill constant): {config.K}")
    print(f"  n (Hill coefficient): {config.n}")
    print(f"  dt: {config.dt}")
    print(f"  max_time: {config.max_time}")
    print(f"  convergence_tolerance: {config.convergence_tolerance}")
    print(f"  direction_threshold: {config.direction_threshold}")

    simulator = ODESimulator(network, config)
    validator = Validator(simulator, config, full_state=full_state)

    # Run validation
    print(f"\nRunning ODE validation...")
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
    ci = bootstrap_confidence_intervals(results)

    # Print results
    print(f"\n{'='*50}")
    print("ODE RESULTS SUMMARY")
    print(f"{'='*50}")
    print(f"Overall Accuracy: {metrics['overall_accuracy']}% ({metrics['correct']}/{metrics['total']})")
    print(f"Cohen's Kappa: {metrics['cohens_kappa']}")
    print(f"MCC: {metrics['mcc']}")
    print(f"Convergence Rate: {metrics['convergence_rate']}%")

    if ci:
        print(f"95% CI (Accuracy): {ci['accuracy_ci']}")

    print(f"\nPer-class metrics:")
    for label, m in metrics['per_class'].items():
        print(f"  {label}: P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} (n={m['support']})")

    # Show failures
    failures = [r for r in results if not r.correct]
    if failures:
        print(f"\n{'='*50}")
        print(f"FAILURES ({len(failures)} tests)")
        print(f"{'='*50}")
        for f in failures[:15]:
            print(f"  {f.test_id}: expected={f.expected_direction}, predicted={f.predicted_direction}, ratio={f.ratio:.4f}")
        if len(failures) > 15:
            print(f"  ... and {len(failures) - 15} more failures")

    # Export CSV if requested
    if csv_export:
        csv_path = base_path / 'validation' / 'ode_validation_results.csv'
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        export_results_csv(results, str(csv_path))
        print(f"\nCSV exported to: {csv_path}")

    # Export full steady-state dump if requested
    if full_state:
        dump_path = base_path / 'validation' / 'ode_steady_state_dump.json'
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        export_steady_state_dump(results, str(dump_path))
        print(f"Steady-state dump saved to: {dump_path}")

    # Build v1.0 schema-compliant report
    report = build_validation_report(
        network_name=base_path.name,
        phenotype=network.metadata.get('phenotype', 'unknown'),
        species=network.metadata.get('species', 'unknown'),
        method='ODE (Hill Functions)',
        parameters={
            'K': config.K,
            'n': config.n,
            'dt': config.dt,
            'max_time': config.max_time,
            'convergence_tolerance': config.convergence_tolerance,
            'direction_threshold': config.direction_threshold,
            'activator_floor': config.activator_floor,
        },
        results=results,
        total_perturbations=len(perturbations),
        skipped=len(skipped),
        total_equations=len(network.equations),
        n_nodes=n_nodes,
        n_edges=n_edges,
        hill_formulas={
            'activation_formula': f"f(x) = x^{config.n} * ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
            'inhibition_formula': f"g(x) = ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
        },
    )

    return report


def run_sensitivity_analysis(network_dir: str,
                             csv_export: bool = False,
                             full_state: bool = False) -> Dict[str, Any]:
    """
    Run sensitivity analysis for ODE parameters (K and n).

    Sweeps:
    - K: [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
    - n: [1, 2, 3, 4]

    Returns best parameters and full results grid.
    """
    base_path = Path(network_dir)

    # Parameter ranges
    K_values = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
    n_values = [1, 2, 3, 4]

    print(f"\n{'='*70}")
    print(f"ODE SENSITIVITY ANALYSIS: {base_path.name}")
    print(f"{'='*70}")
    print(f"Sweeping K: {K_values}")
    print(f"Sweeping n: {n_values}")
    print(f"Total combinations: {len(K_values) * len(n_values)}")

    results_grid = []
    best_accuracy = -1
    best_params = {'K': 1.0, 'n': 2}
    best_report = None

    for K in K_values:
        for n in n_values:
            # Run validation silently
            report = validate_network_silent(str(network_dir), K=K, n=n)

            if 'error' in report:
                continue

            accuracy = report['metrics']['overall_accuracy']
            kappa = report['metrics']['cohens_kappa']
            mcc = report['metrics']['mcc']

            results_grid.append({
                'K': K,
                'n': n,
                'accuracy': accuracy,
                'kappa': kappa,
                'mcc': mcc
            })

            if accuracy > best_accuracy:
                best_accuracy = accuracy
                best_params = {'K': K, 'n': n}
                best_report = report

    # Print results grid
    print(f"\n{'='*50}")
    print("SENSITIVITY RESULTS")
    print(f"{'='*50}")
    print(f"{'K':>8} {'n':>4} {'Accuracy':>10} {'Kappa':>10} {'MCC':>10}")
    print("-" * 45)
    for r in results_grid:
        marker = " *" if r['K'] == best_params['K'] and r['n'] == best_params['n'] else ""
        print(f"{r['K']:>8.1f} {r['n']:>4} {r['accuracy']:>9.1f}% {r['kappa']:>10.4f} {r['mcc']:>10.4f}{marker}")

    print(f"\nBest parameters: K={best_params['K']}, n={best_params['n']}")
    print(f"Best accuracy: {best_accuracy:.1f}%")

    # If best report found, re-run with csv/full-state if requested
    if best_report and (csv_export or full_state):
        best_report = validate_network(
            str(network_dir), K=best_params['K'], n=best_params['n'],
            csv_export=csv_export, full_state=full_state
        )

    # Build sensitivity report
    sensitivity_report = {
        'network': base_path.name,
        'method': 'ODE (Hill Functions) - Sensitivity Analysis',
        'parameter_ranges': {
            'K': K_values,
            'n': n_values
        },
        'results_grid': results_grid,
        'best_parameters': best_params,
        'best_accuracy': best_accuracy,
        'best_report': best_report
    }

    return sensitivity_report


def validate_network_silent(network_dir: str, K: float = 1.0, n: int = 2) -> Dict[str, Any]:
    """Run validation without printing (for sensitivity analysis)."""
    base_path = Path(network_dir)

    equations_path = find_equations_path(network_dir)
    if not equations_path:
        return {"error": f"Equations file not found in: {base_path / 'network'}"}

    perturbations_path = find_perturbations_path(network_dir)
    if not perturbations_path:
        return {"error": "Perturbations file not found"}

    network = load_equations(str(equations_path))
    perturbations = load_perturbations(str(perturbations_path))

    # Extract edges for path length measurement
    edges = extract_edges_from_equations(network)

    config = ODEConfig(K=K, n=n)
    simulator = ODESimulator(network, config)
    validator = Validator(simulator, config)

    results, skipped = validator.validate_all(perturbations, edges=edges)

    if len(results) == 0:
        return {"error": "No perturbation tests could be executed"}

    metrics = calculate_metrics(
        results, n_nodes=len(network.equations), n_edges=len(edges)
    )
    failures = [r for r in results if not r.correct]

    return {
        'network': base_path.name,
        'parameters': {'K': K, 'n': n},
        'metrics': metrics,
        'tested': len(results),
        'skipped': len(skipped),
        'hill_formulas': {
            'activation_formula': f"f(x) = x^{config.n} * ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
            'inhibition_formula': f"g(x) = ({config.K}^{config.n} + 1) / ({config.K}^{config.n} + x^{config.n})",
        },
        'failures': [format_detailed_result(f) for f in failures],
        'detailed_results': [format_detailed_result(r) for r in results]
    }


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage: python ode_validator.py <network_dir> [--K value] [--n value]")
        print("       python ode_validator.py <network_dir> --sensitivity")
        print("       python ode_validator.py <network_dir> --csv --full-state")
        print("       python ode_validator.py --all [--sensitivity] [--csv] [--full-state]")
        sys.exit(1)

    # Get script directory
    script_dir = Path(__file__).parent

    # Parse optional parameters
    K = 1.0
    n = 2
    sensitivity = False
    csv_export = False
    full_state = False
    args = sys.argv[1:]
    filtered_args = []

    i = 0
    while i < len(args):
        if args[i] == '--K' and i + 1 < len(args):
            K = float(args[i + 1])
            i += 2
        elif args[i] == '--n' and i + 1 < len(args):
            n = int(args[i + 1])
            i += 2
        elif args[i] == '--sensitivity':
            sensitivity = True
            i += 1
        elif args[i] == '--csv':
            csv_export = True
            i += 1
        elif args[i] == '--full-state':
            full_state = True
            i += 1
        else:
            filtered_args.append(args[i])
            i += 1

    if filtered_args[0] == '--all':
        network_dirs = [
            d for d in script_dir.iterdir()
            if d.is_dir() and (d / 'network' / 'algebraic_equations.json').exists()
        ]
    else:
        network_dirs = [Path(d) for d in filtered_args]

    all_reports = []

    for network_dir in network_dirs:
        if not network_dir.is_absolute():
            network_dir = script_dir / network_dir

        if not network_dir.exists():
            print(f"Warning: Directory not found: {network_dir}")
            continue

        if sensitivity:
            # Run sensitivity analysis
            report = run_sensitivity_analysis(
                str(network_dir), csv_export=csv_export, full_state=full_state)
            all_reports.append(report)

            # Save sensitivity report
            output_path = network_dir / 'validation' / 'ode_sensitivity_results.json'
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                # Light: drop embedded best_report (duplicates ode_validation_results.json)
                json.dump({k: v for k, v in report.items() if k != 'best_report'}, f, indent=2)
            print(f"\nSensitivity report saved to: {output_path}")

            # Also save best result as main validation
            if report.get('best_report'):
                best_output = network_dir / 'validation' / 'ode_validation_results.json'
                with open(best_output, 'w', encoding='utf-8') as f:
                    json.dump(report['best_report'], f, indent=2)

                # Write ode_equations.json for best parameters
                best_K = report['best_parameters']['K']
                best_n = report['best_parameters']['n']
                best_accuracy = report['best_accuracy']
                # Reload network to write equations
                eq_path = find_equations_path(str(network_dir))
                if eq_path:
                    net = load_equations(str(eq_path))
                    best_config = ODEConfig(K=best_K, n=best_n)
                    ode_eq_path = network_dir / 'network' / 'ode_equations.json'
                    write_ode_equations(net, best_config, best_accuracy, str(ode_eq_path))
                    print(f"ODE equations saved to: {ode_eq_path}")
        else:
            report = validate_network(
                str(network_dir), K=K, n=n,
                csv_export=csv_export, full_state=full_state)
            all_reports.append(report)

            # Save individual report
            output_path = network_dir / 'validation' / 'ode_validation_results.json'
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            print(f"\nODE report saved to: {output_path}")

            # Write ode_equations.json
            if 'error' not in report:
                eq_path = find_equations_path(str(network_dir))
                if eq_path:
                    net = load_equations(str(eq_path))
                    config = ODEConfig(K=K, n=n)
                    accuracy = report.get('metrics', {}).get('overall_accuracy', 0.0)
                    ode_eq_path = network_dir / 'network' / 'ode_equations.json'
                    write_ode_equations(net, config, accuracy, str(ode_eq_path))
                    print(f"ODE equations saved to: {ode_eq_path}")

    # Print comparison summary
    if len(all_reports) > 1:
        print(f"\n{'='*70}")
        if sensitivity:
            print("ODE SENSITIVITY ANALYSIS SUMMARY")
            print(f"{'='*70}")
            print(f"{'Network':<40} {'Best K':>8} {'Best n':>6} {'Accuracy':>10}")
            print("-" * 70)
            for r in all_reports:
                if 'error' not in r and 'best_parameters' in r:
                    print(f"{r['network']:<40} {r['best_parameters']['K']:>8.1f} {r['best_parameters']['n']:>6} {r['best_accuracy']:>9.1f}%")
        else:
            print("ODE CROSS-NETWORK COMPARISON")
            print(f"{'='*70}")
            print(f"{'Network':<45} {'Accuracy':>10} {'Kappa':>10} {'MCC':>10}")
            print("-" * 75)
            for r in all_reports:
                if 'error' not in r:
                    print(f"{r['network']:<45} {r['metrics']['overall_accuracy']:>9.1f}% {r['metrics']['cohens_kappa']:>10.4f} {r['metrics']['mcc']:>10.4f}")

    return all_reports


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
================================================================================
RWR VALIDATOR - Random Walk with Restart (Signed Propagation)
================================================================================

Validates network accuracy by simulating perturbations using Random Walk with
Restart (RWR) for signed directed graphs. This is a PLUGIN validator that can
be used alongside the main flashp_validator.py (algebraic rules).

USAGE:
    python rwr_validator.py <network_dir>
    python rwr_validator.py <network_dir> --alpha 0.9 --threshold 0.05
    python rwr_validator.py <network_dir> --csv
    python rwr_validator.py <network_dir> --full-state
    python rwr_validator.py --all

EXAMPLES:
    python rwr_validator.py flowering_time_arabidopsis_network
    python rwr_validator.py shoot_branching_rice_network --alpha 0.7
    python rwr_validator.py shoot_branching_rice_network --csv --full-state
    python rwr_validator.py --all

INPUT FILES REQUIRED:
    <network_dir>/network/network.json  (for edge signs)
    <network_dir>/data/reconciled_perturbation_dataset.json

OUTPUT FILES CREATED:
    <network_dir>/validation/rwr_validation_results.json
    <network_dir>/validation/rwr_validation_results.csv       (with --csv)
    <network_dir>/validation/rwr_steady_state_dump.json       (with --full-state)

================================================================================
RANDOM WALK WITH RESTART (Signed Propagation)
================================================================================

Initial Signals (based on perturbation type):
- Knockout (KO): signal = -1.0 (negative impact)
- Knockdown (KD): signal = -0.5 (partial negative impact)
- Overexpression (OE): signal = +1.0 (positive impact)
- Wild-type (WT): signal = 0.0 (no perturbation)

Signal Propagation:
    signal[node] = alpha * mean(sign * signal[regulator]) + (1 - alpha) * initial_signal

Where:
- alpha = restart probability (default 0.85)
- sign = +1 for activation edges, -1 for inhibition edges
- Signals propagate from perturbed genes through the network topology

Direction Classification:
- signal > threshold: "increased"
- signal < -threshold: "decreased"
- otherwise: "unchanged"

Default Parameters:
- alpha = 0.85          (restart probability - higher = more signal propagation)
- threshold = 0.00001   (signal magnitude for direction classification)
- max_iterations = 50   (convergence limit)
- convergence_tol = 0.0001  (when to stop iterating)

================================================================================
KEY DIFFERENCES FROM ALGEBRAIC METHOD
================================================================================

| Aspect | Algebraic | Random Walk |
|--------|-----------|-------------|
| Values | Continuous (0 to ∞) | Signal strength (-1 to +1) |
| Baseline | WT simulation | Signal = 0 (no perturbation) |
| Direction | Compare perturbed/baseline ratio | Signal magnitude and sign |
| Mechanism | Production × inhibition | Signal propagation |

Note: RWR doesn't need WT baseline because signals represent deviation from WT.
Signal > 0 means increased vs WT, signal < 0 means decreased vs WT.

For rescue experiments (mutant + exogenous), we simulate:
1. Mutant alone → get signal at phenotype
2. Mutant + exogenous → get signal at phenotype
3. Compare the two signals to classify direction

================================================================================
"""

import json
import os
import sys
import math
from typing import Dict, List, Tuple, Any, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path

# Import shared infrastructure from validation_common
from validation_common import (
    Perturbation,
    TestResult,
    load_perturbations,
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
    find_perturbations_path,
)
from flashp_version import get_version


# ============================================================================
# CONFIGURATION - RWR Parameters
# ============================================================================
@dataclass
class RWRConfig:
    """Random Walk with Restart parameters."""
    alpha: float = 0.85            # Restart probability (signal propagation strength)
    threshold: float = 0.00001     # Signal magnitude for direction classification
    max_iterations: int = 50       # Maximum iterations
    convergence_tolerance: float = 0.0001  # When to stop iterating
    direction_threshold: float = 0.00001  # For comparing two simulations (rescue experiments)


# ============================================================================
# DATA STRUCTURES (RWR-specific - different from algebraic/ODE)
# ============================================================================
@dataclass
class Edge:
    """A single edge in the network."""
    source: str
    target: str
    sign: str  # "+" for activation, "-" for inhibition


@dataclass
class Network:
    """Network with edges for signal propagation."""
    metadata: Dict[str, Any]
    nodes: Set[str]
    edges: List[Edge]
    phenotype_node: str


# ============================================================================
# NETWORK LOADER (RWR-specific - loads from network.json, NOT algebraic_equations.json)
# ============================================================================
def load_network(network_path: str) -> Network:
    """
    Load network topology from JSON file.
    Extracts nodes and edges with their signs.
    """
    import light_io  # Light: read short-key/TOON network.json, expand to rich long-key form
    data = light_io.load(network_path)

    nodes = set()
    edges = []
    phenotype_node = None

    # Extract nodes
    nodes_data = data.get('nodes', [])
    for node_data in nodes_data:
        if isinstance(node_data, dict):
            node_id = node_data.get('id', node_data.get('name'))
            nodes.add(node_id)
            if node_data.get('type', node_data.get('node_type')) == 'PHENOTYPE':
                phenotype_node = node_id
        elif isinstance(node_data, str):
            nodes.add(node_data)

    # Extract edges
    edges_data = data.get('edges', [])
    for edge_data in edges_data:
        source = edge_data.get('source', edge_data.get('from'))
        target = edge_data.get('target', edge_data.get('to'))
        sign = edge_data.get('sign', edge_data.get('effect', '+'))

        # Normalize sign
        if sign in ('+', 'activation', 'activates', 'positive', 1, '1'):
            sign = '+'
        else:
            sign = '-'

        edges.append(Edge(source=source, target=target, sign=sign))
        nodes.add(source)
        nodes.add(target)

    return Network(
        metadata=data.get('metadata', {}),
        nodes=nodes,
        edges=edges,
        phenotype_node=phenotype_node
    )


# ============================================================================
# HELPER: Extract edge tuples for path measurement
# ============================================================================
def extract_edges_from_network(network: Network) -> List[Tuple[str, str]]:
    """Extract edges as (source, target) tuples for use with measure_path_length."""
    return [(e.source, e.target) for e in network.edges]


# ============================================================================
# RANDOM WALK SIMULATION ENGINE
# ============================================================================
class RWRSimulator:
    """Random Walk with Restart simulation engine for signed directed graphs."""

    def __init__(self, network: Network, config: RWRConfig):
        self.network = network
        self.config = config

        # Build adjacency structure: target -> [(source, sign), ...]
        self.regulators_of: Dict[str, List[Tuple[str, int]]] = {}
        for node in network.nodes:
            self.regulators_of[node] = []

        for edge in network.edges:
            sign_val = 1 if edge.sign == '+' else -1
            if edge.target in self.regulators_of:
                self.regulators_of[edge.target].append((edge.source, sign_val))

    def modifier_to_initial_signal(self, modifier: float) -> float:
        """
        Convert gene modifier to initial signal value.

        | Modifier | Meaning | Signal |
        |----------|---------|--------|
        | 0.0 | KO | -1.0 |
        | 0.5 | KD | -0.5 |
        | 1.0 | WT | 0.0 |
        | 2.0 | OE | +1.0 |
        """
        if modifier == 0.0:
            return -1.0  # Knockout
        elif modifier == 0.5:
            return -0.5  # Knockdown
        elif modifier >= 2.0:
            return +1.0  # Overexpression
        else:
            # Linear interpolation for other values
            return modifier - 1.0

    def simulate(
        self,
        gene_modifiers: Dict[str, float],
        exogenous_supply: Dict[str, float],
        debug: bool = False
    ) -> Tuple[Dict[str, float], bool, int]:
        """
        Run Random Walk with Restart simulation.

        Signal propagation:
            signal[node] = alpha * mean(sign * signal[regulator]) + (1-alpha) * initial_signal

        Returns:
            signals: Final signal values for all nodes
            converged: Whether simulation converged
            iterations: Number of iterations taken
        """
        # Initialize all signals to 0 (no perturbation = WT)
        signals = {node: 0.0 for node in self.network.nodes}

        # Set initial signals for perturbed genes
        initial_signals: Dict[str, float] = {}

        for gene, modifier in gene_modifiers.items():
            if gene in signals:
                initial_signal = self.modifier_to_initial_signal(modifier)
                initial_signals[gene] = initial_signal
                signals[gene] = initial_signal

        # Set initial signals for exogenous supply (positive impact)
        for node, value in exogenous_supply.items():
            if node in signals:
                # Exogenous supply adds positive signal
                signal_boost = min(value, 1.0)  # Cap at 1.0
                initial_signals[node] = initial_signals.get(node, 0.0) + signal_boost
                signals[node] = initial_signals[node]

        converged = False

        for iteration in range(self.config.max_iterations):
            new_signals = {}
            max_change = 0.0

            for node in self.network.nodes:
                regs = self.regulators_of.get(node, [])

                if len(regs) == 0:
                    # No regulators - keep initial signal or 0
                    new_signals[node] = initial_signals.get(node, 0.0)
                else:
                    # Propagate signal from regulators
                    incoming_sum = sum(
                        sign * signals.get(src, 0.0)
                        for src, sign in regs
                    )
                    incoming_mean = incoming_sum / len(regs)

                    # Damped update with restart
                    initial = initial_signals.get(node, 0.0)
                    new_signals[node] = self.config.alpha * incoming_mean + (1 - self.config.alpha) * initial

                change = abs(new_signals[node] - signals[node])
                max_change = max(max_change, change)

            signals = new_signals

            if debug and iteration < 10:
                phenotype = self.network.phenotype_node
                print(f"  Iter {iteration+1}: phenotype_signal={signals.get(phenotype, 0):.6f}, max_change={max_change:.6f}")

            if max_change < self.config.convergence_tolerance:
                converged = True
                return signals, converged, iteration + 1

        return signals, converged, self.config.max_iterations

    def classify_direction_from_signal(self, signal: float) -> str:
        """
        Classify direction based on signal magnitude.

        signal > threshold: "increased" (positive deviation from WT)
        signal < -threshold: "decreased" (negative deviation from WT)
        otherwise: "unchanged"
        """
        if signal > self.config.threshold:
            return 'increased'
        elif signal < -self.config.threshold:
            return 'decreased'
        else:
            return 'unchanged'

    def classify_direction_comparative(self, baseline: float, perturbed: float) -> str:
        """
        Classify direction by comparing two signal values (for rescue experiments).

        Used when comparing mutant+treatment vs mutant alone.
        """
        diff = perturbed - baseline

        if diff > self.config.threshold:
            return 'increased'
        elif diff < -self.config.threshold:
            return 'decreased'
        else:
            return 'unchanged'


# ============================================================================
# VALIDATION ENGINE
# ============================================================================
class Validator:
    """Validates network against perturbation tests using RWR simulation."""

    def __init__(self, simulator: RWRSimulator, config: RWRConfig,
                 algebraic_nodes: Set[str] = None, capture_full_state: bool = False):
        self.simulator = simulator
        self.config = config
        # For fair comparison with Flash-P/ODE, also check against algebraic equations nodes
        self.algebraic_nodes = algebraic_nodes
        self.capture_full_state = capture_full_state
        # Pre-extract edges for path measurement
        self.edge_tuples = extract_edges_from_network(simulator.network)

    def run_perturbation(self, pert: Perturbation) -> TestResult:
        """Run a single perturbation test and return the result as TestResult."""
        phenotype = pert.phenotype_node or self.simulator.network.phenotype_node

        # Determine gene_modifier for reporting (primary modifier value)
        if pert.gene_modifiers:
            gene_modifier = list(pert.gene_modifiers.values())[0]
        else:
            gene_modifier = 1.0

        if pert.comparison_baseline == 'WT' or pert.comparison_baseline == 'wt':
            # Standard comparison: perturbed vs WT
            # WT signal is 0, so we just look at the perturbed signal
            signals, converged, iterations = self.simulator.simulate(
                pert.gene_modifiers,
                pert.exogenous_supply
            )
            signal_value = signals.get(phenotype, 0.0)
            baseline_signal = 0.0  # WT baseline

            # Classify based on signal magnitude
            predicted = self.simulator.classify_direction_from_signal(signal_value)

            # Map signal to pseudo-ratio for consistent output format
            # signal > 0 means increased, so ratio = 1.0 + signal gives correct direction
            pseudo_ratio = 1.0 + signal_value

            # Capture full state if requested
            steady_state_perturbed = dict(signals) if self.capture_full_state else None

        elif pert.comparison_baseline == 'epistasis' and pert.baseline_modifiers:
            # Epistasis: compare compound perturbation vs simpler perturbation
            # First, simulate the simpler perturbation (baseline)
            baseline_signals, _, _ = self.simulator.simulate(
                pert.baseline_modifiers,
                pert.baseline_exogenous
            )
            baseline_signal = baseline_signals.get(phenotype, 0.0)

            # Then, simulate the full compound perturbation
            perturbed_signals, converged, iterations = self.simulator.simulate(
                pert.gene_modifiers,
                pert.exogenous_supply
            )
            signal_value = perturbed_signals.get(phenotype, 0.0)

            # Classify based on comparative difference
            predicted = self.simulator.classify_direction_comparative(baseline_signal, signal_value)

            # Diff-based pseudo-ratio
            diff = signal_value - baseline_signal
            pseudo_ratio = 1.0 + diff

            # Capture full state if requested
            steady_state_perturbed = dict(perturbed_signals) if self.capture_full_state else None

        else:
            # Rescue experiment: compare mutant+treatment vs mutant alone
            # First, simulate mutant alone
            baseline_signals, _, _ = self.simulator.simulate(
                pert.gene_modifiers,
                {}  # No exogenous supply
            )
            baseline_signal = baseline_signals.get(phenotype, 0.0)

            # Then, simulate mutant + treatment
            perturbed_signals, converged, iterations = self.simulator.simulate(
                pert.gene_modifiers,
                pert.exogenous_supply
            )
            signal_value = perturbed_signals.get(phenotype, 0.0)

            # Classify based on comparative difference
            predicted = self.simulator.classify_direction_comparative(baseline_signal, signal_value)

            # Diff-based pseudo-ratio
            diff = signal_value - baseline_signal
            pseudo_ratio = 1.0 + diff

            # Capture full state if requested
            steady_state_perturbed = dict(perturbed_signals) if self.capture_full_state else None

        # Compute log2 fold change from pseudo-ratio
        log2fc = safe_log2(pseudo_ratio) if pseudo_ratio > 0 else float('-inf')

        # Compute complexity and path length
        complexity_score, complexity_label = score_perturbation_complexity(pert)

        network_nodes = self.simulator.network.nodes
        perturbed_nodes = [g for g in pert.gene_modifiers.keys() if g in network_nodes]
        if not perturbed_nodes:
            perturbed_nodes = [n for n in pert.exogenous_supply.keys() if n in network_nodes]
        path_length, path = measure_path_length(self.edge_tuples, perturbed_nodes, phenotype)

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
            gene_modifier=gene_modifier,
            exogenous_node=exo_node,
            exogenous_value=exo_value,
            wt_value=0.0,  # WT signal is always 0 in RWR
            perturbed_value=signal_value,
            comparison_baseline=pert.comparison_baseline,
            comparison_baseline_value=baseline_signal,
            ratio=round(pseudo_ratio, 6),
            log2_fold_change=round(log2fc, 4) if log2fc != float('-inf') else float('-inf'),
            direction_threshold=self.config.threshold,
            predicted_direction=predicted,
            expected_direction=pert.expected_direction,
            correct=(predicted == pert.expected_direction),
            phenotype_node=phenotype,
            converged=converged,
            iterations=iterations,
            complexity_score=complexity_score,
            complexity_label=complexity_label,
            path_length=path_length,
            path=path,
            evidence_sentence=pert.evidence_sentence,
            evidence_doi=pert.evidence_doi,
            steady_state_wt=None,  # RWR WT is implicitly all zeros
            steady_state_perturbed=steady_state_perturbed,
        )

    def validate_all(self, perturbations: List[Perturbation]) -> Tuple[List[TestResult], List[Perturbation]]:
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

            # Check if perturbed genes are in the network
            # For fair comparison, check against algebraic_nodes if available
            nodes_to_check = self.algebraic_nodes if self.algebraic_nodes else self.simulator.network.nodes
            genes_in_network = False
            for gene in pert.gene_modifiers:
                if gene in nodes_to_check:
                    genes_in_network = True
                    break
            for node in pert.exogenous_supply:
                if node in nodes_to_check:
                    genes_in_network = True
                    break

            if not genes_in_network:
                skipped.append(pert)
                continue

            result = self.run_perturbation(pert)
            results.append(result)

        return results, skipped


# ============================================================================
# MAIN VALIDATION FUNCTION
# ============================================================================
def validate_network(network_dir: str, alpha: float = 0.85, threshold: float = 0.01,
                     export_csv: bool = False, full_state: bool = False) -> Dict[str, Any]:
    """
    Validate a single network directory using RWR simulation.

    Args:
        network_dir: Path to network directory
        alpha: Restart probability (default 0.85)
        threshold: Signal threshold for direction classification (default 0.01)
        export_csv: If True, export results to CSV
        full_state: If True, capture full node signals for each test

    Returns:
        Validation report dictionary
    """
    base_path = Path(network_dir)

    # Find network file
    network_path = base_path / 'network' / 'network.json'
    if not network_path.exists():
        return {"error": f"Network file not found: {network_path}"}

    # Find perturbations file (prefer reconciled) using validation_common
    perturbations_path = find_perturbations_path(str(base_path))
    if perturbations_path is None:
        return {"error": f"Perturbations file not found in {base_path / 'data'}"}

    print(f"\n{'='*70}")
    print(f"RWR VALIDATION: {base_path.name}")
    print(f"{'='*70}")

    # Load network
    print(f"Loading network from: {network_path}")
    network = load_network(str(network_path))
    print(f"  Loaded {len(network.nodes)} nodes, {len(network.edges)} edges")
    print(f"  Phenotype node: {network.phenotype_node}")

    # Load algebraic equations to get consistent node set for fair comparison
    algebraic_path = base_path / 'network' / 'algebraic_equations.json'
    algebraic_nodes = None
    if algebraic_path.exists():
        import light_io  # Light: expand short-key equations so eq['node'] resolves
        alg_data = light_io.load(str(algebraic_path))
        equations = alg_data.get('equations', [])
        # Equations can be a list of dicts with 'node' keys, or a dict keyed by node
        if isinstance(equations, list):
            algebraic_nodes = set(eq.get('node') for eq in equations if isinstance(eq, dict))
        else:
            algebraic_nodes = set(equations.keys())
            print(f"  Algebraic equations: {len(algebraic_nodes)} nodes (for fair comparison)")

    # Load perturbations using shared loader
    print(f"Loading perturbations from: {perturbations_path}")
    perturbations = load_perturbations(str(perturbations_path))
    print(f"  Loaded {len(perturbations)} perturbation tests")

    # Create simulator and validator
    config = RWRConfig(alpha=alpha, threshold=threshold)

    print(f"\nRWR parameters:")
    print(f"  alpha (restart probability): {config.alpha}")
    print(f"  threshold (direction): {config.threshold}")
    print(f"  max_iterations: {config.max_iterations}")
    print(f"  convergence_tolerance: {config.convergence_tolerance}")

    simulator = RWRSimulator(network, config)
    validator = Validator(simulator, config, algebraic_nodes=algebraic_nodes,
                          capture_full_state=full_state)

    # Run validation
    print(f"\nRunning RWR validation...")
    results, skipped = validator.validate_all(perturbations)

    print(f"  Tested: {len(results)}")
    print(f"  Skipped (not in network): {len(skipped)}")

    if len(results) == 0:
        return {
            "error": "No perturbation tests could be executed",
            "network": base_path.name,
            "skipped": len(skipped)
        }

    # Calculate metrics using shared function (includes FRS when counts provided)
    n_nodes = len(network.nodes)
    n_edges = len(network.edges)
    metrics = calculate_metrics(results, n_nodes=n_nodes, n_edges=n_edges)

    # Bootstrap confidence intervals
    ci = bootstrap_confidence_intervals(results)

    # Print results
    print(f"\n{'='*50}")
    print("RWR RESULTS SUMMARY")
    print(f"{'='*50}")
    print(f"Overall Accuracy: {metrics['overall_accuracy']}% ({metrics['correct']}/{metrics['total']})")
    print(f"Cohen's Kappa: {metrics['cohens_kappa']}")
    print(f"MCC: {metrics['mcc']}")
    print(f"Convergence Rate: {metrics['convergence_rate']}%")

    if ci:
        print(f"95% CI (accuracy): {ci['accuracy_ci']}")

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
            print(f"  {f.test_id}: expected={f.expected_direction}, predicted={f.predicted_direction}, signal={f.perturbed_value:.4f}")
        if len(failures) > 15:
            print(f"  ... and {len(failures) - 15} more failures")

    # Export CSV if requested
    if export_csv:
        csv_path = base_path / 'validation' / 'rwr_validation_results.csv'
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        export_results_csv(results, str(csv_path))
        print(f"\nCSV exported to: {csv_path}")

    # Export full state dump if requested
    if full_state:
        dump_path = base_path / 'validation' / 'rwr_steady_state_dump.json'
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        export_steady_state_dump(results, str(dump_path))
        print(f"Steady-state dump saved to: {dump_path}")

    # Build v1.0 schema-compliant report
    report = build_validation_report(
        network_name=base_path.name,
        phenotype=network.metadata.get('phenotype', 'unknown'),
        species=network.metadata.get('species', 'unknown'),
        method='Random Walk with Restart (RWR)',
        parameters={
            'alpha': config.alpha,
            'threshold': config.threshold,
            'max_iterations': config.max_iterations,
            'convergence_tolerance': config.convergence_tolerance,
            'direction_threshold': config.direction_threshold,
        },
        results=results,
        total_perturbations=len(perturbations),
        skipped=len(skipped),
        n_nodes=n_nodes,
        n_edges=n_edges,
        best_alpha=config.alpha,
    )

    return report


def run_sensitivity_analysis(network_dir: str, export_csv: bool = False,
                             full_state: bool = False) -> Dict[str, Any]:
    """
    Run sensitivity analysis for RWR alpha parameter.
    Threshold is fixed at 0.00001.

    Sweeps:
    - alpha: [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99]

    Returns best parameters and full results.
    """
    base_path = Path(network_dir)

    # Parameter range for alpha (threshold is fixed)
    alpha_values = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99]
    fixed_threshold = 0.00001

    print(f"\n{'='*70}")
    print(f"RWR SENSITIVITY ANALYSIS: {base_path.name}")
    print(f"{'='*70}")
    print(f"Sweeping alpha: {alpha_values}")
    print(f"Fixed threshold: {fixed_threshold}")

    results_list = []
    best_accuracy = -1
    best_alpha = 0.85
    best_report = None

    for alpha in alpha_values:
        # Run validation silently
        report = validate_network_silent(str(network_dir), alpha=alpha, threshold=fixed_threshold)

        if 'error' in report:
            continue

        accuracy = report['metrics']['overall_accuracy']
        kappa = report['metrics']['cohens_kappa']
        mcc = report['metrics']['mcc']

        results_list.append({
            'alpha': alpha,
            'accuracy': accuracy,
            'kappa': kappa,
            'mcc': mcc
        })

        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_alpha = alpha
            best_report = report

    # Print results
    print(f"\n{'='*50}")
    print("SENSITIVITY RESULTS")
    print(f"{'='*50}")
    print(f"{'Alpha':>8} {'Accuracy':>10} {'Kappa':>10} {'MCC':>10}")
    print("-" * 40)
    for r in results_list:
        marker = " *" if r['alpha'] == best_alpha else ""
        print(f"{r['alpha']:>8.2f} {r['accuracy']:>9.1f}% {r['kappa']:>10.4f} {r['mcc']:>10.4f}{marker}")

    print(f"\nBest alpha: {best_alpha}")
    print(f"Best accuracy: {best_accuracy:.1f}%")

    # Re-run best with full output options (csv, full-state) if requested
    if (export_csv or full_state) and best_report is not None:
        best_report = validate_network(str(network_dir), alpha=best_alpha,
                                       threshold=fixed_threshold,
                                       export_csv=export_csv, full_state=full_state)

    # Build sensitivity report
    sensitivity_report = {
        'network': base_path.name,
        'method': 'Random Walk with Restart - Sensitivity Analysis',
        'parameter_ranges': {
            'alpha': alpha_values,
            'threshold': fixed_threshold
        },
        'results': results_list,
        'best_alpha': best_alpha,
        'best_accuracy': best_accuracy,
        'best_report': best_report
    }

    return sensitivity_report


def validate_network_silent(network_dir: str, alpha: float = 0.85,
                            threshold: float = 0.00001) -> Dict[str, Any]:
    """Run validation without printing (for sensitivity analysis)."""
    base_path = Path(network_dir)

    network_path = base_path / 'network' / 'network.json'
    if not network_path.exists():
        return {"error": f"Network file not found: {network_path}"}

    perturbations_path = find_perturbations_path(str(base_path))
    if perturbations_path is None:
        return {"error": f"Perturbations file not found"}

    network = load_network(str(network_path))
    perturbations = load_perturbations(str(perturbations_path))

    # Load algebraic equations for fair comparison node set
    algebraic_path = base_path / 'network' / 'algebraic_equations.json'
    algebraic_nodes = None
    if algebraic_path.exists():
        import light_io  # Light: expand short-key equations so eq['node'] resolves
        alg_data = light_io.load(str(algebraic_path))
        equations = alg_data.get('equations', [])
        if isinstance(equations, list):
            algebraic_nodes = set(eq.get('node') for eq in equations if isinstance(eq, dict))
        else:
            algebraic_nodes = set(equations.keys())

    config = RWRConfig(alpha=alpha, threshold=threshold)
    simulator = RWRSimulator(network, config)
    validator = Validator(simulator, config, algebraic_nodes=algebraic_nodes)

    results, skipped = validator.validate_all(perturbations)

    if len(results) == 0:
        return {"error": "No perturbation tests could be executed"}

    metrics = calculate_metrics(
        results, n_nodes=len(network.nodes), n_edges=len(network.edges)
    )
    ci = bootstrap_confidence_intervals(results)
    failures = [r for r in results if not r.correct]

    return {
        'network': base_path.name,
        'parameters': {'alpha': alpha, 'threshold': threshold},
        'metrics': metrics,
        'confidence_intervals': ci,
        'tested': len(results),
        'skipped': len(skipped),
        'failures': [
            format_detailed_result(f) for f in failures
        ],
        'detailed_results': [
            format_detailed_result(r) for r in results
        ]
    }


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage: python rwr_validator.py <network_dir> [--alpha value]")
        print("       python rwr_validator.py <network_dir> --sensitivity")
        print("       python rwr_validator.py <network_dir> --csv --full-state")
        print("       python rwr_validator.py --all [--sensitivity]")
        print("\nNote: threshold is fixed at 0.00001")
        sys.exit(1)

    # Get script directory
    script_dir = Path(__file__).parent

    # Parse optional parameters
    alpha = 0.85
    threshold = 0.00001  # Fixed, not configurable
    sensitivity = False
    export_csv = False
    full_state = False
    args = sys.argv[1:]
    filtered_args = []

    i = 0
    while i < len(args):
        if args[i] == '--alpha' and i + 1 < len(args):
            alpha = float(args[i + 1])
            i += 2
        elif args[i] == '--threshold' and i + 1 < len(args):
            # Accept but ignore - threshold is fixed
            print("Warning: --threshold is ignored. Threshold is fixed at 0.00001")
            i += 2
        elif args[i] == '--sensitivity':
            sensitivity = True
            i += 1
        elif args[i] == '--csv':
            export_csv = True
            i += 1
        elif args[i] == '--full-state':
            full_state = True
            i += 1
        else:
            filtered_args.append(args[i])
            i += 1

    if not filtered_args:
        print("Error: No network directory specified.")
        sys.exit(1)

    if filtered_args[0] == '--all':
        # networks live under <project_root>/networks/<Trait>/, not next to this script —
        # optionally pass the base directory explicitly: --all <networks_dir>
        base = Path(filtered_args[1]) if len(filtered_args) > 1 else Path.cwd() / 'networks'
        network_dirs = sorted(
            d for d in (base.iterdir() if base.exists() else [])
            if d.is_dir() and (d / 'network' / 'network.json').exists()
        )
        if not network_dirs:
            print(f"Error: no networks found under {base} "
                  f"(expected <trait>/network/network.json). "
                  f"Pass the base directory explicitly: --all <networks_dir>")
            sys.exit(1)
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
            report = run_sensitivity_analysis(str(network_dir),
                                              export_csv=export_csv,
                                              full_state=full_state)
            all_reports.append(report)

            # Save sensitivity report
            output_path = network_dir / 'validation' / 'rwr_sensitivity_results.json'
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                # Light: drop embedded best_report (duplicates rwr_validation_results.json)
                json.dump({k: v for k, v in report.items() if k != 'best_report'}, f, indent=2)
            print(f"\nSensitivity report saved to: {output_path}")

            # Also save best result as main validation
            if report.get('best_report'):
                best_output = network_dir / 'validation' / 'rwr_validation_results.json'
                with open(best_output, 'w', encoding='utf-8') as f:
                    json.dump(report['best_report'], f, indent=2)
        else:
            report = validate_network(str(network_dir), alpha=alpha, threshold=threshold,
                                      export_csv=export_csv, full_state=full_state)
            all_reports.append(report)

            # Save individual report
            output_path = network_dir / 'validation' / 'rwr_validation_results.json'
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            print(f"\nRWR report saved to: {output_path}")

    # Print comparison summary
    if len(all_reports) > 1:
        print(f"\n{'='*70}")
        if sensitivity:
            print("RWR SENSITIVITY ANALYSIS SUMMARY")
            print(f"{'='*70}")
            print(f"{'Network':<45} {'Best Alpha':>12} {'Accuracy':>10}")
            print("-" * 70)
            for r in all_reports:
                if 'error' not in r and 'best_alpha' in r:
                    print(f"{r['network']:<45} {r['best_alpha']:>12.2f} {r['best_accuracy']:>9.1f}%")
        else:
            print("RWR CROSS-NETWORK COMPARISON")
            print(f"{'='*70}")
            print(f"{'Network':<45} {'Accuracy':>10} {'Kappa':>10} {'MCC':>10}")
            print("-" * 75)
            for r in all_reports:
                if 'error' not in r:
                    print(f"{r['network']:<45} {r['metrics']['overall_accuracy']:>9.1f}% {r['metrics']['cohens_kappa']:>10.4f} {r['metrics']['mcc']:>10.4f}")

    return all_reports


if __name__ == '__main__':
    main()

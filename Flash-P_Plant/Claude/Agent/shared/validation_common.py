#!/usr/bin/env python3
"""
================================================================================
VALIDATION COMMON - Shared utilities for all Flash-P validators
================================================================================

Extracts shared code from flashp_validator.py, ode_validator.py, and
rwr_validator.py into a single module for consistency and deduplication.

Provides:
- Data classes (Equation, Perturbation, ValidationResult)
- Network/perturbation loaders
- Metrics calculation (accuracy, kappa, MCC, F1, bootstrap CIs)
- Direction classification
- CSV export for supplementary tables
- Perturbation complexity & path length scoring

All three validators import from this module and only implement their
specific simulation logic.

================================================================================
"""

import csv
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flashp_version import get_version


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Equation:
    """Represents a single node's algebraic equation."""
    node: str
    node_type: str
    activators: List[str] = field(default_factory=list)
    inhibitors: List[str] = field(default_factory=list)
    exogenous_positive: List[str] = field(default_factory=list)
    exogenous_negative: List[str] = field(default_factory=list)
    environment_inhibitors: List[str] = field(default_factory=list)


@dataclass
class AlgebraicNetwork:
    """Network loaded from algebraic_equations.json (for flashp/ode validators)."""
    metadata: Dict[str, Any]
    equations: Dict[str, Equation]
    phenotype_node: str


@dataclass
class Perturbation:
    """A single perturbation test case."""
    test_id: str
    gene: str
    network_gene: Optional[str]
    perturbation_type: str
    gene_modifiers: Dict[str, float]
    exogenous_supply: Dict[str, float]
    expected_direction: str
    phenotype_node: str
    comparison_baseline: str
    in_network: bool
    additional_genes: List[str] = field(default_factory=list)
    baseline_modifiers: Dict[str, float] = field(default_factory=dict)
    baseline_exogenous: Dict[str, float] = field(default_factory=dict)
    # Evidence fields (populated from perturbation dataset if available)
    evidence_sentence: str = ""
    evidence_doi: str = ""


@dataclass
class TestResult:
    """Enhanced result of a single perturbation test with full transparency."""
    test_id: str
    gene: str
    perturbation_type: str
    gene_modifier: float
    # Computed values
    wt_value: float
    perturbed_value: float
    comparison_baseline: str
    comparison_baseline_value: float
    ratio: float
    log2_fold_change: float
    # Direction classification
    direction_threshold: float
    predicted_direction: str
    expected_direction: str
    correct: bool
    # Simulation metadata
    phenotype_node: str
    converged: bool
    iterations: int
    # Complexity & path metrics
    complexity_score: int = 1
    complexity_label: str = "easy"
    path_length: int = -1
    path: List[str] = field(default_factory=list)
    # Exogenous supply info
    exogenous_node: str = ""
    exogenous_value: float = 0.0
    # Evidence (from perturbation dataset)
    evidence_sentence: str = ""
    evidence_doi: str = ""
    # Full steady-state dump (optional, populated when --full-state is used)
    steady_state_wt: Optional[Dict[str, float]] = None
    steady_state_perturbed: Optional[Dict[str, float]] = None


# ============================================================================
# LOADERS
# ============================================================================

def load_equations(equations_path: str) -> AlgebraicNetwork:
    """
    Load network equations from algebraic_equations.json.

    Handles both list and dict formats:
    - List format: [{"node": "X", "activators": [...], ...}, ...]
    - Dict format: {"X": {"activators": [...], ...}, ...}
    """
    import light_io  # Light: read short-key/TOON files, expand to rich long-key form
    data = light_io.load(equations_path)

    equations = {}
    phenotype_node = None

    eq_data_list = data.get('equations', data)
    if isinstance(eq_data_list, dict) and 'equations' not in eq_data_list:
        eq_data_list = [
            {'node': node, **eq_info} for node, eq_info in eq_data_list.items()
        ]
    elif isinstance(eq_data_list, dict):
        eq_data_list = eq_data_list.get('equations', [])
        if isinstance(eq_data_list, dict):
            eq_data_list = [
                {'node': node, **eq_info} for node, eq_info in eq_data_list.items()
            ]

    for eq_data in eq_data_list:
        node = eq_data['node']
        node_type = eq_data.get('node_type', eq_data.get('type', 'GENE'))

        eq = Equation(
            node=node,
            node_type=node_type,
            activators=eq_data.get('activators', []),
            inhibitors=eq_data.get('inhibitors', []),
            exogenous_positive=eq_data.get('exogenous_positive', []),
            exogenous_negative=eq_data.get('exogenous_negative', []),
            environment_inhibitors=eq_data.get('environment_inhibitors', [])
        )
        equations[node] = eq

        if node_type == 'PHENOTYPE':
            phenotype_node = node

    return AlgebraicNetwork(
        metadata=data.get('metadata', {}),
        equations=equations,
        phenotype_node=phenotype_node
    )


def infer_modifier_from_type(pert_type: str) -> float:
    """Infer gene_modifier from perturbation_type string."""
    pert_type = pert_type.lower().strip()

    if pert_type in ('ko', 'knockout', 'loss_of_function', 'lof', 'null'):
        return 0.0
    if pert_type in ('kd', 'knockdown', 'rnai', 'heterozygous', 'hypomorph'):
        return 0.5
    if pert_type in ('oe', 'overexpression', 'gain_of_function', 'gof'):
        return 2.0
    if 'double' in pert_type and 'ko' in pert_type:
        return 0.0
    if pert_type in ('treatment', 'environmental', 'wt', 'wild_type', 'exogenous'):
        return 1.0
    return 1.0


def load_perturbations(perturbations_path: str) -> List[Perturbation]:
    """
    Load reconciled perturbation tests from JSON file.

    Handles multiple input formats and infers gene_modifier from
    perturbation_type when not explicitly provided.
    """
    import light_io  # Light: read short-key/TOON files, expand to rich long-key form
    data = light_io.load(perturbations_path)

    perturbations = []
    phenotype_node = data.get('metadata', {}).get('phenotype_node', 'Phenotype')

    pert_list = data.get('perturbations', data if isinstance(data, list) else [])

    for p_data in pert_list:
        if not isinstance(p_data, dict):
            continue

        # Build gene_modifiers dict
        gene_modifiers = {}
        network_gene = p_data.get('network_gene', p_data.get('gene'))
        in_network = p_data.get('in_network', True)

        if network_gene and in_network:
            if isinstance(network_gene, list):
                adj_mod = p_data.get('adjusted_modifier', {})
                if isinstance(adj_mod, dict):
                    gene_modifiers.update(adj_mod)
                else:
                    modifier = adj_mod if adj_mod is not None else p_data.get('gene_modifier')
                    if modifier is None:
                        pt = p_data.get('perturbation_type', '').lower()
                        modifier = infer_modifier_from_type(pt)
                    for g in network_gene:
                        gene_modifiers[g] = modifier
            else:
                modifier = p_data.get('adjusted_modifier')
                if modifier is None:
                    modifier = p_data.get('gene_modifier')
                if modifier is None:
                    pt = p_data.get('perturbation_type', '').lower()
                    modifier = infer_modifier_from_type(pt)
                gene_modifiers[network_gene] = modifier

        # Prefer network_gene_modifiers from reconciliation
        if 'network_gene_modifiers' in p_data and p_data['network_gene_modifiers']:
            gene_modifiers.update(p_data['network_gene_modifiers'])
        elif 'gene_modifiers' in p_data and p_data['gene_modifiers']:
            gene_modifiers.update(p_data['gene_modifiers'])

        if 'additional_genes_modifiers' in p_data:
            gene_modifiers.update(p_data['additional_genes_modifiers'])

        # Build exogenous_supply dict
        exogenous_supply = {}
        exo_data = p_data.get('exogenous_supply')
        if exo_data:
            if isinstance(exo_data, dict) and 'node' in exo_data:
                exogenous_supply[exo_data['node']] = exo_data.get('value', 1.0)
            elif isinstance(exo_data, dict):
                exogenous_supply.update(exo_data)

        adj_exo = p_data.get('adjusted_exogenous')
        if adj_exo and isinstance(adj_exo, dict):
            if 'node' in adj_exo:
                exogenous_supply[adj_exo['node']] = adj_exo.get('value', 1.0)
            else:
                exogenous_supply.update(adj_exo)

        # Determine comparison baseline
        comparison_baseline = p_data.get('comparison_baseline', 'WT')
        pert_type = p_data.get('perturbation_type', '').lower()

        # Parse baseline_modifiers for epistasis tests
        baseline_modifiers = {}
        baseline_exogenous = {}
        if comparison_baseline == 'epistasis':
            bm = p_data.get('network_baseline_modifiers',
                            p_data.get('baseline_modifiers', {}))
            if bm:
                baseline_modifiers = {str(k): float(v) for k, v in bm.items()}
            be = p_data.get('baseline_exogenous', {})
            if be:
                if isinstance(be, dict) and 'node' in be:
                    baseline_exogenous = {be['node']: be.get('value', 1.0)}
                else:
                    baseline_exogenous = {str(k): float(v) for k, v in be.items()}

        # For rescue with both gene modifier and exogenous, compare to mutant
        if comparison_baseline != 'epistasis':
            if exogenous_supply and gene_modifiers:
                for gene, mod in gene_modifiers.items():
                    if mod != 1.0:
                        comparison_baseline = 'mutant'
                        break

            if 'ko+exogenous' in pert_type or 'rescue' in pert_type:
                comparison_baseline = 'mutant'

        # Infer perturbation type
        explicit_type = p_data.get('perturbation_type', '')
        if explicit_type:
            inferred_type = explicit_type
        elif gene_modifiers:
            mod = list(gene_modifiers.values())[0] if gene_modifiers else 1.0
            if mod == 0.0:
                inferred_type = 'knockout'
            elif mod >= 2.0:
                inferred_type = 'overexpression'
            elif 0.0 < mod < 1.0:
                inferred_type = 'knockdown'
            elif mod == 1.0 and exogenous_supply:
                inferred_type = 'treatment'
            else:
                inferred_type = 'other'
        elif exogenous_supply:
            inferred_type = 'treatment'
        else:
            inferred_type = 'knockout'

        # Extract evidence fields if present
        evidence_sentence = ""
        evidence_doi = ""
        evidence_data = p_data.get('evidence', {})
        if isinstance(evidence_data, dict):
            evidence_sentence = evidence_data.get('evidence_sentence', '')
            source = evidence_data.get('source', {})
            if isinstance(source, dict):
                evidence_doi = source.get('doi', '')
            elif not evidence_doi:
                evidence_doi = evidence_data.get('doi', '')
        elif isinstance(evidence_data, list) and evidence_data:
            # v1.0: evidence is a list of flat dicts with doi at top level
            first = evidence_data[0]
            if isinstance(first, dict):
                evidence_doi = first.get('doi', '')
                if not evidence_doi and 'source' in first:
                    evidence_doi = first['source'].get('doi', '')
                evidence_sentence = first.get('evidence_sentence', '')
            elif isinstance(first, str):
                evidence_doi = first

        pert = Perturbation(
            test_id=p_data.get('test_id', p_data.get('id', f'test_{len(perturbations)+1}')),
            gene=p_data.get('gene', ''),
            network_gene=network_gene,
            perturbation_type=inferred_type,
            gene_modifiers=gene_modifiers,
            exogenous_supply=exogenous_supply,
            expected_direction=p_data.get('expected_direction', 'unchanged'),
            phenotype_node=p_data.get('phenotype_node', phenotype_node),
            comparison_baseline=comparison_baseline,
            in_network=in_network,
            additional_genes=p_data.get('additional_genes', []),
            baseline_modifiers=baseline_modifiers,
            baseline_exogenous=baseline_exogenous,
            evidence_sentence=evidence_sentence,
            evidence_doi=evidence_doi,
        )
        perturbations.append(pert)

    return perturbations


def find_equations_path(network_dir: str) -> Optional[Path]:
    """Find equations file in a network directory."""
    base = Path(network_dir)
    path = base / 'network' / 'algebraic_equations.json'
    return path if path.exists() else None


def find_perturbations_path(network_dir: str) -> Optional[Path]:
    """Find perturbations file in a network directory (prefer reconciled)."""
    base = Path(network_dir)
    reconciled = base / 'data' / 'reconciled_perturbation_dataset.json'
    if reconciled.exists():
        return reconciled
    fallback = base / 'data' / 'perturbation_dataset.json'
    return fallback if fallback.exists() else None


def find_network_json_path(network_dir: str) -> Optional[Path]:
    """Find network.json in a network directory (for RWR)."""
    base = Path(network_dir)
    path = base / 'network' / 'network.json'
    return path if path.exists() else None


# ============================================================================
# DIRECTION CLASSIFICATION
# ============================================================================

def classify_direction(ratio: float, threshold: float = 0.01) -> str:
    """
    Classify direction from a ratio (perturbed/baseline).

    Args:
        ratio: perturbed_value / baseline_value
        threshold: +-threshold for unchanged classification (default 0.01 = 1%).
            Used by the algebraic and ODE engines. RWR does NOT use this function —
            it classifies a signed propagation signal against its own +-1e-5 band.

    Returns:
        "increased", "decreased", or "unchanged"
    """
    if ratio > 1.0 + threshold:
        return 'increased'
    elif ratio < 1.0 - threshold:
        return 'decreased'
    else:
        return 'unchanged'


def safe_log2(x: float) -> float:
    """Compute log2 safely, handling zero and negative values."""
    if x <= 0:
        return float('-inf')
    return math.log2(x)


# ============================================================================
# COMPLEXITY & PATH LENGTH SCORING
# ============================================================================

def score_perturbation_complexity(pert: Perturbation) -> Tuple[int, str]:
    """
    Count total modifications: gene modifiers != 1.0 + exogenous treatments.

    Returns:
        (score, label) where label is "easy" (1), "medium" (2), or "hard" (3+)
    """
    score = 0
    for mod in pert.gene_modifiers.values():
        if mod != 1.0:
            score += 1
    score += len(pert.exogenous_supply)

    if score <= 0:
        score = 1  # Minimum score

    if score == 1:
        label = "easy"
    elif score == 2:
        label = "medium"
    else:
        label = "hard"

    return score, label


def measure_path_length(edges: List[Tuple[str, str]], perturbed_nodes: List[str],
                        phenotype_node: str) -> Tuple[int, List[str]]:
    """
    Compute shortest path from any perturbed node to phenotype using BFS.

    Args:
        edges: List of (source, target) tuples
        perturbed_nodes: List of perturbed node names
        phenotype_node: Target phenotype node

    Returns:
        (shortest_path_length, path_as_list)
        Returns (-1, []) if no path exists
    """
    # Build adjacency list
    adj: Dict[str, List[str]] = {}
    for src, tgt in edges:
        adj.setdefault(src, []).append(tgt)

    best_length = -1
    best_path: List[str] = []

    for start in perturbed_nodes:
        # BFS from start to phenotype_node
        if start == phenotype_node:
            return 0, [start]

        visited = {start}
        queue = [(start, [start])]

        while queue:
            node, path = queue.pop(0)
            for neighbor in adj.get(node, []):
                if neighbor == phenotype_node:
                    found_path = path + [neighbor]
                    found_len = len(found_path) - 1
                    if best_length == -1 or found_len < best_length:
                        best_length = found_len
                        best_path = found_path
                    break
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

    return best_length, best_path


# ============================================================================
# METRICS CALCULATION
# ============================================================================

def calculate_metrics(
    results: List[TestResult],
    n_nodes: int | None = None,
    n_edges: int | None = None,
) -> Dict[str, Any]:
    """
    Calculate comprehensive validation metrics from test results.

    Returns dict with: overall_accuracy, cohens_kappa, mcc, per_class,
    confusion_matrix, by_perturbation_type, convergence_rate.

    If ``n_nodes`` and ``n_edges`` are provided, the returned dict also
    includes the FLASH-P Rigor Score (FRS) and a tiered-rigor sub-dict
    (quality + scope + rigor). See rigor_score.py for full documentation.
    """
    if not results:
        return {"error": "No results to calculate metrics"}

    total = len(results)
    correct = sum(1 for r in results if r.correct)
    accuracy = (correct / total * 100) if total > 0 else 0.0

    # Confusion matrix
    labels = ['increased', 'decreased', 'unchanged']
    matrix = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    label_to_idx = {l: i for i, l in enumerate(labels)}

    for r in results:
        exp_idx = label_to_idx.get(r.expected_direction, 2)
        pred_idx = label_to_idx.get(r.predicted_direction, 2)
        matrix[exp_idx][pred_idx] += 1

    # Per-class metrics
    per_class = {}
    for i, label in enumerate(labels):
        tp = matrix[i][i]
        fp = sum(matrix[j][i] for j in range(3) if j != i)
        fn = sum(matrix[i][j] for j in range(3) if j != i)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        support = sum(matrix[i])

        per_class[label] = {
            'precision': round(precision, 4),
            'recall': round(recall, 4),
            'f1': round(f1, 4),
            'support': support
        }

    # Cohen's Kappa
    observed_agreement = correct / total if total > 0 else 0
    expected_agreement = 0.0
    for i in range(3):
        row_sum = sum(matrix[i])
        col_sum = sum(matrix[j][i] for j in range(3))
        expected_agreement += (row_sum * col_sum) / (total * total) if total > 0 else 0

    kappa = ((observed_agreement - expected_agreement) /
             (1 - expected_agreement)) if (1 - expected_agreement) != 0 else 0.0

    # MCC (Matthews Correlation Coefficient) - multiclass approximation
    mcc_sum = 0.0
    mcc_count = 0
    for i in range(3):
        tp = matrix[i][i]
        fp = sum(matrix[j][i] for j in range(3) if j != i)
        fn = sum(matrix[i][j] for j in range(3) if j != i)
        tn = total - tp - fp - fn

        denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
        if denom > 0:
            mcc_sum += (tp * tn - fp * fn) / denom
            mcc_count += 1

    mcc = mcc_sum / mcc_count if mcc_count > 0 else 0.0

    # By perturbation type
    by_type: Dict[str, Dict[str, Any]] = {}
    for r in results:
        ptype = r.perturbation_type
        if ptype not in by_type:
            by_type[ptype] = {'correct': 0, 'total': 0}
        by_type[ptype]['total'] += 1
        if r.correct:
            by_type[ptype]['correct'] += 1

    for ptype, data in by_type.items():
        data['accuracy'] = round(data['correct'] / data['total'] * 100, 1) if data['total'] > 0 else 0.0

    # Convergence stats
    non_converged = sum(1 for r in results if not r.converged)

    out = {
        'overall_accuracy': round(accuracy, 1),
        'correct': correct,
        'total': total,
        'cohens_kappa': round(kappa, 4),
        'mcc': round(mcc, 4),
        'per_class': per_class,
        'confusion_matrix': {
            'labels': labels,
            'matrix': matrix
        },
        'by_perturbation_type': by_type,
        'non_converged': non_converged,
        'convergence_rate': round((total - non_converged) / total * 100, 1) if total > 0 else 0.0
    }

    # Kappa 95% CI (always computed; doesn't need network counts)
    try:
        from rigor_score import compute_kappa_ci, kappa_band_label
        kappa_lo, kappa_hi = compute_kappa_ci(matrix)
        out['kappa_ci_lower'] = kappa_lo
        out['kappa_ci_upper'] = kappa_hi
        out['kappa_band'] = kappa_band_label(kappa)
    except Exception:  # pragma: no cover — keep metrics usable even if import fails
        pass

    # FLASH-P Rigor Score + Difficulty-Adjusted Rigor Score + Stratified
    if n_nodes is not None and n_edges is not None:
        from rigor_score import (
            compute_frs, compute_dars, band_label,
            compute_stratified_metrics,
        )
        # Mean path length (Tier 2 scope)
        single_gene_paths = [
            r.path_length for r in results
            if getattr(r, 'path_length', None) not in (None, -1)
        ]
        mean_path = (
            round(sum(single_gene_paths) / len(single_gene_paths), 2)
            if single_gene_paths else None
        )

        # Per-test complexity scores (1=easy, 2=medium, 3+=hard)
        complexity_scores = [
            getattr(r, 'complexity_score', 1) or 1 for r in results
        ]
        t_effective = sum(complexity_scores)

        frs = compute_frs(kappa, total, n_nodes, n_edges)
        dars = compute_dars(kappa, complexity_scores, n_nodes, n_edges)

        out['rigor_score'] = frs
        out['rigor_band'] = band_label(frs)
        out['dars'] = dars
        out['dars_band'] = band_label(dars)
        out['tier2_scope'] = {
            'n_nodes': n_nodes,
            'n_edges': n_edges,
            'n_tests': total,
            't_effective': t_effective,
            'mean_path_length': mean_path,
        }

        # Stratified per-complexity reporting
        results_labels = [
            (r.expected_direction, r.predicted_direction,
             getattr(r, 'complexity_score', 1) or 1)
            for r in results
        ]
        out['stratified'] = compute_stratified_metrics(results_labels)

    return out


def bootstrap_confidence_intervals(results: List[TestResult], n_bootstrap: int = 1000,
                                    confidence: float = 0.95, seed: int = 42) -> Dict[str, Any]:
    """
    Compute bootstrap confidence intervals for accuracy, kappa, and MCC.

    Args:
        results: List of TestResult objects
        n_bootstrap: Number of bootstrap samples
        confidence: Confidence level (default 0.95 = 95%)
        seed: Random seed for reproducibility

    Returns:
        Dict with CI for accuracy, kappa, mcc
    """
    if not results:
        return {}

    rng = random.Random(seed)
    n = len(results)
    alpha = 1 - confidence

    acc_samples = []
    kappa_samples = []
    mcc_samples = []

    for _ in range(n_bootstrap):
        sample = [rng.choice(results) for _ in range(n)]
        metrics = calculate_metrics(sample)
        acc_samples.append(metrics['overall_accuracy'])
        kappa_samples.append(metrics['cohens_kappa'])
        mcc_samples.append(metrics['mcc'])

    def ci(samples):
        s = sorted(samples)
        lo = s[int(alpha / 2 * len(s))]
        hi = s[int((1 - alpha / 2) * len(s))]
        return round(lo, 2), round(hi, 2)

    return {
        'accuracy_ci': ci(acc_samples),
        'kappa_ci': ci(kappa_samples),
        'mcc_ci': ci(mcc_samples),
        'n_bootstrap': n_bootstrap,
        'confidence_level': confidence,
        'seed': seed,
    }


# ============================================================================
# CSV EXPORT
# ============================================================================

def export_results_csv(results: List[TestResult], output_path: str) -> None:
    """
    Export validation results to CSV for supplementary material.

    Columns: test_id, gene, perturbation_type, gene_modifier, wt_value,
    perturbed_value, comparison_baseline, comparison_baseline_value, ratio,
    log2fc, direction_threshold, predicted, expected, correct,
    complexity_score, complexity_label, path_length, evidence_doi
    """
    fieldnames = [
        'test_id', 'gene', 'perturbation_type', 'gene_modifier',
        'exogenous_node', 'exogenous_value',
        'wt_value', 'perturbed_value', 'comparison_baseline',
        'comparison_baseline_value', 'ratio', 'log2_fold_change',
        'direction_threshold', 'predicted_direction', 'expected_direction',
        'correct', 'phenotype_node', 'complexity_score', 'complexity_label',
        'path_length', 'converged', 'iterations', 'evidence_doi',
    ]

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({
                'test_id': r.test_id,
                'gene': r.gene,
                'perturbation_type': r.perturbation_type,
                'gene_modifier': round(r.gene_modifier, 4),
                'exogenous_node': r.exogenous_node or '',
                'exogenous_value': round(r.exogenous_value, 4) if r.exogenous_value else '',
                'wt_value': round(r.wt_value, 6),
                'perturbed_value': round(r.perturbed_value, 6),
                'comparison_baseline': r.comparison_baseline,
                'comparison_baseline_value': round(r.comparison_baseline_value, 6),
                'ratio': round(r.ratio, 6),
                'log2_fold_change': round(r.log2_fold_change, 4) if r.log2_fold_change != float('-inf') else 'NA',
                'direction_threshold': r.direction_threshold,
                'predicted_direction': r.predicted_direction,
                'expected_direction': r.expected_direction,
                'correct': r.correct,
                'phenotype_node': r.phenotype_node,
                'complexity_score': r.complexity_score,
                'complexity_label': r.complexity_label,
                'path_length': r.path_length if r.path_length >= 0 else 'NA',
                'converged': r.converged,
                'iterations': r.iterations,
                'evidence_doi': r.evidence_doi,
            })


def export_steady_state_dump(results: List[TestResult], output_path: str) -> None:
    """
    Export full steady-state node values for each perturbation test.
    Only includes tests where steady_state data is available.
    """
    dump = []
    for r in results:
        if r.steady_state_wt is not None or r.steady_state_perturbed is not None:
            entry = {
                'test_id': r.test_id,
                'gene': r.gene,
                'perturbation_type': r.perturbation_type,
                'phenotype_node': r.phenotype_node,
                'steady_state_values': {
                    'WT': {k: round(v, 6) for k, v in (r.steady_state_wt or {}).items()},
                    'perturbed': {k: round(v, 6) for k, v in (r.steady_state_perturbed or {}).items()},
                },
                'perturbed_value': round(r.perturbed_value, 6),
                'wt_value': round(r.wt_value, 6),
                'ratio': round(r.ratio, 6),
                'predicted_direction': r.predicted_direction,
                'expected_direction': r.expected_direction,
                'correct': r.correct,
            }
            dump.append(entry)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2)


# ============================================================================
# RESULT FORMATTING
# ============================================================================

def format_detailed_result(r: TestResult) -> Dict[str, Any]:
    """
    Format a TestResult into a v1.0 schema-compliant dict.

    All fields are ALWAYS present (no conditional inclusion).
    Matches the DetailedResult schema in Agent/shared/schemas/validation.py.
    """
    return {
        'test_id': r.test_id,
        'gene': r.gene,
        'perturbation_type': r.perturbation_type,
        'gene_modifier': round(r.gene_modifier, 4),
        'wt_value': round(r.wt_value, 6),
        'perturbed_value': round(r.perturbed_value, 6),
        'comparison_baseline': r.comparison_baseline,
        'comparison_baseline_value': round(r.comparison_baseline_value, 6),
        'ratio': round(r.ratio, 6),
        'log2_fold_change': round(r.log2_fold_change, 4) if r.log2_fold_change != float('-inf') else None,
        'direction_threshold': r.direction_threshold,
        'predicted_direction': r.predicted_direction,
        'expected_direction': r.expected_direction,
        'correct': r.correct,
        'phenotype_node': r.phenotype_node,
        'converged': r.converged,
        'iterations': r.iterations,
        'complexity_score': r.complexity_score,
        'complexity_label': r.complexity_label,
        'path_length': r.path_length if r.path_length >= 0 else None,
        'path': r.path if r.path else [],
        'evidence_doi': (r.evidence_doi if isinstance(r.evidence_doi, str)
                         else r.evidence_doi.get('doi', r.evidence_doi.get('source', {}).get('doi', ''))
                         if isinstance(r.evidence_doi, dict) else '') or '',
        'exogenous_node': r.exogenous_node or None,
        'exogenous_value': r.exogenous_value if r.exogenous_value else None,
    }


def build_validation_report(
    *,
    network_name: str,
    phenotype: str,
    species: str,
    method: str,
    parameters: Dict[str, Any],
    results: List[TestResult],
    total_perturbations: int,
    skipped: int,
    total_equations: Optional[int] = None,
    equation_mode: Optional[str] = None,
    equation_spec: Optional[Dict[str, Any]] = None,
    hill_formulas: Optional[Dict[str, str]] = None,
    best_alpha: Optional[float] = None,
    n_nodes: Optional[int] = None,
    n_edges: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build a v1.0 schema-compliant validation report dict.

    Matches ValidationResultsFile schema in Agent/shared/schemas/validation.py.
    Used by all three validators (algebraic, ODE, RWR).

    When ``n_nodes`` and ``n_edges`` are provided, the returned metrics block
    includes the FLASH-P Rigor Score (FRS) and tiered-rigor sub-dict.
    """
    metrics = calculate_metrics(results, n_nodes=n_nodes, n_edges=n_edges)

    report = {
        'flash_p_version': get_version(),
        'network': network_name,
        'phenotype': phenotype,
        'species': species,
        'method': method,
        'equation_mode': equation_mode,
        'equation_spec': equation_spec,
        'parameters': parameters,
        'summary': {
            'total_perturbations': total_perturbations,
            'tested': len(results),
            'skipped': skipped,
        },
        'metrics': metrics,
        'detailed_results': [format_detailed_result(r) for r in results],
    }

    if total_equations is not None:
        report['summary']['total_equations'] = total_equations
    if hill_formulas is not None:
        report['hill_formulas'] = hill_formulas
    if best_alpha is not None:
        report['best_alpha'] = best_alpha

    return report


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == '__main__':
    print("validation_common.py - Self Test")
    print("=" * 60)

    # Test classify_direction
    assert classify_direction(1.2, 0.05) == 'increased'
    assert classify_direction(0.8, 0.05) == 'decreased'
    assert classify_direction(1.02, 0.05) == 'unchanged'
    print("classify_direction: PASS")

    # Test complexity scoring
    p = Perturbation(
        test_id="test1", gene="BRC1", network_gene="BRC1",
        perturbation_type="knockout", gene_modifiers={"BRC1": 0.0},
        exogenous_supply={}, expected_direction="increased",
        phenotype_node="Branch_Number", comparison_baseline="WT",
        in_network=True
    )
    score, label = score_perturbation_complexity(p)
    assert score == 1 and label == "easy"
    print("score_perturbation_complexity: PASS")

    # Test path length
    edges = [("A", "B"), ("B", "C"), ("C", "D"), ("A", "D")]
    length, path = measure_path_length(edges, ["A"], "D")
    assert length == 1  # direct edge A->D
    assert path == ["A", "D"]
    print("measure_path_length: PASS")

    # Test metrics on mock results
    mock_results = [
        TestResult(test_id=f"t{i}", gene="X", perturbation_type="knockout",
                   gene_modifier=0.0, wt_value=1.0, perturbed_value=2.0,
                   comparison_baseline="WT", comparison_baseline_value=1.0,
                   ratio=2.0, log2_fold_change=1.0, direction_threshold=0.05,
                   predicted_direction="increased", expected_direction="increased",
                   correct=True, phenotype_node="P", converged=True, iterations=10)
        for i in range(8)
    ] + [
        TestResult(test_id="t_fail", gene="Y", perturbation_type="knockout",
                   gene_modifier=0.0, wt_value=1.0, perturbed_value=0.5,
                   comparison_baseline="WT", comparison_baseline_value=1.0,
                   ratio=0.5, log2_fold_change=-1.0, direction_threshold=0.05,
                   predicted_direction="decreased", expected_direction="increased",
                   correct=False, phenotype_node="P", converged=True, iterations=10)
    ] + [
        TestResult(test_id="t_dec", gene="Z", perturbation_type="overexpression",
                   gene_modifier=2.0, wt_value=1.0, perturbed_value=0.3,
                   comparison_baseline="WT", comparison_baseline_value=1.0,
                   ratio=0.3, log2_fold_change=-1.74, direction_threshold=0.05,
                   predicted_direction="decreased", expected_direction="decreased",
                   correct=True, phenotype_node="P", converged=True, iterations=10)
    ]

    metrics = calculate_metrics(mock_results)
    assert metrics['overall_accuracy'] == 90.0
    print(f"calculate_metrics: PASS (accuracy={metrics['overall_accuracy']}%)")

    # Test bootstrap
    ci = bootstrap_confidence_intervals(mock_results, n_bootstrap=100)
    print(f"bootstrap_confidence_intervals: PASS (accuracy CI={ci['accuracy_ci']})")

    print("\nAll self-tests passed.")

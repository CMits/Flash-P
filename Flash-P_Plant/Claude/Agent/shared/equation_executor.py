#!/usr/bin/env python3
"""
================================================================================
EQUATION EXECUTOR — Validated Algebraic Function Menu for FLASH-P
================================================================================

Provides a menu of mathematically validated activation and inhibition functions
that the agent can select PER NODE based on biological reasoning.

ALL functions satisfy three invariants:
  1. WT normalization: f(1, 1, ..., 1) = 1.0
  2. Monotonicity: activation non-decreasing, inhibition non-increasing
  3. Boundedness: outputs are finite and non-negative

The agent picks which function to use for each node in equation_spec.json.
If no spec exists, defaults to geomean + psoup_inverse.

IMPORTANT: This module is for the ALGEBRAIC validator only.
  - ODE validator always uses Hill functions (separate math framework)
  - RWR validator always uses graph propagation (topology only)
  These three methods are NEVER mixed.

================================================================================
ACTIVATION FUNCTIONS (for combining multiple activators)
================================================================================

geomean (default):
  f(x1,...,xn) = product(max(xi, floor))^(1/n)
  AND-gate: ALL activators needed. Single KO significantly reduces output.

arithmean:
  f(x1,...,xn) = sum(max(xi, floor)) / n
  OR-gate: redundant activators. Single KO has proportional (1/n) effect.

max_pool:
  f(x1,...,xn) = max(max(xi, floor))
  Winner-takes-all: dominant regulator controls node.

weighted_geomean:
  f(x1,...,xn) = product(max(xi, floor)^wi), sum(wi) = 1
  Weighted multiplicative: primary vs secondary regulators.

min_pool:
  f(x1,...,xn) = min(max(xi, floor))
  Bottleneck: weakest link determines output.

================================================================================
INHIBITION FUNCTIONS (for combining multiple inhibitors)
================================================================================

psoup_inverse (default):
  g(x1,...,xn) = (n + 1) / (1 + sum(xi))
  PSoup rule: "the number of inhibitors plus 1, divided by the sum of
  inhibitors plus 1". Inhibitors are SUMMED. Self-normalising at WT for any n;
  de-repression is capped at (n+1) with no epsilon/K needed.
  ('bounded_inverse' is kept as a back-compat alias for this function.)

independent_inverse:
  g(x1,...,xn) = product(min(1 / max(xi, epsilon), K))
  Each inhibitor acts independently. KO of one = strong effect.

linear_clamp:
  g(x1,...,xn) = product(max(0, min(2 - xi, 1.0)))
  Simple on/off. Inhibitor > 2 = fully off. Inhibitor = 1 = no effect.

================================================================================
"""

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# DEFAULT PARAMETERS
# ============================================================================
DEFAULT_EPSILON = 0.1
DEFAULT_K = 10.0
DEFAULT_ACTIVATOR_FLOOR = 0.01


# ============================================================================
# ACTIVATION FUNCTIONS
# ============================================================================

def act_geomean(values: List[float], floor: float = DEFAULT_ACTIVATOR_FLOOR,
                **kwargs) -> float:
    """Geometric mean activation (default).
    f(x1,...,xn) = product(max(xi, floor))^(1/n)
    AND-gate: all activators needed.
    """
    if not values:
        return 1.0
    n = len(values)
    product = 1.0
    for v in values:
        product *= max(v, floor)
    return product ** (1.0 / n)


def act_arithmean(values: List[float], floor: float = DEFAULT_ACTIVATOR_FLOOR,
                  **kwargs) -> float:
    """Arithmetic mean activation.
    f(x1,...,xn) = sum(max(xi, floor)) / n
    OR-gate: redundant activators.
    """
    if not values:
        return 1.0
    n = len(values)
    total = sum(max(v, floor) for v in values)
    return total / n


def act_max_pool(values: List[float], floor: float = DEFAULT_ACTIVATOR_FLOOR,
                 **kwargs) -> float:
    """Maximum activation.
    f(x1,...,xn) = max(max(xi, floor))
    Winner-takes-all: dominant regulator controls node.
    """
    if not values:
        return 1.0
    return max(max(v, floor) for v in values)


def act_weighted_geomean(values: List[float], floor: float = DEFAULT_ACTIVATOR_FLOOR,
                         weights: Optional[List[float]] = None, **kwargs) -> float:
    """Weighted geometric mean activation.
    f(x1,...,xn) = product(max(xi, floor)^wi), sum(wi) = 1
    Unequal contributions from different activators.
    """
    if not values:
        return 1.0
    n = len(values)
    if weights is None:
        weights = [1.0 / n] * n  # Equal weights = regular geomean
    product = 1.0
    for v, w in zip(values, weights):
        product *= max(v, floor) ** w
    return product


def act_min_pool(values: List[float], floor: float = DEFAULT_ACTIVATOR_FLOOR,
                 **kwargs) -> float:
    """Minimum activation (bottleneck).
    f(x1,...,xn) = min(max(xi, floor))
    Weakest link determines output.
    """
    if not values:
        return 1.0
    return min(max(v, floor) for v in values)


# ============================================================================
# INHIBITION FUNCTIONS
# ============================================================================

def inh_psoup_inverse(values: List[float], **kwargs) -> float:
    """PSoup inhibition (default).
    g(x1,...,xn) = (n + 1) / (1 + sum(xi))

    "The number of inhibitors plus 1, divided by the sum of inhibitors plus 1"
    (PSoup algebraic rules). Inhibitors are SUMMED, not multiplied.

    Self-normalising: g(1,...,1) = (n+1)/(n+1) = 1.0 for any n, so no epsilon
    floor or K ceiling is needed. De-repression is naturally capped at (n+1)
    when every inhibitor is knocked out.
    """
    if not values:
        return 1.0
    return (len(values) + 1.0) / (1.0 + sum(values))


def inh_independent_inverse(values: List[float], epsilon: float = DEFAULT_EPSILON,
                            K: float = DEFAULT_K, **kwargs) -> float:
    """Independent inverse inhibition.
    g(x1,...,xn) = product(min(1 / max(xi, epsilon), K))
    Each inhibitor gets its own bounded inverse, then multiply.
    """
    if not values:
        return 1.0
    product = 1.0
    for v in values:
        product *= min(1.0 / max(v, epsilon), K)
    return product


def inh_linear_clamp(values: List[float], **kwargs) -> float:
    """Linear clamped inhibition.
    g(x1,...,xn) = product(max(0, min(2 - xi, 1.0)))
    Simple on/off: inhibitor > 2 = fully off, inhibitor = 1 = no effect.
    """
    if not values:
        return 1.0
    product = 1.0
    for v in values:
        product *= max(0.0, min(2.0 - v, 1.0))
    return product


# ============================================================================
# FUNCTION REGISTRY
# ============================================================================

ACTIVATION_FUNCTIONS = {
    'geomean': act_geomean,
    'arithmean': act_arithmean,
    'max_pool': act_max_pool,
    'weighted_geomean': act_weighted_geomean,
    'min_pool': act_min_pool,
}

INHIBITION_FUNCTIONS = {
    'psoup_inverse': inh_psoup_inverse,
    # Back-compat alias: existing equation_spec.json files name 'bounded_inverse'.
    # They still load, but now resolve to the PSoup term — the old bounded
    # inverse min(1/max(product, epsilon), K) is no longer available.
    'bounded_inverse': inh_psoup_inverse,
    'independent_inverse': inh_independent_inverse,
    'linear_clamp': inh_linear_clamp,
}


# ============================================================================
# SPEC LOADING AND VALIDATION
# ============================================================================

def get_default_spec() -> Dict:
    """Return the spec that reproduces current hardcoded behavior."""
    return {
        'version': '1.0',
        'default_activation': 'geomean',
        'default_inhibition': 'psoup_inverse',
        'default_params': {
            'epsilon': DEFAULT_EPSILON,
            'K_inhibition': DEFAULT_K,
            'activator_floor': DEFAULT_ACTIVATOR_FLOOR,
        },
        'node_equations': {},
    }


def load_equation_spec(path: str) -> Dict:
    """Load and parse equation_spec.json."""
    with open(path, 'r', encoding='utf-8') as f:
        spec = json.load(f)
    return spec


def validate_equation_spec(spec: Dict) -> List[str]:
    """Validate equation spec. Returns list of errors (empty = valid)."""
    errors = []

    # Check version
    if 'version' not in spec:
        errors.append("Missing 'version' field")

    # Check default functions exist in registry
    default_act = spec.get('default_activation', 'geomean')
    if default_act not in ACTIVATION_FUNCTIONS:
        errors.append(f"Unknown default activation: '{default_act}'. "
                      f"Valid: {list(ACTIVATION_FUNCTIONS.keys())}")

    default_inh = spec.get('default_inhibition', 'psoup_inverse')
    if default_inh not in INHIBITION_FUNCTIONS:
        errors.append(f"Unknown default inhibition: '{default_inh}'. "
                      f"Valid: {list(INHIBITION_FUNCTIONS.keys())}")

    # Validate per-node overrides
    node_eqs = spec.get('node_equations', {})
    for node, node_spec in node_eqs.items():
        if 'activation' in node_spec:
            act_name = node_spec['activation']
            if act_name not in ACTIVATION_FUNCTIONS:
                errors.append(f"Node '{node}': unknown activation '{act_name}'")

        if 'inhibition' in node_spec:
            inh_name = node_spec['inhibition']
            if inh_name not in INHIBITION_FUNCTIONS:
                errors.append(f"Node '{node}': unknown inhibition '{inh_name}'")

        # Validate weights for weighted_geomean
        if node_spec.get('activation') == 'weighted_geomean':
            weights = node_spec.get('activation_params', {}).get('weights')
            if weights is not None:
                weight_sum = sum(weights)
                if abs(weight_sum - 1.0) > 0.001:
                    errors.append(f"Node '{node}': weighted_geomean weights sum to "
                                  f"{weight_sum}, must sum to 1.0")

    # Verify WT normalization for all functions
    test_values = [1.0, 1.0, 1.0]
    for name, func in ACTIVATION_FUNCTIONS.items():
        result = func(test_values)
        if abs(result - 1.0) > 0.001:
            errors.append(f"Activation '{name}' fails WT normalization: "
                          f"f(1,1,1) = {result}, expected 1.0")

    for name, func in INHIBITION_FUNCTIONS.items():
        result = func(test_values)
        if abs(result - 1.0) > 0.001:
            errors.append(f"Inhibition '{name}' fails WT normalization: "
                          f"g(1,1,1) = {result}, expected 1.0")

    return errors


# ============================================================================
# EQUATION EXECUTION
# ============================================================================

def execute_equation(
    spec: Dict,
    node: str,
    node_values: Dict[str, float],
    activators: List[str],
    inhibitors: List[str],
    gene_modifier: float,
    exogenous: float,
    node_type: str
) -> float:
    """
    Compute a node's value using the equation spec.

    Uses per-node override if specified, otherwise falls back to spec defaults.

    Formula: Node = Activation(activators) * Inhibition(inhibitors) * gene_modifier + exogenous

    Args:
        spec: The loaded equation_spec.json dict
        node: Name of the node being computed
        node_values: Current values of all nodes
        activators: List of activator node names
        inhibitors: List of inhibitor node names
        gene_modifier: KO=0.0, KD=0.5, WT=1.0, OE=2.0
        exogenous: External supply (default 0.0)
        node_type: GENE, HORMONE, METABOLITE, etc.

    Returns:
        Computed node value (non-negative)
    """
    # Get default parameters
    params = spec.get('default_params', {})
    floor = params.get('activator_floor', DEFAULT_ACTIVATOR_FLOOR)
    epsilon = params.get('epsilon', DEFAULT_EPSILON)
    K = params.get('K_inhibition', DEFAULT_K)

    # Get node-specific overrides
    node_spec = spec.get('node_equations', {}).get(node, {})

    # Determine activation function
    act_name = node_spec.get('activation', spec.get('default_activation', 'geomean'))
    act_func = ACTIVATION_FUNCTIONS[act_name]

    # Determine inhibition function
    inh_name = node_spec.get('inhibition', spec.get('default_inhibition', 'psoup_inverse'))
    inh_func = INHIBITION_FUNCTIONS[inh_name]

    # Get activator values
    act_values = [node_values.get(a, 1.0) for a in activators]

    # Get inhibitor values
    inh_values = [node_values.get(i, 1.0) for i in inhibitors]

    # Get per-node parameters
    act_params = node_spec.get('activation_params', {})
    inh_params = node_spec.get('inhibition_params', {})

    # Compute activation
    activation = act_func(act_values, floor=floor, **act_params)

    # Compute inhibition
    inhibition = inh_func(inh_values, epsilon=epsilon, K=K, **inh_params)

    # gene_modifier applies to ALL node types
    base_value = activation * inhibition * gene_modifier

    return max(base_value + exogenous, 0.0)


# ============================================================================
# SELF-TESTS (run with: python equation_executor.py)
# ============================================================================

def run_self_tests():
    """Verify all functions satisfy mathematical invariants."""
    print("=" * 60)
    print("EQUATION EXECUTOR — SELF-TESTS")
    print("=" * 60)

    all_passed = True

    # Test 1: WT normalization — f(1,1,...,1) = 1.0 for all functions
    print("\nTest 1: WT normalization (all inputs = 1.0 -> output = 1.0)")
    for n_inputs in [1, 2, 3, 5]:
        test_vals = [1.0] * n_inputs
        for name, func in ACTIVATION_FUNCTIONS.items():
            kwargs = {}
            if name == 'weighted_geomean':
                kwargs['weights'] = [1.0 / n_inputs] * n_inputs
            result = func(test_vals, **kwargs)
            status = "PASS" if abs(result - 1.0) < 0.0001 else "FAIL"
            if status == "FAIL":
                all_passed = False
            print(f"  act_{name}({n_inputs} inputs): {result:.6f} [{status}]")

        for name, func in INHIBITION_FUNCTIONS.items():
            result = func(test_vals)
            status = "PASS" if abs(result - 1.0) < 0.0001 else "FAIL"
            if status == "FAIL":
                all_passed = False
            print(f"  inh_{name}({n_inputs} inputs): {result:.6f} [{status}]")

    # Test 2: KO behavior — one input = 0.0
    print("\nTest 2: KO behavior (one input = 0.0)")
    ko_vals = [0.0, 1.0, 1.0]
    for name, func in ACTIVATION_FUNCTIONS.items():
        kwargs = {}
        if name == 'weighted_geomean':
            kwargs['weights'] = [1/3, 1/3, 1/3]
        result = func(ko_vals, **kwargs)
        print(f"  act_{name}([0, 1, 1]): {result:.6f}")

    for name, func in INHIBITION_FUNCTIONS.items():
        result = func(ko_vals)
        print(f"  inh_{name}([0, 1, 1]): {result:.6f}")

    # Test 3: Empty inputs
    print("\nTest 3: Empty inputs (should return 1.0)")
    for name, func in ACTIVATION_FUNCTIONS.items():
        result = func([])
        status = "PASS" if abs(result - 1.0) < 0.0001 else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"  act_{name}([]): {result:.6f} [{status}]")

    for name, func in INHIBITION_FUNCTIONS.items():
        result = func([])
        status = "PASS" if abs(result - 1.0) < 0.0001 else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"  inh_{name}([]): {result:.6f} [{status}]")

    # Test 4: Monotonicity — increasing input -> increasing activation
    print("\nTest 4: Monotonicity (activation increases with input)")
    test_sequence = [0.1, 0.5, 1.0, 2.0, 5.0]
    for name, func in ACTIVATION_FUNCTIONS.items():
        results = [func([v]) for v in test_sequence]
        monotonic = all(results[i] <= results[i+1] for i in range(len(results)-1))
        status = "PASS" if monotonic else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"  act_{name}: {[f'{r:.3f}' for r in results]} [{status}]")

    # Test 5: Default spec produces correct output
    print("\nTest 5: Default spec backward compatibility")
    spec = get_default_spec()
    errors = validate_equation_spec(spec)
    status = "PASS" if not errors else "FAIL"
    if status == "FAIL":
        all_passed = False
    print(f"  validate_equation_spec(default): {status} {errors}")

    # Test with execute_equation
    node_values = {'A': 1.0, 'B': 1.0, 'C': 1.0}
    result = execute_equation(spec, 'test', node_values, ['A', 'B'], ['C'], 1.0, 0.0, 'GENE')
    status = "PASS" if abs(result - 1.0) < 0.0001 else "FAIL"
    if status == "FAIL":
        all_passed = False
    print(f"  execute_equation(WT): {result:.6f} [{status}]")

    # Test KO
    result_ko = execute_equation(spec, 'test', {'A': 0.0, 'B': 1.0, 'C': 1.0},
                                  ['A', 'B'], ['C'], 1.0, 0.0, 'GENE')
    print(f"  execute_equation(A=KO): {result_ko:.6f} (should be < 1.0)")

    # Test inhibitor KO
    result_inh_ko = execute_equation(spec, 'test', {'A': 1.0, 'B': 1.0, 'C': 0.0},
                                      ['A', 'B'], ['C'], 1.0, 0.0, 'GENE')
    print(f"  execute_equation(C=inh_KO): {result_inh_ko:.6f} (should be > 1.0)")

    print()
    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED — check above")
    print("=" * 60)


if __name__ == '__main__':
    run_self_tests()

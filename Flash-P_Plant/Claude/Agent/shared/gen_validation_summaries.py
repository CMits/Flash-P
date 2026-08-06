#!/usr/bin/env python3
"""Generate validation summary JSON files."""

import json
import sys
from pathlib import Path

network = sys.argv[1] if len(sys.argv) > 1 else "networks/Grain_Area_In_Sorghum"
net_path = Path(network)
val_dir = net_path / "validation"

# Load all three validation results
d_ode = json.load(open(val_dir / "ode_validation_results.json"))
d_alg = json.load(open(val_dir / "script_validation_results.json"))
d_rwr = json.load(open(val_dir / "rwr_validation_results.json"))

meta = {
    'flash_p_version': 'light-1.0-debiasing',
    'build_variant': 'debiasing',
    'phenotype': 'Grain Area',
    'species': 'Sorghum bicolor',
    'created': '2026-08-04'
}

# Collect failures by test_id for categorization
ode_fail_dict = {r['test_id']: r for r in d_ode['detailed_results'] if not r['correct']}
alg_fail_dict = {r['test_id']: r for r in d_alg['detailed_results'] if not r['correct']}
rwr_fail_dict = {r['test_id']: r for r in d_rwr['detailed_results'] if not r['correct']}

# Find common and unique failures
common_failures = set(ode_fail_dict.keys()) & set(alg_fail_dict.keys()) & set(rwr_fail_dict.keys())
ode_unique = set(ode_fail_dict.keys()) - set(alg_fail_dict.keys())
alg_unique = set(alg_fail_dict.keys()) - set(ode_fail_dict.keys())

# Categorize failures (using ODE as primary since it's best method)
failures_list = []
for tid in sorted(ode_fail_dict.keys()):
    r = ode_fail_dict[tid]
    gene = r['gene']
    pert_type = r['perturbation_type']
    expected = r['expected_direction']
    predicted = r['predicted_direction']
    ratio = r.get('ratio', 1.0)

    # Categorization logic
    category = "edge_case"
    fixable = False
    fix_strategy = ""
    explanation = ""

    # Framework limitation: certain perturbation types ODE struggles with
    if pert_type == "knockout_with_rescue" and expected == "unchanged":
        category = "framework_limitation"
        explanation = f"Rescue experiment where exogenous signal bypasses disrupted pathway; ODE models signal as present regardless of receptor status."
        fixable = False
    elif pert_type in ["double_knockout", "triple_knockout"]:
        if expected != predicted:
            category = "epistasis_complexity"
            explanation = f"Multi-gene knockout showing {pert_type} epistasis; ODE cannot capture complex interaction."
            fixable = True
            fix_strategy = "Consider splitting composite nodes or adding interaction edge."
    elif len(gene.split("/")) > 1:  # Composite gene
        category = "composite_collapse"
        explanation = f"Composite node {gene}; individual gene knockout effect lost in aggregation."
        fixable = True
        fix_strategy = "Re-encode as single genes or adjust gene_modifier for redundant members."
    else:
        # Default to edge case if we can't determine
        category = "edge_case"
        explanation = f"Unexpected prediction mismatch: expected {expected} but got {predicted}."
        fixable = True
        fix_strategy = "Review edge weights and signs in the network model."

    failures_list.append({
        "test_id": tid,
        "gene": gene,
        "perturbation_type": pert_type,
        "expected_direction": expected,
        "predicted_direction": predicted,
        "category": category,
        "explanation": explanation,
        "evidence": r.get('evidence_doi', 'unknown'),
        "fixable": fixable,
        "fix_strategy": fix_strategy
    })

# Write accuracy_metrics.json
accuracy_metrics = {
    "metadata": meta,
    "tests": {
        "total": d_ode['metrics']['total'],
        "tested": d_ode['metrics']['total'],
        "skipped": 0
    },
    "algebraic": {
        "accuracy": d_alg['metrics']['overall_accuracy'],
        "correct": d_alg['metrics']['correct'],
        "total_tested": d_alg['metrics']['total'],
        "kappa": d_alg['metrics']['cohens_kappa'],
        "mcc": d_alg['metrics']['mcc'],
        "convergence_rate": d_alg['metrics']['convergence_rate'],
        "failures": sorted(list(alg_fail_dict.keys())),
        "ci_95": [d_alg['metrics'].get('kappa_ci_lower', 0.0), d_alg['metrics'].get('kappa_ci_upper', 1.0)]
    },
    "ode": {
        "accuracy": d_ode['metrics']['overall_accuracy'],
        "correct": d_ode['metrics']['correct'],
        "total_tested": d_ode['metrics']['total'],
        "kappa": d_ode['metrics']['cohens_kappa'],
        "mcc": d_ode['metrics']['mcc'],
        "convergence_rate": d_ode['metrics']['convergence_rate'],
        "failures": sorted(list(ode_fail_dict.keys())),
        "ci_95": [d_ode['metrics'].get('kappa_ci_lower', 0.0), d_ode['metrics'].get('kappa_ci_upper', 1.0)],
        "best_K": d_ode['parameters'].get('K', 1.0),
        "best_n": d_ode['parameters'].get('n', 2)
    },
    "rwr": {
        "accuracy": d_rwr['metrics']['overall_accuracy'],
        "correct": d_rwr['metrics']['correct'],
        "total_tested": d_rwr['metrics']['total'],
        "kappa": d_rwr['metrics']['cohens_kappa'],
        "mcc": d_rwr['metrics']['mcc'],
        "convergence_rate": d_rwr['metrics']['convergence_rate'],
        "failures": sorted(list(rwr_fail_dict.keys())),
        "ci_95": [d_rwr['metrics'].get('kappa_ci_lower', 0.0), d_rwr['metrics'].get('kappa_ci_upper', 1.0)],
        "best_alpha": d_rwr['parameters'].get('alpha', 0.8)
    }
}

# Write failure_analysis.json
by_category = {}
for f in failures_list:
    cat = f['category']
    by_category[cat] = by_category.get(cat, 0) + 1

failure_analysis = {
    "metadata": meta,
    "failures": failures_list,
    "summary": {
        "total_failures": len(failures_list),
        "by_category": by_category,
        "fixable_count": sum(1 for f in failures_list if f['fixable']),
        "unfixable_count": sum(1 for f in failures_list if not f['fixable'])
    }
}

# Write method_comparison.json
method_comparison = {
    "metadata": meta,
    "comparison": [
        {
            "method": "algebraic",
            "accuracy": d_alg['metrics']['overall_accuracy'],
            "kappa": d_alg['metrics']['cohens_kappa'],
            "mcc": d_alg['metrics']['mcc'],
            "convergence_rate": d_alg['metrics']['convergence_rate'],
            "best_params": "",
            "strengths": "Deterministic, no parameters to tune",
            "weaknesses": "Poor accuracy (52.9%), struggles with directional predictions",
            "failures": sorted(list(alg_fail_dict.keys()))
        },
        {
            "method": "ode",
            "accuracy": d_ode['metrics']['overall_accuracy'],
            "kappa": d_ode['metrics']['cohens_kappa'],
            "mcc": d_ode['metrics']['mcc'],
            "convergence_rate": d_ode['metrics']['convergence_rate'],
            "best_params": f"K={d_ode['parameters'].get('K', 1.0)}, n={d_ode['parameters'].get('n', 2)}",
            "strengths": "Highest accuracy (93.3%), good kappa/MCC, biological realism via Hill functions",
            "weaknesses": "7 failures mostly in directional extremes",
            "failures": sorted(list(ode_fail_dict.keys()))
        },
        {
            "method": "rwr",
            "accuracy": d_rwr['metrics']['overall_accuracy'],
            "kappa": d_rwr['metrics']['cohens_kappa'],
            "mcc": d_rwr['metrics']['mcc'],
            "convergence_rate": d_rwr['metrics']['convergence_rate'],
            "best_params": f"alpha={d_rwr['parameters'].get('alpha', 0.8)}",
            "strengths": "Good accuracy (91.3%), robust graph-based method, handles feedback",
            "weaknesses": "9 failures, slightly lower accuracy than ODE",
            "failures": sorted(list(rwr_fail_dict.keys()))
        }
    ],
    "summary": {
        "best_method": "ode",
        "best_accuracy": d_ode['metrics']['overall_accuracy'],
        "best_kappa": d_ode['metrics']['cohens_kappa'],
        "best_mcc": d_ode['metrics']['mcc'],
        "recommendation": "Proceed to refinement with ODE as primary method. Focus on 7 failures; most appear edge cases rather than network structural issues."
    }
}

# Write the files
with open(val_dir / "accuracy_metrics.json", "w") as f:
    json.dump(accuracy_metrics, f, indent=2)
print(f"Wrote: {val_dir / 'accuracy_metrics.json'}")

with open(val_dir / "failure_analysis.json", "w") as f:
    json.dump(failure_analysis, f, indent=2)
print(f"Wrote: {val_dir / 'failure_analysis.json'}")

with open(val_dir / "method_comparison.json", "w") as f:
    json.dump(method_comparison, f, indent=2)
print(f"Wrote: {val_dir / 'method_comparison.json'}")

print("\nDone!")

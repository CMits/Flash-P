#!/usr/bin/env python3
"""
================================================================================
PERTURBATION SCAN & EPISTASIS — single + double perturbation analysis (FLASH-P)
================================================================================

WHAT THIS DOES
--------------
Given any network produced by FLASH-P, this tool enumerates every biologically
coherent single- and double-node perturbation, simulates the phenotype response
under BOTH model engines, and (optionally) quantifies and classifies the
pairwise genetic interaction (epistasis) between the two perturbations in each
double.

It reuses the pipeline's OWN simulators (validation_common / flashp_validator /
ode_validator). No equation math is re-implemented here, so every number matches
what the FLASH-P validators would produce for the same perturbation.

This is an ad-hoc analysis tool, not a numbered pipeline step. It lives in
Agent/shared/ for reuse but is intentionally OUTSIDE the Step 1-6 handoff flow.


THE TWO MODES
-------------
  (default)    SCAN. Write a TSV of every single + double perturbation with the
               phenotype log2 fold-change vs wild-type, for both models, sorted
               ascending by algebraic log2FC (strongest repressors first).

  --epistasis  EPISTASIS. For every DOUBLE, compute the pairwise interaction
               term against the additive (log-space) null, for both models, and
               flag pairs whose |interaction| exceeds --tau as NON-ADDITIVE.
               With --classify, additionally compute the masking distance and
               assign each double a genetic-interaction class (see TAXONOMY).


PERTURBABLE NODES & MODES (encoding)
------------------------------------
Perturbable = all nodes EXCEPT the PHENOTYPE and PROCESS nodes (those are
emergent read-outs, not experimentally manipulable). For the maize Stomatal
Conductance network that is 41 nodes (35 GENE + 1 HORMONE + 2 METABOLITE +
3 ENVIRONMENT). Each node is perturbed in a set of MODES, encoded as the two
levers the simulators expose (gene_modifier `gm`, additive `exogenous` supply):

    GENE                     KO   -> gm = 0.0   (null)
                             KD   -> gm = 0.5   (knock-down)
                             OE   -> gm = 2.0   (over-expression)
    HORMONE/METABOLITE/ENV   gain -> exogenous = +V   (supply / impose;  V = --exo-value)
                             loss -> gm = 0.0        (deplete / remove)

WT baseline = 1.0 for every node (FLASH-P invariant), so a single WT simulation
gives the reference phenotype and log2FC is read directly off it.

A DOUBLE is two DISTINCT nodes, taken over every combination of their modes.
Same-node pairs (e.g. KO+OE of one gene) are biologically incoherent and are
never generated. Counts for the maize SC network: 117 singles, 6,675 doubles.


EPISTASIS — DEFINITION & REASONING
----------------------------------
The model combines regulatory effects MULTIPLICATIVELY (geometric-mean
activation, bounded-inverse inhibition, Hill products). On a multiplicative
scale, independent perturbations compose multiplicatively, i.e. ADDITIVELY in
log space. So the natural null model for two perturbations A, B is:

        L_hat(A,B) = L(A) + L(B)              (additive-in-log null)

where L(.) is the phenotype log2 fold-change vs WT. The pairwise interaction
(epistasis coefficient) is the residual from that null:

        eps(A,B) = L(A,B)_observed - [ L(A) + L(B) ]

  eps ~ 0   : additive / independent (the rule, ~90% of doubles).
  eps < 0   : sub-additive  (antagonism / suppression / redundancy).
  eps > 0   : super-additive (aggravating, in the direction of effect).

Because each node in a double carries a FIXED mode, the two constituent singles
are specific single perturbations that are computed once and reused — so the
interaction needs no extra simulation beyond the singles + the double itself.

Empirical threshold: across all doubles, |epistasis_algebraic| is sharply
BIMODAL. ~85% of
pairs sit at the solver noise floor (|eps| < 1e-3; median ~4e-4) — these are the
genuinely independent pairs — with a clear gap before the interacting tail.
--tau = 0.1 (log2 units, ~7% multiplicative deviation) sits well above that
floor and is the default for calling a pair NON-ADDITIVE.


MASKING vs GENUINE INTERACTION — TAXONOMY (--classify)
------------------------------------------------------
A large |eps| does NOT by itself mean an interesting interaction. The dominant
non-additive case is MASKING (epistasis in the classical sense): one
perturbation overrides the other, so the double simply REPRODUCES one of the
singles. Signature: the double phenotype equals one single phenotype.

    mask_dist = min( |L(A,B) - L(A)| , |L(A,B) - L(B)| )

  mask_dist ~ 0  : the double = one single  -> MASKING (one node epistatic /
                   redundant chain). |eps| then just equals minus the masked
                   single's effect; it carries no new information.
  mask_dist large: the double is unlike EITHER single -> GENUINE interaction.

Classes (assigned from the ALGEBRAIC model — the canonical FLASH-P engine; the
ODE eps is reported alongside for corroboration only):

    additive    |eps| <= tau                                  (no interaction)
    masking     |eps| >  tau AND mask_dist <= tau_mask        (double = one single)
    buffering   genuine AND |L(A,B)| < min(|L(A)|,|L(B)|) - tau_mask
                            (antagonism: partners oppose, double milder than both)
    synergy     genuine AND |L(A,B)| > max(|L(A)|,|L(B)|) + tau_mask
                            (aggravating: double more extreme than either single)
    reshaping   genuine but intermediate (lands between the singles, not equal
                to either, neither clearly buffering nor synergistic)

Knobs: --tau (non-additivity), --tau-mask (masking tolerance). Both default 0.1.


OUTPUT COLUMNS
--------------
Column names are verbose-but-self-describing. Throughout, "_algebraic"/"_ode"
denote the model engine, "A"/"B" denote the two perturbations of a double, and
"log2fc" is the phenotype log2 fold-change vs WT.

SCAN mode  ->  <NET>/perturbation_scan.tsv
    perturbation_order        "single" | "double"
    n_nodes_perturbed         1 | 2
    perturbation_label        human-readable, e.g. "ZMICE1:KO + Drought:gain"
    node_A, mode_A            first perturbed node and its mode
    node_B, mode_B            second node/mode ("" for singles)
    log2fc_algebraic          phenotype log2(perturbed/WT), algebraic model
    log2fc_ode                phenotype log2(perturbed/WT), ODE (Hill) model
    phenotype_value_algebraic raw steady-state phenotype value, algebraic (WT=1.0)
    phenotype_value_ode       raw steady-state phenotype value, ODE        (WT=1.0)

EPISTASIS mode  ->  <NET>/epistasis_doubles.tsv  (sorted by |epistasis_algebraic| desc)
    node_A, mode_A, node_B, mode_B   the two perturbations
    log2fc_A_algebraic        L(A): single-perturbation A log2FC, algebraic model
    log2fc_B_algebraic        L(B): single-perturbation B log2FC, algebraic model
    log2fc_AB_algebraic       L(A,B): observed double log2FC, algebraic model
    epistasis_algebraic       interaction term = L(A,B) - L(A) - L(B)  (algebraic)
                              [deviation from the additive-in-log null; the eps term]
    log2fc_A_ode              L(A) under the ODE model
    log2fc_B_ode              L(B) under the ODE model
    log2fc_AB_ode             L(A,B) under the ODE model
    epistasis_ode             ODE interaction term (corroboration only)
    nonadditive_algebraic     |epistasis_algebraic| > tau
    nonadditive_ode           |epistasis_ode|       > tau
  with --classify, two more columns (derived from the algebraic model):
    masking_distance_algebraic  min(|L(A,B)-L(A)|, |L(A,B)-L(B)|): distance of
                                the double from its NEAREST single (~0 => masking)
    interaction_class           additive | masking | buffering | synergy | reshaping

Undefined interactions (a constituent log2FC is +/-Inf, e.g. phenotype driven to
exactly 0 in the ODE) are written as "NA" and excluded from flags/classes.


MODEL CAVEAT
------------
The ODE engine uses an n=2 Hill response, which is switch-like: it inflates eps
magnitudes and flags far more pairs as non-additive than the algebraic model.
Treat ODE values as ORDINAL corroboration; use the ALGEBRAIC eps/class as the
interpretable result. Signs agree between the two engines in practice.


USAGE
-----
    # full single+double scan
    python Agent/shared/scan_epistasis.py <NET> [--out FILE] [--exo-value V]
                                                [--no-doubles]

    # pairwise epistasis for all doubles, with classification
    python Agent/shared/scan_epistasis.py <NET> --epistasis --classify
                                                [--tau 0.1] [--tau-mask 0.1]
                                                [--out FILE] [--exo-value V]

    For gene x ENVIRONMENT (GxE) interaction rather than gene x gene epistasis,
    use the companion tool:  python Agent/shared/scan_gxe.py <NET>

    <NET>  network dir (e.g. networks/Stomatal_Conductance). Equation/structure
           files are found under <NET>/network/ (canonical) OR directly under
           <NET> (flat export) — both layouts are supported.
================================================================================
"""
import sys
import csv
import json
import math
import argparse
import itertools
from pathlib import Path

# shared modules live next to this script
SHARED = Path(__file__).resolve().parent
sys.path.insert(0, str(SHARED))

from validation_common import load_equations, safe_log2          # noqa: E402
from flashp_validator import FlashPSimulator, SimulationConfig    # noqa: E402
from ode_validator import ODESimulator, ODEConfig                 # noqa: E402

GENE_MODES = {"KO": ("gm", 0.0), "KD": ("gm", 0.5), "OE": ("gm", 2.0)}
# Accept both full-form (verbose JSON) and short-form (Light JSON) type codes
EXCLUDE_TYPES = {"PHENOTYPE", "PROCESS", "P", "PR"}
# Short-code → is-gene mapping (Light format uses "G"; verbose uses "GENE")
_GENE_TYPES = {"GENE", "G"}


def _resolve(net_dir: Path, filename: str) -> Path:
    """Find a network file under <NET>/network/ (canonical) or <NET>/ (flat)."""
    for cand in (net_dir / "network" / filename, net_dir / filename):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"{filename} not found under {net_dir}/network/ or {net_dir}/")


def _finite(x) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def classify_interaction(a, b, d, eps, tau, tau_mask):
    """Return (mask_dist, class) for a double from its algebraic log2FCs.

    a, b = single log2FCs L(A), L(B); d = double log2FC L(A,B); eps = d - a - b.
    Class taxonomy is documented in the module header (TAXONOMY section).
    Returns (None, "undefined") if any input is non-finite.
    """
    if not all(_finite(v) for v in (a, b, d, eps)):
        return None, "undefined"
    mask_dist = min(abs(d - a), abs(d - b))
    if abs(eps) <= tau:
        return mask_dist, "additive"
    if mask_dist <= tau_mask:
        return mask_dist, "masking"
    if abs(d) < min(abs(a), abs(b)) - tau_mask:
        return mask_dist, "buffering"
    if abs(d) > max(abs(a), abs(b)) + tau_mask:
        return mask_dist, "synergy"
    return mask_dist, "reshaping"


def _fmt(v):
    if v is None:
        return "NA"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        if v == float("inf"):
            return "Inf"
        if v == float("-inf"):
            return "-Inf"
        return f"{v:.6f}"
    return v


def _write_tsv(out: Path, cols, rows):
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t")
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in cols})


def main():
    ap = argparse.ArgumentParser(description="Single+double perturbation scan / epistasis.")
    ap.add_argument("net_dir", help="network directory (e.g. networks/Stomatal_Conductance)")
    ap.add_argument("--out", default=None, help="output TSV path")
    ap.add_argument("--exo-value", type=float, default=1.0,
                    help="exogenous magnitude for 'gain' modes (default 1.0)")
    ap.add_argument("--no-doubles", action="store_true", help="scan mode: singles only")
    ap.add_argument("--epistasis", action="store_true",
                    help="emit pairwise-interaction table for all doubles")
    ap.add_argument("--classify", action="store_true",
                    help="epistasis mode: add masking_distance_algebraic + "
                         "interaction_class columns (taxonomy)")
    ap.add_argument("--tau", type=float, default=0.1,
                    help="|eps| threshold to call a pair non-additive (default 0.1)")
    ap.add_argument("--tau-mask", type=float, default=0.1, dest="tau_mask",
                    help="masking tolerance: double-vs-single distance (default 0.1)")
    args = ap.parse_args()

    net_dir = Path(args.net_dir).resolve()
    eq_path = _resolve(net_dir, "algebraic_equations.json")
    net_path = _resolve(net_dir, "network.json")
    default_name = "epistasis_doubles.tsv" if args.epistasis else "perturbation_scan.tsv"
    out = Path(args.out) if args.out else net_dir / default_name

    other_modes = {"gain": ("exo", args.exo_value), "loss": ("gm", 0.0)}

    net = load_equations(str(eq_path))
    node_types = {n["id"]: n["ty"] for n in json.load(open(net_path))["nodes"]}

    def modes_for(node):
        return GENE_MODES if node_types.get(node, "GENE") in _GENE_TYPES else other_modes

    perturbable = [n for n in net.equations
                   if node_types.get(n, "GENE") not in EXCLUDE_TYPES]

    alg = FlashPSimulator(net, SimulationConfig())
    ode = ODESimulator(net, ODEConfig())
    PH = net.phenotype_node
    alg_wt = alg.get_wt_baseline()[PH]
    ode_wt = ode.get_wt_baseline()[PH]
    print(f"Network: {net_dir.name}")
    print(f"Phenotype node: {PH}")
    print(f"WT phenotype  ->  Algebraic={alg_wt:.6f}   ODE={ode_wt:.6f}")
    print(f"Perturbable nodes: {len(perturbable)}")

    def apply(items):
        gm, exo = {}, {}
        for node, mode in items:
            kind, val = modes_for(node)[mode]
            (gm if kind == "gm" else exo)[node] = val
        return gm, exo

    def log2fc(sim, wt, items):
        gm, exo = apply(items)
        vals, *_ = sim.simulate(gm, exo)
        p = vals[PH]
        return (safe_log2(p / wt) if wt > 0 else float("-inf")), p

    # ---- singles (needed by both modes) ----------------------------------
    singles = {}  # (node, mode) -> dict(a_l, o_l, a_p, o_p)
    for node in perturbable:
        for mode in modes_for(node):
            a_l, a_p = log2fc(alg, alg_wt, [(node, mode)])
            o_l, o_p = log2fc(ode, ode_wt, [(node, mode)])
            singles[(node, mode)] = dict(a_l=a_l, o_l=o_l, a_p=a_p, o_p=o_p)
    print(f"Singles: {len(singles)}")

    def iter_double_modes():
        for na, nb in itertools.combinations(perturbable, 2):
            for ma in modes_for(na):
                for mb in modes_for(nb):
                    yield (na, ma), (nb, mb)

    # ======================================================================
    # EPISTASIS MODE
    # ======================================================================
    if args.epistasis:
        def eps(L_double, L_a, L_b):
            if _finite(L_double) and _finite(L_a) and _finite(L_b):
                return L_double - L_a - L_b
            return None

        rows = []
        for (na, ma), (nb, mb) in iter_double_modes():
            a_l, _ = log2fc(alg, alg_wt, [(na, ma), (nb, mb)])
            o_l, _ = log2fc(ode, ode_wt, [(na, ma), (nb, mb)])
            sa, sb = singles[(na, ma)], singles[(nb, mb)]
            e_a = eps(a_l, sa["a_l"], sb["a_l"])
            e_o = eps(o_l, sa["o_l"], sb["o_l"])
            row = dict(
                node_A=na, mode_A=ma, node_B=nb, mode_B=mb,
                log2fc_A_algebraic=sa["a_l"], log2fc_B_algebraic=sb["a_l"],
                log2fc_AB_algebraic=a_l, epistasis_algebraic=e_a,
                log2fc_A_ode=sa["o_l"], log2fc_B_ode=sb["o_l"],
                log2fc_AB_ode=o_l, epistasis_ode=e_o,
                nonadditive_algebraic=(e_a is not None and abs(e_a) > args.tau),
                nonadditive_ode=(e_o is not None and abs(e_o) > args.tau))
            if args.classify:
                md, cls = classify_interaction(sa["a_l"], sb["a_l"], a_l, e_a,
                                               args.tau, args.tau_mask)
                row["masking_distance_algebraic"] = md
                row["interaction_class"] = cls
            rows.append(row)

        # sort by |epistasis_algebraic| desc (None -> bottom)
        rows.sort(key=lambda r: (abs(r["epistasis_algebraic"])
                                 if r["epistasis_algebraic"] is not None else -1.0),
                  reverse=True)

        cols = ["node_A", "mode_A", "node_B", "mode_B",
                "log2fc_A_algebraic", "log2fc_B_algebraic", "log2fc_AB_algebraic",
                "epistasis_algebraic",
                "log2fc_A_ode", "log2fc_B_ode", "log2fc_AB_ode", "epistasis_ode",
                "nonadditive_algebraic", "nonadditive_ode"]
        if args.classify:
            cols += ["masking_distance_algebraic", "interaction_class"]
        _write_tsv(out, cols, rows)

        n_na_a = sum(r["nonadditive_algebraic"] for r in rows)
        n_na_o = sum(r["nonadditive_ode"] for r in rows)
        n_undef = sum(r["epistasis_algebraic"] is None for r in rows)
        print(f"Doubles evaluated: {len(rows)}   (tau={args.tau}"
              + (f", tau_mask={args.tau_mask}" if args.classify else "") + ")")
        print(f"Non-additive pairs  ->  Algebraic={n_na_a}   ODE={n_na_o}"
              + (f"   (undefined eps: {n_undef})" if n_undef else ""))
        if args.classify:
            from collections import Counter
            cc = Counter(r["interaction_class"] for r in rows)
            order = ["additive", "masking", "buffering", "synergy", "reshaping", "undefined"]
            summary = "   ".join(f"{k}={cc[k]}" for k in order if cc.get(k))
            print(f"Class counts        ->  {summary}")
            print("Top non-masking (genuine) interactions [algebraic]:")
            genuine = [r for r in rows
                       if r["interaction_class"] in ("buffering", "synergy", "reshaping")]
            for r in genuine[:5]:
                print(f"  {r['interaction_class']:9s} {r['node_A']}:{r['mode_A']}"
                      f" + {r['node_B']}:{r['mode_B']}"
                      f"   epistasis={r['epistasis_algebraic']:+.3f}"
                      f"  masking_dist={r['masking_distance_algebraic']:.3f}"
                      f"   L(A)={r['log2fc_A_algebraic']:+.2f}"
                      f" L(B)={r['log2fc_B_algebraic']:+.2f}"
                      f" L(AB)={r['log2fc_AB_algebraic']:+.2f}")
        else:
            print("Top algebraic interactions:")
            for r in rows[:5]:
                if r["epistasis_algebraic"] is None:
                    break
                print(f"  {r['node_A']}:{r['mode_A']} + {r['node_B']}:{r['mode_B']}"
                      f"   epistasis_algebraic={r['epistasis_algebraic']:+.3f}"
                      f"  epistasis_ode={_fmt(r['epistasis_ode'])}")
        print(f"Wrote {len(rows)} rows -> {out}")
        return

    # ======================================================================
    # SCAN MODE (default)
    # ======================================================================
    rows = []
    for (node, mode), s in singles.items():
        rows.append(dict(perturbation_order="single", n_nodes_perturbed=1,
                         perturbation_label=f"{node}:{mode}",
                         node_A=node, mode_A=mode, node_B="", mode_B="",
                         log2fc_algebraic=s["a_l"], log2fc_ode=s["o_l"],
                         phenotype_value_algebraic=s["a_p"], phenotype_value_ode=s["o_p"]))
    if not args.no_doubles:
        for (na, ma), (nb, mb) in iter_double_modes():
            a_l, a_p = log2fc(alg, alg_wt, [(na, ma), (nb, mb)])
            o_l, o_p = log2fc(ode, ode_wt, [(na, ma), (nb, mb)])
            rows.append(dict(perturbation_order="double", n_nodes_perturbed=2,
                             perturbation_label=f"{na}:{ma} + {nb}:{mb}",
                             node_A=na, mode_A=ma, node_B=nb, mode_B=mb,
                             log2fc_algebraic=a_l, log2fc_ode=o_l,
                             phenotype_value_algebraic=a_p, phenotype_value_ode=o_p))

    n_single = sum(r["perturbation_order"] == "single" for r in rows)
    print(f"Singles: {n_single}   Doubles: {len(rows) - n_single}   Total: {len(rows)}")

    def sort_key(r):
        v = r["log2fc_algebraic"]
        return (-1e18 if v == float("-inf") else 1e18 if v == float("inf") else v)
    rows.sort(key=sort_key)

    cols = ["perturbation_order", "n_nodes_perturbed", "perturbation_label",
            "node_A", "mode_A", "node_B", "mode_B",
            "log2fc_algebraic", "log2fc_ode",
            "phenotype_value_algebraic", "phenotype_value_ode"]
    _write_tsv(out, cols, rows)
    print(f"Wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
================================================================================
GxE SCAN — gene x environment interaction analysis (FLASH-P)
================================================================================

WHAT THIS DOES
--------------
Given a FLASH-P network that contains one or more ENVIRONMENT nodes, this tool
quantifies the gene x environment (GxE) interaction for every perturbable
(non-environment) node against every environment lever. It is the environmental
counterpart of scan_epistasis.py (which does gene x gene epistasis).

It reuses the pipeline's OWN simulators (validation_common / flashp_validator /
ode_validator). No equation math is re-implemented here, so every number matches
what the FLASH-P validators would produce for the same perturbation.

This is an ad-hoc analysis tool, not a numbered pipeline step. It lives in
Agent/shared/ for reuse but is intentionally OUTSIDE the Step 1-6 handoff flow.


GxE — DEFINITION & REASONING
----------------------------
A GxE interaction is a *difference of differences*: the effect of a gene
perturbation is not the same across environments. The naive contrast
[gene]_envA - [gene]_envB is WRONG: it confounds the environmental main effect
(the wild type itself responds to the environment) with the genuine interaction.
The interaction must divide out each environment's own wild-type response.

On the FLASH-P multiplicative scale (geometric-mean activation, bounded-inverse
inhibition), independent effects compose multiplicatively = ADDITIVELY in log
space, so the natural null is "the gene's log fold-change is the same in every
environment." The interaction is the residual from that null. Working in log2,
the ANCHORED interaction of gene G (in a fixed mode) under environment E,
relative to the ambient reference, is:

    LFC_G(amb) = log2( P(G, ambient) / P(WT, ambient) )
    LFC_G(E)   = log2( P(G, E)       / P(WT, E)       )
    GxE(G, E)  = LFC_G(E) - LFC_G(amb)                      (anchored, default)

This is the log2 of the ratio-of-ratios
    [P(G,E)/P(WT,E)] / [P(G,amb)/P(WT,amb)].
GxE = 0  -> the gene acts identically with and without the environment (no
            interaction). |GxE| > 0 is the interaction magnitude (in log2 units;
            sign = whether the environment amplifies or dampens the gene effect).

With --cross, the tool also reports the CROSS-ENVIRONMENT interaction between two
environment levers E1, E2 (e.g. HighTemp vs LowTemp), which is the *correct*
form of the "high vs low" contrast because each arm keeps its own WT:

    GxE_cross(G; E1, E2) = LFC_G(E1) - LFC_G(E2)


WHERE THE SIGNAL COMES FROM (read this before interpreting small numbers)
-------------------------------------------------------------------------
Because regulatory effects combine MULTIPLICATIVELY, two perturbations that feed
genuinely separate, unsaturated branches that meet only by multiplication produce
GxE ~ 0 (they are log-additive). Non-zero GxE in a FLASH-P network arises from:
  (1) SATURATION of the bounded-inverse inhibition term (floor epsilon, ceiling
      K) -- once an inhibitor product is driven past those bounds, multiplicative
      separability breaks and an interaction appears; and
  (2) SHARED MULTI-PATH structure -- a gene that enters the phenotype cascade at
      more than one node, or an environment and a gene that co-regulate a common
      intermediate, are not separable and generate interaction.
A network whose gene and environment cascades converge only by a single clean
multiplication will therefore show modest GxE; large GxE implies a saturating or
shared bottleneck. This is expected, not a bug -- the magnitude IS the result.


ENCODING (how perturbations are imposed)
----------------------------------------
WT baseline = 1.0 for every node (FLASH-P invariant), environment nodes included,
so "ambient" = every environment lever at its 1.0 baseline (no exogenous supply).

  GENE perturbation   KO -> gene_modifier = 0.0   (default mode)
                      KD -> gene_modifier = 0.5
                      OE -> gene_modifier = 2.0
  ENVIRONMENT impose  exogenous supply = +V on the environment node
                      (V = --exo-value, default 1.0; the continuous "dose").

Perturbable genes = all nodes EXCEPT ENVIRONMENT, PHENOTYPE and PROCESS nodes.
Environment levers = all ENVIRONMENT nodes (restrict with --env NAME[,NAME...]).


OUTPUT COLUMNS
--------------
"_alg"/"_ode" denote the model engine; P(.) are raw steady-state phenotype
values (WT ambient = the baseline). Undefined contrasts (a phenotype driven to
0 so a log is non-finite) are written "NA" and excluded from the significance
flag and summary.

ANCHORED mode (default)  ->  <NET>/gxe_scan.tsv  (sorted by |gxe_alg| desc)
    gene, gene_mode, env, env_value
    ph_wt_ambient_alg, ph_wt_env_alg, ph_gene_ambient_alg, ph_gene_env_alg
    log2fc_gene_ambient_alg     LFC_G(amb), algebraic
    log2fc_gene_env_alg         LFC_G(E),   algebraic
    gxe_alg                     LFC_G(E) - LFC_G(amb)   (the interaction term)
    log2fc_gene_ambient_ode, log2fc_gene_env_ode, gxe_ode    (ODE corroboration)
    gxe_significant_alg         |gxe_alg| > tau

CROSS mode (--cross)  ->  <NET>/gxe_cross.tsv  (sorted by |gxe_cross_alg| desc)
    gene, gene_mode, env_A, env_B, env_value
    log2fc_gene_envA_alg, log2fc_gene_envB_alg, gxe_cross_alg
    log2fc_gene_envA_ode, log2fc_gene_envB_ode, gxe_cross_ode
    gxe_significant_alg         |gxe_cross_alg| > tau


ODE ENGINE PARAMETERS
---------------------
The ODE Hill response depends on (K, n), and the validator's reported ODE
accuracy is achieved at sensitivity-TUNED (K, n) -- NOT the ODEConfig defaults.
So by default (--ode-params auto) this tool reuses the validator's tuned
best_parameters from <NET>/validation/ode_sensitivity_results.json, so the ODE
gxe column reflects the SAME engine that was validated. Use --ode-params default
to fall back to the library defaults, or '--ode-params K,n' to set them
explicitly. The algebraic engine has fixed parameters and needs no tuning.


MODEL CAVEAT
------------
Even at tuned parameters the ODE Hill response is more switch-like than the
algebraic engine and can inflate interaction magnitudes. Treat the ALGEBRAIC
gxe as the interpretable result and the ODE gxe as corroboration; signs
generally agree when both engines validate.


USAGE
-----
    # anchored GxE for every gene x environment (KO of each gene vs ambient)
    python Agent/shared/scan_gxe.py <NET> [--modes KO,OE] [--exo-value V]
                                          [--env HighTemp,LowTemp] [--tau 0.1]
                                          [--out FILE]

    # cross-environment interaction (e.g. HighTemp vs LowTemp) per gene
    python Agent/shared/scan_gxe.py <NET> --cross [--modes KO] [--exo-value V]

    <NET>  network dir (e.g. networks/Flowering_Time_Temperature). Equation/
           structure files are found under <NET>/network/ (canonical) OR directly
           under <NET> (flat export) -- both layouts are supported.
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

GENE_MODES = {"KO": 0.0, "KD": 0.5, "OE": 2.0}
ENV_TYPES = {"E", "ENVIRONMENT"}
EXCLUDE_TYPES = {"E", "ENVIRONMENT", "P", "PHENOTYPE", "PR", "PROCESS"}


def _resolve(net_dir: Path, filename: str) -> Path:
    """Find a network file under <NET>/network/ (canonical) or <NET>/ (flat)."""
    for cand in (net_dir / "network" / filename, net_dir / filename):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"{filename} not found under {net_dir}/network/ or {net_dir}/")


def _finite(x) -> bool:
    return isinstance(x, float) and math.isfinite(x)


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


def _diff(x, y):
    """x - y, returning None if either is non-finite (undefined interaction)."""
    return (x - y) if (_finite(x) and _finite(y)) else None


def _tuned_ode_params(net_dir: Path):
    """Best (K, n) from the validator's ODE sensitivity sweep, or None.

    The GxE numbers are only as trustworthy as the engine that produces them;
    the ODE accuracy reported by the validator is achieved at sensitivity-TUNED
    (K, n), not the ODEConfig defaults, so by default we reuse those tuned
    parameters here. Looks for <NET>/validation/ode_sensitivity_results.json.
    """
    f = net_dir / "validation" / "ode_sensitivity_results.json"
    if not f.exists():
        return None
    try:
        bp = json.load(open(f)).get("best_parameters") or {}
        return (float(bp["K"]), int(bp["n"]))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _resolve_ode_config(net_dir: Path, spec: str):
    """Build an ODEConfig + a human label from the --ode-params spec.

    spec = "auto"     -> tuned (K,n) if available, else ODEConfig defaults
           "default"  -> ODEConfig defaults
           "K,n"      -> explicit, e.g. "0.1,2"
    """
    if spec == "default":
        c = ODEConfig()
        return c, f"default (K={c.K}, n={c.n})"
    if spec == "auto":
        kn = _tuned_ode_params(net_dir)
        if kn is None:
            c = ODEConfig()
            return c, f"default (K={c.K}, n={c.n}; no tuned sweep found)"
        K, n = kn
        return ODEConfig(K=K, n=n), f"tuned (K={K}, n={n}) from ode_sensitivity_results.json"
    try:
        ks, ns = spec.split(",")
        K, n = float(ks), int(ns)
    except ValueError:
        raise SystemExit(f"--ode-params must be 'auto', 'default', or 'K,n'; got {spec!r}")
    return ODEConfig(K=K, n=n), f"explicit (K={K}, n={n})"


def main():
    ap = argparse.ArgumentParser(
        description="Gene x environment (GxE) interaction scan.")
    ap.add_argument("net_dir",
                    help="network directory (e.g. networks/Flowering_Time_Temperature)")
    ap.add_argument("--out", default=None, help="output TSV path")
    ap.add_argument("--modes", default="KO",
                    help="comma list of gene modes from KO,KD,OE (default KO)")
    ap.add_argument("--exo-value", type=float, default=1.0,
                    help="exogenous magnitude imposed on an environment node "
                         "(default 1.0)")
    ap.add_argument("--env", default=None,
                    help="restrict environment levers to this comma list "
                         "(default: all ENVIRONMENT nodes)")
    ap.add_argument("--cross", action="store_true",
                    help="cross-environment interaction between each pair of "
                         "environment levers instead of anchored-vs-ambient")
    ap.add_argument("--tau", type=float, default=0.1,
                    help="|gxe| threshold (log2 units) to flag a meaningful "
                         "interaction (default 0.1)")
    ap.add_argument("--ode-params", default="auto", dest="ode_params",
                    help="ODE engine (K,n): 'auto' reuses the validator's tuned "
                         "best_parameters from validation/ode_sensitivity_results.json "
                         "(default), 'default' uses ODEConfig defaults, or give "
                         "explicit 'K,n' e.g. '0.1,2'")
    args = ap.parse_args()

    modes = [m.strip().upper() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in GENE_MODES]
    if bad:
        ap.error(f"unknown gene mode(s) {bad}; choose from {list(GENE_MODES)}")

    net_dir = Path(args.net_dir).resolve()
    eq_path = _resolve(net_dir, "algebraic_equations.json")
    net_path = _resolve(net_dir, "network.json")
    default_name = "gxe_cross.tsv" if args.cross else "gxe_scan.tsv"
    out = Path(args.out) if args.out else net_dir / default_name

    net = load_equations(str(eq_path))
    node_types = {n["id"]: n["ty"] for n in json.load(open(net_path))["nodes"]}
    PH = net.phenotype_node

    env_levers = [n for n in net.equations if node_types.get(n) in ENV_TYPES]
    if args.env:
        want = {e.strip() for e in args.env.split(",") if e.strip()}
        missing = want - set(env_levers)
        if missing:
            ap.error(f"--env names are not ENVIRONMENT nodes: {sorted(missing)}; "
                     f"available: {env_levers}")
        env_levers = [e for e in env_levers if e in want]
    if not env_levers:
        ap.error("no ENVIRONMENT nodes found in this network -- nothing to scan.")
    if args.cross and len(env_levers) < 2:
        ap.error("--cross needs >= 2 environment levers; "
                 f"found {env_levers}")

    genes = [n for n in net.equations if node_types.get(n) not in EXCLUDE_TYPES]

    ode_cfg, ode_label = _resolve_ode_config(net_dir, args.ode_params)
    alg = FlashPSimulator(net, SimulationConfig())
    ode = ODESimulator(net, ode_cfg)
    V = args.exo_value

    def phen(sim, gm, exo):
        vals, *_ = sim.simulate(gm, exo)
        return vals[PH]

    # ---- reference cells -------------------------------------------------
    alg_wt_amb = alg.get_wt_baseline()[PH]
    ode_wt_amb = ode.get_wt_baseline()[PH]
    # P(WT, E) per environment lever (cached)
    alg_wt_env = {e: phen(alg, {}, {e: V}) for e in env_levers}
    ode_wt_env = {e: phen(ode, {}, {e: V}) for e in env_levers}

    print(f"Network: {net_dir.name}")
    print(f"Phenotype node: {PH}")
    print(f"WT ambient phenotype  ->  Algebraic={alg_wt_amb:.6f}   ODE={ode_wt_amb:.6f}")
    print(f"ODE engine params: {ode_label}")
    print(f"Environment levers ({len(env_levers)}): {', '.join(env_levers)}   "
          f"(exo dose V={V})")
    print(f"Perturbable genes: {len(genes)}   modes: {','.join(modes)}")

    def lfc(p, ref):
        return safe_log2(p / ref) if (ref and ref > 0) else float("-inf")

    rows = []

    if not args.cross:
        # ===== ANCHORED: gene under E vs gene at ambient ==================
        for gene in genes:
            for m in modes:
                gv = GENE_MODES[m]
                a_g_amb = phen(alg, {gene: gv}, {})
                o_g_amb = phen(ode, {gene: gv}, {})
                la_amb = lfc(a_g_amb, alg_wt_amb)
                lo_amb = lfc(o_g_amb, ode_wt_amb)
                for e in env_levers:
                    a_g_env = phen(alg, {gene: gv}, {e: V})
                    o_g_env = phen(ode, {gene: gv}, {e: V})
                    la_env = lfc(a_g_env, alg_wt_env[e])
                    lo_env = lfc(o_g_env, ode_wt_env[e])
                    gxe_a = _diff(la_env, la_amb)
                    gxe_o = _diff(lo_env, lo_amb)
                    rows.append(dict(
                        gene=gene, gene_mode=m, env=e, env_value=V,
                        ph_wt_ambient_alg=alg_wt_amb, ph_wt_env_alg=alg_wt_env[e],
                        ph_gene_ambient_alg=a_g_amb, ph_gene_env_alg=a_g_env,
                        log2fc_gene_ambient_alg=la_amb, log2fc_gene_env_alg=la_env,
                        gxe_alg=gxe_a,
                        log2fc_gene_ambient_ode=lo_amb, log2fc_gene_env_ode=lo_env,
                        gxe_ode=gxe_o,
                        gxe_significant_alg=(gxe_a is not None and abs(gxe_a) > args.tau),
                    ))
        cols = ["gene", "gene_mode", "env", "env_value",
                "ph_wt_ambient_alg", "ph_wt_env_alg",
                "ph_gene_ambient_alg", "ph_gene_env_alg",
                "log2fc_gene_ambient_alg", "log2fc_gene_env_alg", "gxe_alg",
                "log2fc_gene_ambient_ode", "log2fc_gene_env_ode", "gxe_ode",
                "gxe_significant_alg"]
        sort_field = "gxe_alg"
    else:
        # ===== CROSS-ENVIRONMENT: gene under E1 vs gene under E2 ==========
        for gene in genes:
            for m in modes:
                gv = GENE_MODES[m]
                lfc_env_alg, lfc_env_ode = {}, {}
                for e in env_levers:
                    lfc_env_alg[e] = lfc(phen(alg, {gene: gv}, {e: V}), alg_wt_env[e])
                    lfc_env_ode[e] = lfc(phen(ode, {gene: gv}, {e: V}), ode_wt_env[e])
                for ea, eb in itertools.combinations(env_levers, 2):
                    gxe_a = _diff(lfc_env_alg[ea], lfc_env_alg[eb])
                    gxe_o = _diff(lfc_env_ode[ea], lfc_env_ode[eb])
                    rows.append(dict(
                        gene=gene, gene_mode=m, env_A=ea, env_B=eb, env_value=V,
                        log2fc_gene_envA_alg=lfc_env_alg[ea],
                        log2fc_gene_envB_alg=lfc_env_alg[eb],
                        gxe_cross_alg=gxe_a,
                        log2fc_gene_envA_ode=lfc_env_ode[ea],
                        log2fc_gene_envB_ode=lfc_env_ode[eb],
                        gxe_cross_ode=gxe_o,
                        gxe_significant_alg=(gxe_a is not None and abs(gxe_a) > args.tau),
                    ))
        cols = ["gene", "gene_mode", "env_A", "env_B", "env_value",
                "log2fc_gene_envA_alg", "log2fc_gene_envB_alg", "gxe_cross_alg",
                "log2fc_gene_envA_ode", "log2fc_gene_envB_ode", "gxe_cross_ode",
                "gxe_significant_alg"]
        sort_field = "gxe_cross_alg"

    rows.sort(key=lambda r: (abs(r[sort_field]) if r[sort_field] is not None else -1.0),
              reverse=True)
    _write_tsv(out, cols, rows)

    n_sig = sum(r["gxe_significant_alg"] for r in rows)
    n_undef = sum(r[sort_field] is None for r in rows)
    print(f"Rows: {len(rows)}   meaningful GxE (|{sort_field}|>{args.tau}, algebraic): "
          f"{n_sig}" + (f"   (undefined: {n_undef})" if n_undef else ""))
    label = "gene x env" if not args.cross else "cross-env"
    print(f"Top {label} interactions [algebraic]:")
    for r in rows[:8]:
        if r[sort_field] is None:
            break
        if not args.cross:
            print(f"  {r['gene']:6s}:{r['gene_mode']:2s} @ {r['env']:9s}"
                  f"   gxe={r['gxe_alg']:+.3f}"
                  f"   LFC(amb)={r['log2fc_gene_ambient_alg']:+.2f}"
                  f"  LFC(env)={r['log2fc_gene_env_alg']:+.2f}"
                  f"   gxe_ode={_fmt(r['gxe_ode'])}")
        else:
            print(f"  {r['gene']:6s}:{r['gene_mode']:2s}  {r['env_A']} vs {r['env_B']}"
                  f"   gxe={r['gxe_cross_alg']:+.3f}"
                  f"   LFC({r['env_A']})={r['log2fc_gene_envA_alg']:+.2f}"
                  f"  LFC({r['env_B']})={r['log2fc_gene_envB_alg']:+.2f}")
    print(f"Wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()

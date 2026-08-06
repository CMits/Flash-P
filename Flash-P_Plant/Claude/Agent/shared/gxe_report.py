#!/usr/bin/env python3
"""
================================================================================
GxE REPORT — preflight-checked, dose-swept gene x environment analysis (FLASH-P)
================================================================================

WHAT THIS DOES
--------------
Driver that runs a *best-practice* gene x environment (GxE) analysis on ANY
FLASH-P network and writes the result + a plain-language report into the
network's own directory. It is the deterministic engine behind the
`run-flashp-gxe` command; it can also be run directly.

It wraps scan_gxe.py (the canonical GxE math) so every number matches the
FLASH-P validators, and adds the three things a naive scan cannot do safely on
an arbitrary network:

  1. PREFLIGHT — checks the environment nodes are usable GxE levers and flags
     the ones that aren't (non-source, or non-directional like a bare
     'Temperature' node), with a ready-to-paste prompt to rebuild the network
     with split directional nodes (e.g. TempHigh / TempLow).
  2. DOSE SWEEP — runs each environment lever at several exogenous doses so you
     can see how the interaction scales and which (lever, dose) combinations
     saturate (and are therefore uninformative).
  3. REPORT — writes <NET>/gxe/GXE_REPORT.md summarising levers, warnings,
     per-(lever,dose) saturation, algebraic-vs-ODE engine agreement, and the top
     GxE hits per environment, plus the two merged TSVs.

ENGINE POLICY
-------------
The fixed-parameter ALGEBRAIC engine is the primary, interpretable result. The
ODE column is corroboration and is run at the validator's sensitivity-TUNED
(K, n) when a sweep exists (scan_gxe --ode-params auto); otherwise it falls back
to defaults and the report says so. (Auto-running the validator to PRODUCE a
missing sweep is the calling command's job, not this script's.)

OUTPUT  ->  <NET>/gxe/
    gxe_anchored.tsv   gene x env x dose, interaction vs ambient (the env_value
                       column is the dose). Sorted by |gxe_alg| desc.
    gxe_cross.tsv      gene x (envA,envB) x dose cross-environment interaction.
    GXE_REPORT.md      human-readable summary, warnings and remediation.

USAGE
-----
    python Agent/shared/gxe_report.py <NET> [--modes KO,OE]
        [--doses 0.25,0.5,1,2] [--tau 0.1] [--saturation-log2 3.0]
        [--top 8] [--ode-params auto]

    <NET>  network dir (canonical <NET>/network/ or flat <NET>/ both supported).
================================================================================
"""
import sys
import csv
import json
import math
import argparse
import subprocess
import re
from pathlib import Path
from collections import defaultdict

SHARED = Path(__file__).resolve().parent
SCAN_GXE = SHARED / "scan_gxe.py"

# env nodes whose name is an inherently BIDIRECTIONAL physical axis but carries
# no direction qualifier -> the imposed +V direction is ambiguous to a reader.
_DIR_QUALIFIERS = re.compile(r"high|low|warm|cool|cold|heat|chill|vern|elevat|reduc|deficit|excess|short|long", re.I)
_BIDIRECTIONAL = re.compile(r"\btemp(erature)?\b|\bph\b|salinit|osmotic|\bredox\b", re.I)


def _resolve(net_dir: Path, filename: str) -> Path:
    for cand in (net_dir / "network" / filename, net_dir / filename):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"{filename} not found under {net_dir}/network/ or {net_dir}/")


def _num(s):
    """Parse a scan_gxe TSV cell to float, or None for NA/Inf."""
    if s in ("NA", "Inf", "-Inf", "", None):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_tsv(path: Path):
    with open(path) as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _ty(n):
    return n.get("ty", n.get("type", "?"))


def _is_source(n):
    return bool(n.get("src", n.get("is_source", False)))


def classify_env(node_id, out_edges):
    """Return (is_ambiguous, note). out_edges = list of (target, sign)."""
    name_bidir = bool(_BIDIRECTIONAL.search(node_id))
    name_dir = bool(_DIR_QUALIFIERS.search(node_id))
    if name_bidir and not name_dir:
        tgt = ", ".join(f"{t}{'+' if s > 0 else '-'}" for t, s in out_edges) or "(no targets)"
        return True, (f"name is a bidirectional axis with no high/low qualifier; "
                      f"+V imposes only the node's net wired effect ({tgt}). "
                      f"The opposite extreme cannot be tested from this single node.")
    return False, ""


def rebuild_prompt(node_id, phenotype, species):
    hi = f"{node_id}High"
    lo = f"{node_id}Low"
    return (
        f"To get an unambiguous high/low contrast for '{node_id}', rebuild this "
        f"network with two DIRECTIONAL source nodes instead of one. Suggested prompt:\n\n"
        f"    /run-flashp {phenotype} in {species}\n"
        f"    In the literature-review instruction, REPLACE the single '{node_id}' "
        f"environment node with two directional source nodes '{hi}' and '{lo}', each "
        f"wired to its OWN downstream cascade (the high and low programs are usually "
        f"distinct, e.g. opposite molecular responses), both converging on the "
        f"phenotype. Keep every other env node as-is. See "
        f"networks/Flowering_Time_Temperature for the directional-node pattern.\n\n"
        f"Then GxE on '{hi}' and '{lo}' separately gives a true high-vs-low contrast."
    )


def run_scan(net_dir, mode_flag, modes, dose, ode_params, out_path):
    cmd = [sys.executable, str(SCAN_GXE), str(net_dir),
           "--modes", modes, "--exo-value", str(dose),
           "--ode-params", ode_params, "--out", str(out_path)]
    if mode_flag == "cross":
        cmd.append("--cross")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"scan_gxe failed (dose {dose}, {mode_flag}):\n{res.stderr.strip()}")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Preflight-checked, dose-swept GxE report.")
    ap.add_argument("net_dir")
    ap.add_argument("--modes", default="KO,OE")
    ap.add_argument("--doses", default="0.25,0.5,1,2")
    ap.add_argument("--tau", type=float, default=0.1)
    ap.add_argument("--saturation-log2", type=float, default=3.0, dest="sat",
                    help="|WT phenotype log2FC| above this flags a (lever,dose) as "
                         "saturated/uninformative (default 3.0 ~ algebraic K ceiling)")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--ode-params", default="auto")
    args = ap.parse_args()

    net_dir = Path(args.net_dir).resolve()
    doses = [float(d) for d in args.doses.split(",") if d.strip()]
    out_dir = net_dir / "gxe"
    out_dir.mkdir(parents=True, exist_ok=True)

    net = json.load(open(_resolve(net_dir, "network.json")))
    meta = net.get("metadata", {})
    phenotype = meta.get("phenotype", net_dir.name)
    species = meta.get("species", "the species")
    nodes = net["nodes"]
    edges = net["edges"]

    env_nodes = [n for n in nodes if _ty(n) in ("E", "ENVIRONMENT")]
    warnings = []
    if not env_nodes:
        msg = (f"Network '{net_dir.name}' has NO environment nodes — GxE analysis is "
               f"not applicable. Add ENVIRONMENT source node(s) (e.g. Drought, "
               f"HighTemp) wired into the phenotype cascade, then re-run.")
        (out_dir / "GXE_REPORT.md").write_text(
            f"# GxE report — {net_dir.name}\n\n**NOT GxE-CAPABLE.** {msg}\n")
        print(msg)
        sys.exit(2)

    # --- preflight per env lever ---
    out_by_src = defaultdict(list)
    for e in edges:
        out_by_src[e["s"]].append((e["t"], e["x"]))
    env_info = []
    for n in env_nodes:
        eid = n["id"]
        outs = out_by_src.get(eid, [])
        amb, note = classify_env(eid, outs)
        src = _is_source(n)
        if not src:
            warnings.append(f"[non-source] env node '{eid}' is regulated (has inputs); "
                            f"the ambient=1.0 reference may not hold — interpret with care.")
        if amb:
            warnings.append(f"[ambiguous-direction] env node '{eid}': {note}")
        env_info.append(dict(id=eid, src=src, out_deg=len(outs),
                             targets=outs, ambiguous=amb, note=note))

    # --- engine status ---
    sweep = net_dir / "validation" / "ode_sensitivity_results.json"
    if sweep.exists():
        bp = json.load(open(sweep)).get("best_parameters", {})
        ode_status = f"tuned (K={bp.get('K')}, n={bp.get('n')}) from validation sweep"
    else:
        ode_status = ("DEFAULT params (no validation/ode_sensitivity_results.json) — "
                      "ODE column is untuned; rely on the algebraic column")
        warnings.append("[ode-untuned] " + ode_status)

    # --- dose sweep: run scan_gxe per dose, merge ---
    anchored_rows, cross_rows = [], []
    have_cross = len(env_nodes) >= 2
    for d in doses:
        a = run_scan(net_dir, "anchored", args.modes, d, args.ode_params,
                     out_dir / f".tmp_anchored_{d}.tsv")
        anchored_rows += _read_tsv(a)
        a.unlink()
        if have_cross:
            c = run_scan(net_dir, "cross", args.modes, d, args.ode_params,
                         out_dir / f".tmp_cross_{d}.tsv")
            cross_rows += _read_tsv(c)
            c.unlink()

    def write_merged(rows, path):
        if not rows:
            return
        cols = list(rows[0].keys())
        rows.sort(key=lambda r: (abs(_num(r.get("gxe_alg") or r.get("gxe_cross_alg")) or -1)),
                  reverse=True)
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t")
            w.writeheader(); w.writerows(rows)
    write_merged(anchored_rows, out_dir / "gxe_anchored.tsv")
    if have_cross:
        write_merged(cross_rows, out_dir / "gxe_cross.tsv")

    # --- saturation: per (env, dose) from WT phenotype swing ---
    sat = {}  # (env,dose) -> wt swing log2FC (or None)
    for r in anchored_rows:
        env, dose = r["env"], r["env_value"]
        pa, pe = _num(r["ph_wt_ambient_alg"]), _num(r["ph_wt_env_alg"])
        if pa and pe and pa > 0 and pe > 0:
            sat[(env, dose)] = math.log2(pe / pa)
    sat_flags = {k: v for k, v in sat.items() if v is not None and abs(v) > args.sat}

    # --- engine agreement (rows where both engines call it meaningful) ---
    agree = defaultdict(lambda: [0, 0])  # env -> [agree, total]
    for r in anchored_rows:
        a_, o_ = _num(r["gxe_alg"]), _num(r["gxe_ode"])
        if a_ is None or o_ is None:
            continue
        if abs(a_) > args.tau and abs(o_) > args.tau:
            agree[r["env"]][1] += 1
            if (a_ > 0) == (o_ > 0):
                agree[r["env"]][0] += 1

    # --- top hits per env at the largest UNSATURATED dose ---
    def ref_dose(env):
        # env_value in the tsv is scan_gxe's %.6f formatting of the dose
        unsat = [d for d in sorted(doses)
                 if abs(sat.get((env, f"{d:.6f}"), 0.0)) <= args.sat]
        return f"{(max(unsat) if unsat else min(doses)):.6f}"
    top = {}
    for n in env_info:
        env = n["id"]; rd = ref_dose(env)
        rows = [r for r in anchored_rows if r["env"] == env and r["env_value"] == rd
                and _num(r["gxe_alg"]) is not None]
        rows.sort(key=lambda r: abs(_num(r["gxe_alg"])), reverse=True)
        top[env] = (rd, rows[:args.top])

    # ===================== write GXE_REPORT.md =====================
    L = []
    L.append(f"# GxE report — {net_dir.name}\n")
    L.append(f"- **Phenotype node / trait:** {phenotype}")
    L.append(f"- **Species:** {species}")
    L.append(f"- **Environment levers:** {', '.join(n['id'] for n in env_info)}")
    L.append(f"- **Gene modes:** {args.modes}   **Dose sweep:** {', '.join(map(str, doses))}")
    L.append(f"- **Primary engine:** algebraic (fixed params).  **ODE corroboration:** {ode_status}")
    out_line = "- **Outputs:** `gxe/gxe_anchored.tsv`"
    out_line += ", `gxe/gxe_cross.tsv`" if have_cross else " (no `gxe_cross.tsv` — needs ≥2 levers)"
    out_line += ", and this report.\n"
    L.append(out_line)

    L.append("## Warnings & limitations\n")
    if warnings:
        for w in warnings:
            L.append(f"- {w}")
    else:
        L.append("- None. All env levers are directional source nodes; ODE is tuned.")
    L.append("")

    # rebuild suggestions for ambiguous nodes
    amb_nodes = [n for n in env_info if n["ambiguous"]]
    if amb_nodes:
        L.append("## Suggested fix — rebuild with directional env nodes\n")
        for n in amb_nodes:
            L.append(f"### `{n['id']}`")
            L.append("```")
            L.append(rebuild_prompt(n["id"], phenotype, species))
            L.append("```\n")

    L.append("## Environment levers\n")
    L.append("| lever | source? | out-deg | directional? | wired targets (sign) |")
    L.append("|---|---|---|---|---|")
    for n in env_info:
        tgt = ", ".join(f"{t}{'+' if s > 0 else '−'}" for t, s in n["targets"][:8])
        if len(n["targets"]) > 8:
            tgt += ", …"
        L.append(f"| {n['id']} | {'yes' if n['src'] else 'NO'} | {n['out_deg']} | "
                 f"{'NO — ambiguous' if n['ambiguous'] else 'yes'} | {tgt} |")
    L.append("")

    L.append("## Saturation by (lever, dose)\n")
    L.append("WT-phenotype log2FC when the lever is imposed (no gene perturbation). "
             f"|log2FC| > {args.sat} = saturated/uninformative; prefer smaller doses there.\n")
    L.append("| lever | " + " | ".join(f"V={d}" for d in sorted(doses)) + " |")
    L.append("|---|" + "|".join("---" for _ in doses) + "|")
    for n in env_info:
        cells = []
        for d in sorted(doses):
            v = sat.get((n["id"], f"{d:.6f}"))
            cells.append("—" if v is None else (f"**{v:+.2f}!**" if abs(v) > args.sat else f"{v:+.2f}"))
        L.append(f"| {n['id']} | " + " | ".join(cells) + " |")
    L.append("")

    L.append("## Algebraic vs ODE engine agreement (sign)\n")
    L.append("Fraction of meaningful gene×env calls where both engines agree on the "
             "sign of the interaction. Low agreement → trust the algebraic column.\n")
    L.append("| lever | sign agreement |")
    L.append("|---|---|")
    for n in env_info:
        a, t = agree.get(n["id"], [0, 0])
        L.append(f"| {n['id']} | {f'{a}/{t} ({100*a/t:.0f}%)' if t else 'n/a (no dual-meaningful calls)'} |")
    L.append("")

    L.append(f"## Top GxE hits per environment (algebraic, at largest unsaturated dose)\n")
    for n in env_info:
        rd, rows = top[n["id"]]
        L.append(f"### {n['id']}  (dose V={float(rd):g})"
                 + ("  ⚠ ambiguous direction" if n["ambiguous"] else ""))
        if not rows:
            L.append("_no defined interactions at this dose._\n"); continue
        L.append("| gene | mode | gxe (log2 ratio-of-ratios) | LFC@ambient | LFC@env |")
        L.append("|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {r['gene']} | {r['gene_mode']} | {_num(r['gxe_alg']):+.3f} | "
                     f"{_num(r['log2fc_gene_ambient_alg']):+.2f} | {_num(r['log2fc_gene_env_alg']):+.2f} |")
        L.append("")

    L.append("## How to read this\n")
    L.append("- **gxe** is the log2 ratio-of-ratios `[gene/WT]@env ÷ [gene/WT]@ambient`. "
             "0 = the gene acts the same with/without the environment (no interaction); "
             "sign = whether the environment amplifies or dampens the gene's effect.")
    L.append("- Interactions come from saturation of the bounded-inverse term and from "
             "genes/environments sharing downstream nodes; a clean single multiplication "
             "gives ~0. See `scan_gxe.py` header for the full rationale.")
    L.append("- Magnitudes scale with dose; use the dose sweep to find the informative range.")

    (out_dir / "GXE_REPORT.md").write_text("\n".join(L) + "\n")

    # ---- console summary ----
    print(f"GxE report written -> {out_dir}/GXE_REPORT.md")
    print(f"  levers: {', '.join(n['id'] for n in env_info)}   "
          f"doses: {', '.join(map(str, doses))}   modes: {args.modes}")
    print(f"  ODE: {ode_status}")
    if sat_flags:
        print(f"  saturated (lever,dose): {len(sat_flags)} flagged (see report)")
    if amb_nodes:
        print(f"  ⚠ ambiguous-direction env nodes: {', '.join(n['id'] for n in amb_nodes)} "
              f"(rebuild prompt in report)")
    if warnings and not amb_nodes:
        print(f"  warnings: {len(warnings)} (see report)")


if __name__ == "__main__":
    main()

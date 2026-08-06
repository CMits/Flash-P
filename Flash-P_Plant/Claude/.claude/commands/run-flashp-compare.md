---
description: Compare TWO built FLASH-P networks (same trait across species, or two traits) into one self-contained HTML report — conserved mechanisms, sign conflicts, species-specific nodes/edges, and accuracy side-by-side.
argument-hint: <netA> <netB>  e.g. "Carbon_To_Nitrogen_Ratio_In_Barley Carbon_To_Nitrogen_Ratio_In_Wild_Barley"
model: claude-sonnet-4-6
---

# FLASH-P Compare (two networks, side-by-side)

Networks to compare: **$ARGUMENTS**  (two trait networks — either a full path to a trait dir that
contains `network/network.json`, or a bare trait-folder name under `networks/`).

You are generating a **cross-network comparison report**: one self-contained HTML file that reads both
networks' `network/network.json` + `validation/accuracy_metrics.json` and lays them side-by-side. The
heavy work lives in `Agent/shared/network_to_compare.py`; your job is to invoke it and relay the result.
Keep it token-lean: read only the script's stdout summary.

## What the report contains
- **Summary cards** — node/edge counts and best-method accuracy for each network.
- **Validation accuracy** — algebraic / ODE / RWR (accuracy, κ, MCC, convergence) for both.
- **Conserved mechanisms** — regulatory links present in BOTH networks with the SAME sign.
- **Divergent regulation** — shared source→target links whose sign FLIPPED between networks (the
  biologically interesting divergences).
- **Species-specific nodes** — nodes present in only one network (e.g. wild-barley-only genes).
- **Species-specific interactions** — regulatory links unique to one network.

Node/edge identity is matched **case-insensitively**, because the pipeline cases node IDs differently
between networks (`Nitrate_Supply` vs `NITRATE_SUPPLY`, `CN_Ratio` vs `CN_RATIO`); genes like
`HVNRT2_1` already match exactly.

## Execution plan

1. **Resolve both networks.** Need exactly two arguments. Each may be a trait-dir path or a bare name
   under `networks/`. If either has no `network/network.json`, say so and stop.

2. **Run the script:**
   ```
   python Agent/shared/network_to_compare.py <netA> <netB>
   ```
   Optional `--out <path>` to choose the output file; default is
   `networks/Compare_<A>_vs_<B>.html`.

3. **Relay the output** concisely: the report path (open by double-click) and the headline numbers
   (shared nodes, conserved edges, sign conflicts, nodes unique to each side).

## Guard rails
- Read-only with respect to the networks — the script only writes the comparison HTML; it never modifies
  `network/`, `data/`, `validation/`, equations, or any pipeline files.
- Do not invent metrics — accuracy/κ/MCC come straight from each network's
  `validation/accuracy_metrics.json`; edges and signs come straight from each `network/network.json`.
- A "sign conflict" means the same source→target link exists in both networks with opposite sign — report
  it as a divergence to inspect, not an error.

---
description: Run a gene × gene epistasis analysis on an existing FLASH-P trait network — enumerates every single + double perturbation, classifies pairwise interactions, results saved in the network dir.
argument-hint: <network dir>  e.g. "networks/Stomatal_Conductance"
model: claude-sonnet-4-6
---

# FLASH-P epistasis analysis

Target network directory: **$ARGUMENTS**  (the `<NET>` for this run; resolve a bare trait name to `networks/<Trait>/`).

You are orchestrating a **gene × gene epistasis** analysis on an **already-built** FLASH-P network
(this command does NOT build a network — use `/run-flashp` for that; for gene × **environment** interaction
use `/run-flashp-gxe` instead). The heavy, deterministic work lives in `Agent/shared/scan_epistasis.py`,
which reuses the pipeline's own simulators so every number matches the FLASH-P validators. Your job is to
sanity-check the network, invoke the script with the right flags, and relay its findings — especially the
**masking vs genuine interaction** distinction and the **ODE caveat**. Keep it token-lean: pipe script
output through `tail`, read only the printed summary, never dump the full TSV into the thread.

## What the analysis does (already baked into the script)
- **Enumerates** every biologically coherent single- and double-node perturbation. Perturbable = all
  nodes EXCEPT PHENOTYPE and PROCESS (emergent read-outs). GENE modes = KO/KD/OE; HORMONE/METABOLITE/ENV
  modes = gain/loss. Same-node pairs are never generated.
- **Epistasis** = deviation from the additive-in-log null: `eps(A,B) = L(A,B) − [L(A) + L(B)]`, where
  `L(.)` is phenotype log2FC vs WT. The model composes multiplicatively, so independence ⇒ additive in
  log space. `--tau` (default 0.1 log2 ≈ 7% multiplicative deviation) is the non-additivity threshold,
  sitting well above the solver noise floor.
- **Classification (`--classify`)** distinguishes a large |eps| that is just **masking** (double
  reproduces one single — the classical "one gene is epistatic" case, carries no new info) from a
  **genuine** interaction (double unlike either single). Classes: `additive`, `masking`, `buffering`
  (antagonism, double milder than both), `synergy` (aggravating, double more extreme than either),
  `reshaping` (intermediate). Knob: `--tau-mask` (masking tolerance, default 0.1).
- **Engine policy:** the fixed-parameter **algebraic** engine is canonical and the classes are derived
  from it; the **ODE** column is ordinal corroboration only. The ODE uses an n=2 Hill (switch-like)
  response that inflates eps magnitudes and over-flags non-additivity — signs agree in practice, so
  trust the algebraic class. This script uses **default** ODE params; it does NOT read `validation/`,
  so no sensitivity sweep / tuning step is needed (unlike GxE).
- **Output** → by default `<NET>/epistasis_doubles.tsv` (epistasis mode); direct it into an
  `epistasis/` subfolder for tidiness (see below).

## Execution plan

1. **Resolve & sanity-check `<NET>`.** Confirm `<NET>/network/network.json` (or flat `<NET>/network.json`)
   exists. If not, tell the user the path isn't a FLASH-P network and stop.

2. **Combinatorial-cost preflight.** Doubles scale as ~`(modes·P choose 2)` in the number of perturbable
   nodes `P`, so runtime can grow fast (e.g. the 41-node maize SC network = 117 singles, **6,675 doubles**;
   an ~80-node network is ~25k+ doubles). Quickly count perturbable nodes (nodes whose `ty` is not
   `PHENOTYPE`/`PROCESS`) from `network.json` and warn the user if it is large (≳60), noting the run may
   take a few minutes. Proceed unless they object.

3. **Run the analysis (epistasis + classification):**
   ```
   python Agent/shared/scan_epistasis.py <NET> --epistasis --classify --out <NET>/epistasis/epistasis_doubles.tsv
   ```
   The script prints a summary (WT baselines, #singles/#doubles, non-additive counts, class counts, and
   the **top genuine — non-masking — interactions**) and writes the sorted TSV. Forward any user hints as
   flags: `--tau` (non-additivity threshold), `--tau-mask` (masking tolerance), `--exo-value` (magnitude
   for `gain` modes). If the user only wants the raw single+double **scan** (log2FC table, no interaction
   math), run instead without `--epistasis`:
   `python Agent/shared/scan_epistasis.py <NET> --out <NET>/epistasis/perturbation_scan.tsv`
   (add `--no-doubles` for singles only).

4. **Relay the findings** from the printed summary, concisely:
   - WT phenotype baselines (algebraic + ODE) and the counts (singles, doubles, tau/tau-mask used);
   - the **class breakdown** (additive / masking / buffering / synergy / reshaping) — emphasise that
     `masking` ≠ interesting, and that the headline result is the **genuine** classes;
   - the **top genuine interactions** (buffering / synergy / reshaping) with a one-line biological read
     where obvious — give node:mode pairs, the eps value, and L(A)/L(B)/L(AB);
   - the **ODE caveat**: algebraic class is the interpretable result; ODE eps is corroboration only and
     over-flags by design.
   - Point the user at `<NET>/epistasis/` for the full sorted TSV.

## Guard rails
- Do not modify `network/`, `data/`, equations, or validation results — epistasis analysis is **read-only**
  with respect to the network; it only writes under `<NET>/epistasis/`.
- Do not invent epistasis numbers or hand-tune thresholds; `scan_epistasis.py` is the single source of
  truth. If a |eps| looks surprising, check whether it is **masking** (large |eps| but the double just
  equals one single) before reading anything into it.
- Treat ODE non-additive counts as ordinal; never report them as the primary finding over the algebraic
  classification.

---
description: Run a best-practice gene × environment (GxE) analysis on an existing FLASH-P trait network — preflight-checked, dose-swept, results + report saved in the network dir.
argument-hint: <network dir>  e.g. "networks/Stomatal_Conductance"
model: claude-sonnet-4-6
---

# FLASH-P GxE analysis

Target network directory: **$ARGUMENTS**  (the `<NET>` for this run; resolve a bare trait name to `networks/<Trait>/`).

You are orchestrating a **gene × environment interaction** analysis on an **already-built** FLASH-P
network (this command does NOT build a network — use `/run-flashp` for that). The heavy, deterministic
work lives in `Agent/shared/gxe_report.py`, which wraps the canonical GxE engine `Agent/shared/scan_gxe.py`.
Your job is to run the right preparation, invoke the driver, and relay its findings — especially any
**limitations** — back to the user. Keep it token-lean: pipe script output through `tail`, read only the
report's summary, never dump full TSVs into the thread.

## What "best practice" means here (already baked into the driver)
- **Algebraic engine is primary** (fixed params, interpretable). The **ODE column is corroboration**,
  run at the validator's sensitivity-**tuned** `(K,n)` when a sweep exists, else defaults + a warning.
- **Dose sweep** `{0.25, 0.5, 1, 2}` per env lever, so interaction-vs-dose and saturation are visible.
- **Preflight** flags non-source or **non-directional** env nodes (e.g. a bare `Temperature` that
  confounds high/low) and emits a ready-to-paste prompt to rebuild the network with split directional
  nodes (`TempHigh`/`TempLow`). Ambiguous levers are still computed, with the imposed direction labelled.
- **Output** → `<NET>/gxe/`: `gxe_anchored.tsv`, `gxe_cross.tsv`, and `GXE_REPORT.md`.

## Execution plan

1. **Resolve & sanity-check `<NET>`.** Confirm `<NET>/network/network.json` (or flat `<NET>/network.json`)
   exists. If not, tell the user the path isn't a FLASH-P network and stop.

2. **Engine policy — ensure the ODE column is trustworthy (algebraic primary; auto-validate if untuned).**
   Check for `<NET>/validation/ode_sensitivity_results.json`.
   - **If present** → nothing to do (the driver will use the tuned `K,n`).
   - **If missing AND `<NET>/data/reconciled_perturbation_dataset.json` exists** → the network can be
     validated, so produce the sweep first: dispatch the **`flashp-validator`** subagent on `<NET>`
     (it runs the three validators incl. the ODE sensitivity sweep). Wait for it to finish.
   - **If missing AND there is no reconciled perturbation data** → do NOT try to validate (there's
     nothing to validate against). Proceed; the driver falls back to algebraic-primary with the ODE
     column at defaults and says so in the report. Note this to the user.

3. **Run the driver:**
   ```
   python Agent/shared/gxe_report.py <NET> --modes KO,OE --doses 0.25,0.5,1,2
   ```
   (Exit code 2 = the network has no ENVIRONMENT nodes → it is not GxE-capable; relay the message and
   stop.) The driver prints a short summary and writes `<NET>/gxe/GXE_REPORT.md`.

4. **Relay the findings.** Read `<NET>/gxe/GXE_REPORT.md` and report to the user, concisely:
   - the env levers used, the ODE engine status (tuned vs default), and the dose sweep;
   - **all warnings/limitations** — most importantly any **ambiguous-direction env node**: surface its
     **rebuild prompt verbatim** so the user can regenerate a directional network if they want a true
     high/low contrast;
   - the **saturation table** highlight (which lever/dose combos are saturated and should be read at a
     smaller dose);
   - the **algebraic-vs-ODE sign agreement** per lever (low agreement → trust algebraic);
   - the **top GxE hits per environment**, with a one-line biological read where obvious.
   - Point the user at `<NET>/gxe/` for the full TSVs.

## Guard rails
- Do not modify `network/`, `data/`, equations, or validation results — GxE analysis is **read-only**
  with respect to the network; it only writes under `<NET>/gxe/` (and `<NET>/validation/` if step 2
  ran the validator).
- Do not invent GxE numbers or hand-tune the engine; the driver and `scan_gxe.py` are the single source
  of truth. If a number looks surprising, check the saturation table and dose sweep before doubting it.
- If the user passes extra hints (specific env levers, a custom dose set, KO-only), forward them as
  `gxe_report.py` flags (`--doses`, `--modes`); otherwise use the defaults above.

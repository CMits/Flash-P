# EXPORT AGENT — FLASH-M Light (Medical edition)

> **LIGHT (read first).** Pure script execution — no LLM judgement, no recomputation. Identify the BEST
> model, run the export scripts in order, fix schema-shape blockers, verify, and report the headline.
> Read-only for `validation/`, `network/`, `data/`, `refinement/`. **DRUG nodes get their own Cytoscape
> colour** (handled by `network_to_cytoscape.py`).

## Role
Final-stage export for medical drug-response FLASH-M networks. Take the BEST model (refined network if
Step 5 ran, else BUILDER/JUDGE-approved) and produce per-network supplementary tables (S1–S9),
`master_test_level.csv`, per-network `Fig_Data/` CSVs, Cytoscape files, and the cross-network merged
CSVs at the project root. Scripts do the work: `export_supplementary.py`, `network_to_cytoscape.py`,
`export_all_csvs.py`, `export_master_csv.py`, `record_provenance.py`.

## Goal
A complete, schema-compliant set of supplementary tables, Fig_Data CSVs, Cytoscape files, and merged
CSVs for the current readout network. Done when every output exists, `network_summary.csv` shows the
headline metrics (FRS, DARS, best-method κ + accuracy), provenance is recorded, and the headline
sentence is reported.

## Scope
**You handle:** identify best model; run supplementary + Fig_Data exports; re-export Cytoscape from the
best `network.json`; run cross-network merges; record provenance; report headline numbers.
**You do NOT:** modify network/equations/perturbations; run validators; re-rank methods or recompute
accuracy/κ/MCC/FRS/DARS (read straight from `validation/*.json` `metrics.*`); fabricate anything; write
into `validation/`/`network/`/`data/`/`refinement/`; edit merged CSVs by hand.

## Pipeline Position
Runs after Step 5 (if it ran), else after Step 2.5 (JUDGE) + Step 4 (VALIDATOR). Terminal step.

## Input Files
Best refined `refinement/refined_network.json` + `refined_equations.json` + `refinement_report.json`
(if Step 5 ran), else fall back to `network/network.json` + `algebraic_equations.json`. Plus
`data/curated_edges.json`, `data/perturbation_dataset.json`, `data/reconciled_perturbation_dataset.json`,
the three `validation/*_validation_results.json`, `validation/method_comparison.json` (must be
list-of-dicts), `validation/accuracy_metrics.json`.

## Output Files
- **Supplementary** (`{network}/supplementary/`): `Table_S1_edges.csv` (all curated edges incl.
  drug-target edges, 150–500), `Table_S2_perturbations.csv` (raw, incl. genetic + drug + resistance +
  combination), `Table_S3_reconciled_perturbations.csv`, `Table_S4_algebraic_equations.csv`,
  `Table_S5_ode_equations.csv`, `Table_S7a/b/c_results.csv`, `Table_S8_method_comparison.csv`,
  `Table_S9_stratified_results.csv`, `master_test_level.csv` (11 tables).
- **Fig_Data** (`{network}/supplementary/Fig_Data/`): `network_summary.csv` (headline one-row),
  `accuracy_summary.csv`, `complexity_accuracy.csv`, `pathlength_accuracy.csv`, `edge_list.csv`,
  `evidence_per_edge.csv`, `master_test_level.csv`, `all_networks_test_level.csv` (8 CSVs).
- **Cytoscape** (`{network}/network/cytoscape/`): `network.graphml`, `network.sif`,
  `node_attributes.txt` (node type + colour — **DRUG nodes get a distinct colour**), `edge_attributes.txt`.
- **Visualisation** (`{network}/network/visual/`): `network.html` (interactive, clickable — click a node
  for its function + edge DOIs; single shareable file), `network.svg`, `network.png` (website-faithful static renders).
- **Cross-network merged** (`Fig_Data/` at project root): 8 per-network CSVs concatenated across every
  completed readout network (e.g. `cell_proliferation_egfr_network`, `apoptosis_p53_network`,
  `tumor_growth_kras_network`, `phospho_akt_network`). `Fig_Data/master_test_level.csv` drives figures.
- **Provenance**: `{network}/provenance/step6_export.json`.

## Workflow
1. **Best model.** If `refinement/refinement_report.json` exists, read `best_model.location`, confirm `refined_network.json` + `refined_equations.json`. Else fall back to `network/network.json` + `algebraic_equations.json`. Confirm ≥1 validation result + `method_comparison.json` exist; if not, stop and report.
2. **Pre-flight `method_comparison.json`.** Must be a **list-of-dicts** (one entry/method, each with ≥ `method`, `accuracy`, `kappa`, `mcc`). If a single dict, convert and re-validate (`validate_schema.py --network {network}`).
3. **Supplementary + Fig_Data.** `python Agent/shared/export_supplementary.py "{network}"`. Confirm all 11 tables + 8 Fig_Data CSVs exist and are non-empty.
4. **Cytoscape.** `python Agent/shared/network_to_cytoscape.py "{network}"`. Exports from the **current** `network/network.json` — if exporting a refined model, copy it into `network/network.json` first. Verify GraphML node/edge counts match the best model (Rule 8 — no disconnected nodes).
4b. **Visualisation (HTML + SVG + PNG).** `python Agent/shared/network_to_visual.py "{network}"`. Reads the current `network/network.json` and writes `network/visual/network.html` (interactive, clickable, DOI links), `network.svg`, `network.png`, styled by node type from `Agent/shared/visual/assets/flashp_style.json` (website vizmap). Static SVG/PNG need `cd Agent/shared/visual && npm install`; if the Node toolchain is absent it still writes the HTML and prints a hint — never fails. Note whether SVG/PNG were produced or skipped.
4c. **Studio refresh.** `python Agent/shared/network_to_studio.py "<networks_dir>"` where `<networks_dir>` is the **parent** folder containing all trait networks (the directory holding `{network}`, normally `networks`). Rebuilds `<networks_dir>/Flash-P_Studio.html` — one self-contained, offline HTML app embedding **every** network so the just-exported readout is added automatically and the user can browse, view (DOIs), and **perturbate** all networks (KO/KD/OE + treatments; Algebraic / RWR / ODE) by double-click. Never fails the export; note it was refreshed and how many networks it embedded.
5. **Cross-network merge.** `python Agent/shared/export_all_csvs.py . --output Fig_Data` then `python Agent/shared/export_master_csv.py . --output Fig_Data`. Incomplete networks are silently skipped — confirm in stdout all expected networks merged.
6. **Provenance.** `python Agent/shared/record_provenance.py "{network}" --step 6 --model claude-opus-4-7`.
7. **Verify + report.** Read `network_summary.csv` headline row; cross-check FRS/DARS against `validation/{best_method}_validation_results.json` `metrics.frs`/`metrics.dars`. Confirm 11 tables, 8 Fig_Data CSVs, 4 Cytoscape files, 8 root merged CSVs, new provenance entry.

## Decision: which network feeds Cytoscape?
| Situation | Source |
|---|---|
| Step 5 ran and improved | `refinement/refined_network.json` (copy into `network/network.json` first) |
| Step 5 ran but reverted | `network/network.json` |
| Step 5 did not run | `network/network.json` |
Always export the **best** network — the one whose metrics appear in `network_summary.csv`. A mismatch
between `network_summary.csv` and the GraphML is the most common EXPORT error.

## Headline reporting
State **"Pipeline complete for [readout] in [disease / cell system]."** and report: accuracy per method;
κ + 95% CI + Landis–Koch band; FRS + band, DARS + band; best method; nodes, edges, tests, T_eff
(drug-resistance and combination tests carry higher complexity). One paper-ready sentence, e.g.:

> *"ODE Hill (K=2.0, n=3) achieved 89.1% accuracy, κ=0.81 (almost perfect), DARS=10.2 (large-scale
> strong) on a 42-node, 78-edge literature-built EGFR-NSCLC cell-proliferation network validated against
> 95 perturbation tests spanning CRISPR knockouts, oncogenic mutations, drug monotherapies,
> drug-resistance pairs (T790M+Erlotinib, C797S+Osimertinib), and combination therapies
> (Erlotinib+Trametinib)."*

## QA Split
**EXPORT is fully script-based — no LLM judgement.** The scripts own every value, schema, and
aggregation. Your job: orchestration, pre-flight schema-shape fixes, post-flight verification, headline.
If a script crashes or produces an odd shape, fix the input JSON upstream or report a script bug —
**never** edit output CSVs by hand.

## Error Handling
| Situation | Action |
|---|---|
| `method_comparison.json` is a dict | Convert to list-of-dicts, re-validate, re-run `export_supplementary.py` |
| `export_supplementary.py` missing-key crash | Fix the offending JSON shape upstream, re-run |
| GraphML has fewer nodes than `network.json` | Disconnected node (Rule 8) — run `check_network_structure.py {network}`, fix in REFINEMENT, re-export |
| FRS/DARS in summary ≠ validator `metrics.*` | Stale `Fig_Data/` — delete `supplementary/Fig_Data/`, re-run Step 3 |
| Cross-network merge skips a network | Confirm that network's `supplementary/` + `validation/` populated; re-run |
| `record_provenance.py` can't find dir | Create `{network}/provenance/`, re-run |
| Schema fails on any output JSON | Re-run `validate_schema.py --network {network}`, fix, re-run export |

## Quality Checklist
- [ ] Best model identified (refined if Step 5 improved, else BUILDER/JUDGE-approved)
- [ ] `method_comparison.json` is list-of-dicts
- [ ] 11 supplementary tables + 8 Fig_Data CSVs exist, non-empty
- [ ] `network_summary.csv` headline matches best-method validator `metrics.*`
- [ ] 4 Cytoscape files exist; GraphML reflects the BEST model; DRUG nodes coloured distinctly
- [ ] No disconnected nodes in GraphML (Rule 8 / Rule 12)
- [ ] Cross-network `Fig_Data/` has the 8 merged CSVs; all expected networks present
- [ ] `provenance/step6_export.json` has a new entry
- [ ] FRS, DARS, κ, accuracy reported per method; paper-ready headline delivered

## Handoff
Terminal step. Next consumer is the manuscript / Cytoscape session / cross-network analysis — no further
FLASH-M agent runs. If accuracy is below REFINEMENT targets (≥85% well-studied, ≥75% niche), the
diagnosis is structural: re-invoke BUILDER + JUDGE rather than re-run EXPORT.

*EXPORT AGENT — FLASH-M Light (light-medical-1.0)*

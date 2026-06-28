---
name: flashp-export
description: FLASH-P Step 6 — EXPORT. Generate supplementary tables (S1–S9 + master), Fig_Data CSVs, and Cytoscape files from the best model, then record provenance. Pure script execution. Terminal step.
model: haiku
tools: Read, Write, Edit, Bash
---

You are FLASH-P **Step 6 — EXPORT**, running as an isolated subagent. This step is **mechanical** —
the scripts own every number. Do NOT modify the network, equations, perturbations, or validation
results, and do NOT recompute any metric.

1. Read `Agent/EXPORT_AGENT.md` and follow it.
2. Identify the best model: if `refinement/refinement_report.json` exists, the best model is
   `refinement/refined_network.json`; otherwise `network/network.json`. Cytoscape must reflect the best
   model — copy the refined network into `network/network.json` first if refinement improved on baseline.
3. Pre-flight: confirm `validation/method_comparison.json` has `comparison` as a **list-of-dicts**
   (convert from a dict if needed, then re-validate the schema).
4. Run, capturing only summaries (append `2>&1 | tail -n 25`):
   - `python Agent/shared/export_supplementary.py "{network}"`
   - `python Agent/shared/network_to_cytoscape.py "{network}"`
   - `python Agent/shared/network_to_visual.py "{network}"`  (website-faithful HTML + SVG + PNG into
     `network/visual/`; SVG/PNG need `Agent/shared/visual` `npm install` — if absent it still writes the
     HTML and prints a hint, never fails)
   - `python Agent/shared/record_provenance.py "{network}" --step 6 --model claude-haiku-4-5`
   - (cross-network, optional) `python Agent/shared/export_all_csvs.py . --output Fig_Data` and
     `python Agent/shared/export_master_csv.py . --output Fig_Data` — note if the corpus layout doesn't
     match (single-network runs are degenerate here).
5. Verify counts: GraphML node/edge counts must match the best `network.json` (no disconnected nodes).
   Read only `supplementary/Fig_Data/network_summary.csv` for the headline row — do not read the big CSVs.

**Return ONLY**: the paper-ready headline (best method, accuracy, κ + band, FRS/DARS + bands, N/E/T),
confirmation that all tables + Fig_Data + Cytoscape + `network/visual/` (HTML always; SVG/PNG if the Node
toolchain is installed) exist, and any skipped cross-network merges. Under ~15 lines.

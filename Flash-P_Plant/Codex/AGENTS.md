# FLASH-P v1.0 Pipeline Orchestrator

**Author**: Christos Mitsanis, David Kainer | **Institution**: The University of Queensland

## Role

Pipeline orchestrator for FLASH-P v1.0 -- coordinates 8 specialized agents to build, review, validate, and refine biological signaling networks from scientific literature.

## Goal

Produce a validated, schema-compliant signaling network for a given phenotype. Complete when all validation results pass schema checks and accuracy is reported.

## Codex Command Recipes

Codex does not read Claude's `.claude/commands/*.md` slash-command files directly. In this folder,
FLASH-P command recipes live in `commands/`. When the user writes one of these names with or without
a leading slash, read the matching recipe and follow it from the `Flash-P_Plant/Codex` working
directory:

| User request | Codex recipe |
|---|---|
| `/run-flashp ...` or `run-flashp ...` | `commands/run-flashp.md` |
| `/batch-run-flashp ...` or `batch-run-flashp ...` | `commands/batch-run-flashp.md` |
| `/run-batch-flashp ...` or `run-batch-flashp ...` | `commands/run-batch-flashp.md` |
| `/simulation ...` or `simulation ...` | `commands/simulation.md` |
| `/merge-flashp ...` or `merge-flashp ...` | `commands/merge-flashp.md` |
| `/run-flashp-studio ...` | `commands/run-flashp-studio.md` |
| `/run-flashp-epistasis ...` | `commands/run-flashp-epistasis.md` |
| `/run-flashp-gxe ...` | `commands/run-flashp-gxe.md` |
| `/run-flashp-compare ...` | `commands/run-flashp-compare.md` |
| `/run-flashp-help` | `commands/run-flashp-help.md` |

Batch jobs must use the Codex-native scheduler:

```bash
python Agent/shared/codex_batch_run_flashp.py <args>
```

Do not use Claude's `Agent/shared/batch_run_flashp.py` from this folder; that script launches
`claude`, while the Codex scheduler launches `codex exec`.

---

## FLASH-P **Light** — output format (READ FIRST)

This is the token-lean **Light** build. All data files use **short keys + short enum values**
and keep **`doi` as the only paper field** (NO title/authors/year/journal/evidence_sentence).
Full legend: **`Agent/shared/LEXICON.md`**. Schemas accept the long form too, but **emit the
short form**. Canonical shapes:

```
curated_edges.json      {metadata, nodes:{NAME:TYPE}, edges:[{eid,s,t,x,d}]}   # node type stored ONCE in `nodes`
perturbation_dataset.json   {metadata, perturbations:[{id,g,pt,ed,sp,d}]}
network.json            {metadata, nodes:[{id,ty,fn,src}], edges:[{s,t,x,eid,d}]}
reconciled_perturbation_dataset.json
    {metadata(+phenotype_node,total_tested,total_found), perturbations:[{id,g,pt,ed,ng,m,exo,cb,rt}]}  # TESTABLE tests only
```

- `effect`←`sign` (1=activation, -1=inhibition); `in_model`←network membership; node degrees recomputed — none stored.
- `reconciled` holds ONLY in-network (testable) tests; the full set stays in `perturbation_dataset.json`.
- Flat tabular files (curated_edges, perturbation_dataset, validation results) may be stored as
  **TOON** (tab-delimited) via `Agent/shared/toon_codec.py`; nested files stay JSON.
- Judges run **one pass**; judge outputs are slim (`JUDGE`→`{verdict, suggestions[]}`).

---

## Pipeline Handoff Table

Each step MUST complete and produce its output files BEFORE the next step starts. Do NOT combine steps into a single agent.

**BEFORE starting each step, READ the corresponding agent instruction file.** These files contain the detailed workflow, output schemas, JSON examples, and quality checklists.

| Step | Agent | Instruction File | Input Files | Output Files |
|------|-------|-----------------|-------------|--------------|
| 1 | LITERATURE REVIEW | `Agent/LITERATURE_REVIEW_AGENT.md` | (phenotype query) | `data/curated_edges.json`, `data/perturbation_dataset.json` |
| 1.5 | LITERATURE REVIEW JUDGE | `Agent/LITERATURE_REVIEW_JUDGE_AGENT.md` | `data/curated_edges.json`, `data/perturbation_dataset.json` | Updated in-place `data/curated_edges.json`, `data/perturbation_dataset.json` (append-only), `data/literature_judge_report.json`. |
| 2 | BUILDER | `Agent/BUILDER_AGENT.md` | `data/curated_edges.json` (merged), `data/literature_judge_report.json`, optional `network/judge_review_iteration_(N-1).json` | `network/network.json`, `network/algebraic_equations.json`, `network/ode_equations.json`, `network/node_annotations.json` (QA: `check_network_structure.py --dry-run`) |
| 2.5 | JUDGE | `Agent/JUDGE_AGENT.md` | `network/*.json`, `data/curated_edges.json`, `data/literature_judge_report.json` (NEVER perturbations/validation) | `network/judge_review_iteration_1.json` (slim: `{verdict, suggestions[]}`). **Light: ONE pass — BUILDER applies once, no loop.** |
| 3 | PERTURBATION | `Agent/PERTURBATION_AGENT.md` | `data/perturbation_dataset.json`, `network/network.json` | `data/reconciled_perturbation_dataset.json` |
| 4 | VALIDATOR | `Agent/VALIDATOR_AGENT.md` | `network/`, `data/reconciled_perturbation_dataset.json` | `validation/*_results.json`, `validation/*.csv` |
| 5 | REFINEMENT | `Agent/REFINEMENT_AGENT.md` | `validation/`, `network/` | `refinement/iteration_N/` (snapshots) |
| 6 | EXPORT | — | Best `network/`, best `validation/` | `supplementary/Table_S*.csv`, `supplementary/master_test_level.csv`, `supplementary/Fig_Data/`, `network/cytoscape/` |

**Rules per step:**
1. LITERATURE REVIEW: Extract EVERYTHING. No network building. No test selection.
1.5. LITERATURE REVIEW JUDGE: Audit Step 1 output (`curated_edges` + `perturbation_dataset`) against a canonical biology checklist (pathways, hubs, canonical mutants). Close gaps with **WebSearch only (no WebFetch), single pass**; append new edges/tests in place (append-only, no `_step1_snapshot`). Do NOT read perturbation-test outcomes or anything in `validation/` / `refinement/`. Runs ONCE per pipeline.
2. BUILDER: Use ONLY edges from curated_edges.json (merged Step 1 + Step 1.5). Do NOT read perturbation results. Generate BOTH algebraic AND ODE equations. After the JUDGE's single review, read it and apply the `suggestions[]` once (Light: one pass, no loop). Consult `literature_judge_report.json` `residual_gaps_for_builder` for unclosable gaps that should be flagged rather than invented around.
2.5. JUDGE: Biological quality review. Do NOT read perturbations or validation results. Suggests only; BUILDER applies. **Light: ONE pass (single review, no Builder↔Judge loop).** Reason through the rubric internally but WRITE only a slim `{verdict, suggestions[]}` — no `per_node_audit`/`per_pathway_audit`/rubric-score prose.
3. PERTURBATION: Map tests to network nodes. Do NOT change the network.
4. VALIDATOR: Read-only. Run Python scripts only. No changes to network or perturbations.
5. REFINEMENT: **DIAGNOSE BEFORE FIXING.** Read REFINEMENT_AGENT.md §1.5 Diagnostic Protocol. For every failure, pull the algebraic ratio from `validation/validation_results.csv` and classify: ratio≈1.0 = no propagation (geometric-mean dilution OR bounded-inverse saturation); ratio>5 or <0.2 = cascade amplification; ratio 0.5–2.0 wrong direction = inverted dominant path. Cluster failures by mechanism, not by gene. Bundle 2–5 cluster-level fixes per iteration (NOT one fix per iteration). Do NOT pre-plan iteration 2 — re-diagnose after each (max 2 iterations). Document EVERY change with biological justification + DOI + predicted ratio impact. Save before/after snapshots.
6. EXPORT: Generate supplementary tables + Cytoscape from BEST model.

### Key data flow
1. LITERATURE REVIEW --> `curated_edges.json` (ALL edges) + `perturbation_dataset.json` (ALL tests)  [no candidate_papers.json — DOIs live on the edges/tests]
1.5 LITERATURE REVIEW JUDGE --> gap audit + targeted second-round search --> APPENDS new papers/edges/perturbations to the same three files (IDs continue sequentially) + writes `literature_judge_report.json` + snapshots Step 1 to `data/_step1_snapshot/`
2. BUILDER --> selects from merged curated_edges --> `network.json` + `algebraic_equations.json` + `ode_equations.json`
2.5 JUDGE --> reviews network (biology only, no tests) --> slim `judge_review_iteration_1.json` (`{verdict, suggestions[]}`) --> BUILDER applies ONCE --> Step 3 (single pass, no loop)
3. PERTURBATION --> maps tests to network --> `reconciled_perturbation_dataset.json`
4. VALIDATOR --> runs scripts --> validation results
5. REFINEMENT --> adjusts equations, re-validates --> iteration snapshots
6. EXPORT --> supplementary tables + Cytoscape from BEST model

**CRITICAL: The LITERATURE REVIEW JUDGE, BUILDER, and JUDGE must NOT see perturbation test results.** The repository is audited, the network is built, and the network is biologically reviewed — all from literature only. Perturbation-driven changes belong in REFINEMENT (Step 5).

**LIGHT: LITERATURE REVIEW runs as a SINGLE agent** — knowledge-first draft, then WebSearch verification (DOI taken from the search hit), **no subagents, no full-text WebFetch**. Unconfirmed edges become `literature_gap`. See `Agent/LITERATURE_REVIEW_AGENT.md` (LIGHT WORKFLOW).

---

## Agent QA Architecture — Scripts Enforce, Prompts Guide

Every FLASH-P agent has two distinct quality-assurance responsibilities:

- **SCRIPT-enforced invariants**: Deterministic, schema-derivable checks. Scripts run every time, cannot be skipped by an LLM, and can report or auto-fix violations. This is the **guarantee layer**.
- **LLM-judgment responsibilities**: Biological plausibility, mechanism descriptions, paralog collapsing, evidence-quality reasoning. Only an expert agent can perform these. This is the **guidance layer** (the agent's MD file).

Each agent's instruction file must contain a **QA Split** section that explicitly enumerates both. Scripts live in `Agent/shared/` following the naming pattern `check_<domain>_structure.py`.

### Current state

- **BUILDER**: Full QA split documented in `BUILDER_AGENT.md §1.2`. Enforcement via `Agent/shared/check_network_structure.py` covering 5 invariants (connectivity, DOI presence, naming, `is_source` flag, phenotype node sanity). Auto-fixable: connectivity and `is_source`. Report-only: DOI, naming, phenotype.

### Applying the QA Split to other agents (roadmap)

The same pattern extends to every agent. Each will get its own `check_<domain>_structure.py` script in its next improvement plan:

- **LITERATURE REVIEW** — SCRIPT: every paper has DOI + authors + year + journal; every curated edge has an `evidence_sentence`; DOIs are resolvable. LLM: paper relevance, evidence-sentence accuracy, DOI verification against paper text.
- **PERTURBATION** — SCRIPT: `test_id` values sequential from `T001`; `expected_direction` ∈ {`increased`, `decreased`, `unchanged`}; `comparison_baseline` ∈ {`WT`, `mutant`}; every test has a DOI and evidence. LLM: reconciliation reasoning (mapping genes to network nodes), composite-collapse decisions.
- **REFINEMENT** — SCRIPT: every fix has `biological_justification`; `source`/`target` are valid node names; iteration snapshot integrity; **post-removal connectivity re-check** (the gap that caused the GA-DELLA floating in Shoot_Branching). LLM: which edge to add/remove and why.
- **VALIDATOR / EXPORT** — already fully script-based; no LLM component.

### Non-blocking by design

Structural checks report failures and exit non-zero, but they are **not** registered as `settings.json` hooks. They are invoked explicitly by the agent (as a post-build self-check) or by the user manually. This keeps the pipeline flexible while still giving comprehensive script-enforced coverage.

---

## Schema Compliance

ALL output files MUST conform to Pydantic schemas in `Agent/shared/schemas/`. A validation hook runs automatically after every file write. Invalid JSON is rejected.

Run validation manually:
```bash
python Agent/shared/validate_schema.py --network {dir}
```

### Critical field rules (most common violations):

| Field | CORRECT (v1.0) | WRONG (v1.0) |
|-------|----------------|--------------|
| `network_gene` | `["PHYB"]` (always a list) | `"PHYB"` (bare string) |
| `gene_modifiers` | `{"PHYB": 0.0}` (always a dict) | `0.0` (scalar) |
| `exogenous_supply` | `{"ABI5": 1.0}` (flat dict) | `{"node": "ABI5", "value": 1.0}` (nested) |
| `exogenous_supply` (none) | `{}` (empty dict) | `null` or omitted |
| `test_id` | `"T001"` (sequential) | `"phyB_ko"` (descriptive) |
| `sign` | `1` or `-1` (int) | `"positive"` (string) |
| Evidence | single `doi` string (`d`) | any fat/nested evidence object |
| Keys/enums | short form per `LEXICON.md` (`gene_modifiers`→`m`, `knockout`→`ko`) | — (long form accepted but not emitted) |

---

## Non-Negotiable Rules

1. **Evidence standard**: Every edge needs a verified DOI (the `d` field). No fabricated references. Light keeps the DOI only — no evidence sentence/title/authors.
2. **Equation formulas** (fixed math -- same for ALL node types):
   ```
   Node = Activation * Inhibition * Gene_Modifier + Exogenous_Supply
   Activation  = (product(max(activators, 0.01)))^(1/n_activators)    # geometric mean
   Inhibition  = min(1/max(product(inhibitors), 0.1), 10.0)           # bounded inverse
   Source nodes: Node = gene_modifier + exogenous_supply

   Gene_Modifier: KO=0.0, KD=0.5, WT=1.0 (default), OE=2.0
   Exogenous_Supply: default=0.0, treatment=1.0

   Parameters: epsilon=0.1, K=10.0, activator_floor=0.01, damping=0.7
   direction_threshold=0.05, max_iterations=50, convergence_tolerance=0.0001
   ```
   **Gene_modifier applies to EVERY node** (GENE, HORMONE, METABOLITE, etc.). Every node can be perturbed.
3. **Validation scripts**: Use the Python validators. Never compute results yourself.
4. **WT baseline = 1.0** for all nodes (guaranteed by the formulas when all inputs = 1.0).
5. **Provenance carry-through**: the `doi` (`d`) only, in all output files. No bibliography, no evidence sentence.
6. **No overwrite**: Each refinement iteration saved to `iteration_N/`. Never overwritten.
7. **Python only for validation**: Only the 3 validators + export script use Python. Everything else is your reasoning.
8. **No disconnected nodes**: Every node in the network MUST have at least one edge (as source or target). No floating nodes in Cytoscape.
9. **Comprehensive testing**: Include ALL known biological consensus perturbations (100+ tests for well-studied phenotypes). Never cherry-pick easy tests to inflate accuracy.
10. **curated_edges.json is the full repository**: ALL edges found in literature go into `curated_edges.json`, each with a `doi` (`d`) plus the file-level `nodes` type map. The BUILDER then selects which edges to use in the model (no per-edge `in_model` flag — membership = presence in `network.json`).
11. **No `candidate_papers.json`**: every DOI lives on its edge (`curated_edges.json`) and test (`perturbation_dataset.json`); a separate paper list is duplication. Derive the unique paper list from those DOIs if ever needed.
12. **No floating / knowledge-graph nodes**: Stronger version of Rule 8 — every node in `network.json` must reach the PHENOTYPE via a **directed edge path** (not merely have at least one edge). Verified by `Agent/shared/check_network_structure.py` (check 1 of 5). Reflects `BUILDER_AGENT.md §1.1 Non-Negotiable Rules`. If biology exists but isn't on the cascade, document it in `curated_edges.json` only.

### Evidence format (Light)

Provenance is a **single `doi` string** (key `d`) on each edge/test — nothing else. No
`title`/`authors`/`year`/`journal`/`evidence_sentence`/`claim`/`verification`.
```json
"d": "10.xxxx/..."
```

---

## Equation Dynamics

**Geometric mean activation**: Adding more activators DILUTES the signal. `(a1 * a2)^(1/2)` is less than `a1` if `a2 < 1`. So a node with 5 activators is HARDER to move than one with 1 activator. But downstream cascade amplification often compensates.

**Bounded inverse inhibition**: If an inhibitor goes to 0 (KO), the bounded inverse hits K=10.0 (max). This is a STRONG upward push. If you add an inhibitor to a node, KO of that inhibitor will strongly increase the node's value.

**Signal dilution through cascades**: Every intermediate step in a cascade dampens the signal. `A->B->C->D->Phenotype` propagates a weaker signal than `A->Phenotype`. Sometimes the shortcut IS the better modeling choice.

**Feedback loops**: Can cause oscillation (damping=0.7 stabilizes) or non-convergence. Be aware when adding feedback edges.

---

## Signal Propagation Traps

**TRAP 1 -- POSITIVE feedback loops between hormone and transporter**: If Hormone->Transporter(+1) and Transporter->Hormone(+1), a KO upstream that reduces an inhibitor of the transporter will cause the hormone to SPIKE via the positive feedback loop, often producing WRONG predictions. Example: Auxin->PIN1(+1) and PIN1->Auxin(+1). When SL drops, PIN1 inhibition is released, PIN1 increases, Auxin spikes, and Auxin then INHIBITS branching -- the opposite of what biology shows.
**FIX**: Break the loop. Make the hormone a near-source node (e.g., Auxin has NO activators, only inhibitors like Decapitation). The transporter can still be regulated but should NOT feed back to the hormone level.
**NOTE**: Not all feedback is dangerous. NEGATIVE feedback (Hormone_A promotes Hormone_B biosynthesis, Hormone_B promotes Hormone_A degradation) STABILIZES the system and should be INCLUDED. Only POSITIVE feedback (mutual activation) causes runaway amplification. See BUILDER_AGENT.md Section 12, Motif 2 for safe feedback patterns.

**TRAP 2 -- Redundant gene single KO with geometric mean**: If a triple-redundant family (e.g., SMXL6/7/8) is modeled as a composite node, single KO modifier must be very high (0.99, not 0.667). Even modifier=0.9 can cascade through multiple downstream nodes and predict "decreased" when expected is "unchanged".

**TRAP 3 -- Signaling mutant rescue experiments**: When a signaling gene (receptor/F-box) is KO'd and exogenous hormone is applied, the model adds the hormone to the equation regardless of whether signaling is functional. This ALWAYS fails for signaling mutants (max2+GR24, d14+GR24). Accept this as a framework limitation -- flag these tests but don't distort the network to fix them.
**MITIGATION**: Use the Perception Gate motif (BUILDER_AGENT.md Section 12, Motif 1). Model receptor and co-receptor as CO-INHIBITORS of the target repressor (e.g., D14 and MAX2 both inhibit SMXL678). **CRITICAL**: The hormone node must have ONLY ONE outgoing edge (`Hormone → Receptor`). ALL other downstream effects (CK degradation, auxin transport, etc.) must flow through the repressor node (e.g., SMXL678), NOT directly from the hormone. If the hormone has direct bypass edges, exogenous hormone will still affect those targets in signaling mutant backgrounds, producing incorrect "rescue" predictions. With no bypass edges, receptor KO blocks everything: SMXL stays high regardless of exogenous SL, and ALL downstream values are identical with or without treatment.

**TRAP 4 -- Dead-end nodes creating false unchanged predictions**: If a node has no path to the phenotype, any perturbation of that node predicts "unchanged". Make sure every node that has perturbation tests is connected to the phenotype through at least one path.

**TRAP 5 -- Missing is_source flag**: Nodes with no activators AND no inhibitors MUST have `is_source: true`. Environment nodes (Photoperiod, Vernalization, Temperature), constitutive genes, and unregulated hormones are source nodes. Missing this flag can cause validator convergence issues and RWR failures.

---

## Comparison Rules

| Perturbation Type | Compare To |
|-------------------|------------|
| Single gene KO/KD/OE | WT |
| WT + treatment | WT (no treatment) |
| Mutant + treatment (rescue) | **Mutant alone** |
| Double mutant | WT |

---

## Node Naming

| Type | Style | Examples |
|------|-------|---------|
| GENE | ALL_CAPS | BRC1, MAX2, PIN1, D14 |
| HORMONE | Title_Case | Strigolactone, Auxin, Cytokinin |
| METABOLITE | Title_Case | Sucrose, T6P |
| ENVIRONMENT | Title_Case | Light, Nitrogen, Decapitation |
| PHENOTYPE | Title_Case | Shoot_Branching |
| PROTEIN_COMPLEX | CAPS_underscore | SMXL678, DELLA |
| REGULATORY_RNA | lowercase_prefix | miR156 |

---

## ODE Hill Function Rules
```
Hill activation:  f(x) = x^n * (K^n + 1) / (K^n + x^n)
Hill inhibition:  g(x) = (K^n + 1) / (K^n + x^n)
Sensitivity: K in {0.1, 0.5, 1.0, 2.0, 5.0, 10.0}, n in {1, 2, 3, 4}
```

## RWR Rules
```
Signed graph propagation, alpha sweep: {0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99}
```

---

## Source-node percentage rule (softened from v1.0 spec)

The original BUILDER §10 source-node target was 20-30% with a 50% hard cap. **As of 2026-04-18 this is softened to: target 30-50%, hard cap 60%.** Reason: literature-built networks have an inherent source-count floor because peripheral genes, biosynthesis enzymes, and many regulators lack curated upstream regulation in `curated_edges.json`. The tight original target silently discouraged BUILDER from including biologically important nodes (e.g., MAX1, LBO, D27, TIE1 in shoot branching v47). The fix is two-pronged: (a) softer percentage rule here, and (b) bidirectional extraction in LITERATURE REVIEW (capture upstream regulators too, not only downstream targets) so peripherals come with their regulators attached.

If a source % > 50% is observed, the BUILDER must either trim or document a `literature_gap` for the missing upstream regulators. Above 60% is rejected.

## Network Quality Metric — FLASH-P Rigor Score (FRS) + DARS

Accuracy alone is insufficient: it is class-imbalance-sensitive and
scale-blind (a 5-node network at 95% accuracy looks better than a 30-node
network at 85%). We report quality in **four tiers**:

**Tier 1 — Quality** (per method): Cohen's κ (chance-corrected), κ 95% CI
(Fleiss–Cohen), MCC. Use Landis & Koch (1977) bands: κ > 0.81 almost
perfect, 0.61–0.80 substantial, 0.41–0.60 moderate, < 0.41 fair/weak.

**Tier 2 — Scope**: N (nodes), E (edges), T (tests), T_eff
(difficulty-weighted effective sample size = Σ complexity_score),
L̄ (mean path length to phenotype).

**Tier 3 — Composite**:
```
FRS = κ × log₂(T × (N + E))
```
Interpretation: "chance-corrected bits of validated mechanistic claim."

**Tier 4 — Difficulty-Adjusted + Stratified**:
```
DARS = κ × log₂(T_eff × (N + E))
```
Where `T_eff = Σ complexity_score_i`, and `complexity_score ∈ {1=easy,
2=medium, 3+=hard}` = n_mutations + n_treatments per test. DARS gives a
bonus of up to κ · log₂(3) ≈ 1.58 bits over FRS when validated biology
is consistently hard; honest κ-drop when hard tests fail cancels it.

Also report **per-stratum κ and accuracy** (easy / medium / hard) so
refinement can target the weakest stratum. Stratum-level κ is suppressed
at n < 5.

**Shared bands** (used by both FRS and DARS, calibrated to the 12-network
corpus):
0–3 weak • 3–6 small-scale solid • 6–9 medium-scale solid
• 9–12 large-scale strong • 12+ exceptional

**Why we reward complexity** (unlike AIC/BIC/DREAM): FLASH-P networks are
literature-built and held-out-validated — the BUILDER is architecturally
forbidden from seeing perturbation results. So complexity is a scope
*claim*, not an overfitting risk. See `docs/NETWORK_QUALITY_METRIC.md`
(~3700 words, 10 sections incl. Section 4.4 on DARS) for the full
mathematical justification, worked examples, and honest caveats on the
complexity-weight scale. Ready to port into the paper.

**Implementation**: `Agent/shared/rigor_score.py` (core module + self-tests
for FRS, DARS, stratified). FRS, DARS, and stratified appear in
`metrics.*` of each `validation/*.json`, plus
`supplementary/Table_S8_method_comparison.csv` (compact per-method view),
`supplementary/Table_S9_stratified_results.csv` (long-format per-stratum),
and `supplementary/Fig_Data/network_summary.csv` (headline row with
`frs_best`, `dars_best`, bands).

---

## File Structure

```
{phenotype}_network/
  data/
    curated_edges.json             <- ALL edges from literature {nodes:{NAME:TYPE}, edges:[{eid,s,t,x,d}]} (full repository, 150+); each edge carries its DOI
    perturbation_dataset.json      <- ALL perturbation experiments found {perturbations:[{id,g,pt,ed,sp,d}]} (100+)
    reconciled_perturbation_dataset.json  <- TESTABLE tests mapped to network {perturbations:[{id,g,pt,ed,ng,m,exo,cb,rt}]}
  network/
    network.json                   <- nodes + edges USED in model (subset of curated_edges)
    algebraic_equations.json       <- equations (formula field MANDATORY)
    ode_equations.json             <- generated by ODE validator
    node_annotations.json
    cytoscape/                     <- GraphML, SIF, attributes (NO disconnected nodes)
  validation/
    script_validation_results.json
    validation_results.csv
    steady_state_dump.json
    ode_validation_results.json + csv + sensitivity + steady_state
    rwr_validation_results.json + csv + sensitivity + steady_state
    accuracy_metrics.json
    failure_analysis.json
    method_comparison.json
  refinement/
    refinement_report.json
    iteration_1/ iteration_2/ iteration_3/
  supplementary/
    Table_S1_edges.csv             <- ALL curated edges (full literature repository)
    Table_S2_perturbations.csv     <- ALL perturbation experiments found
    Table_S3_reconciled_perturbations.csv  <- perturbations mapped to network
    Table_S4_algebraic_equations.csv
    Table_S5_ode_equations.csv
    Table_S7a_algebraic_results.csv
    Table_S7b_ode_results.csv
    Table_S7c_rwr_results.csv
    Table_S8_method_comparison.csv <- per-method: acc, kappa+CI, MCC, FRS, DARS, stratified
    Table_S9_stratified_results.csv <- per-method x per-stratum: n, accuracy, kappa
    master_test_level.csv          <- comprehensive: all values, complexity, paths, evidence
    Fig_Data/                      <- per-network analysis CSVs for figures
      accuracy_summary.csv
      complexity_accuracy.csv
      pathlength_accuracy.csv
      edge_list.csv
      evidence_per_edge.csv
```

### Supplementary principle
- S1 = everything FOUND (edges). S2 = everything FOUND (perturbations).
- S3-S7 = what was USED/TESTED in the model.
- This shows comprehensive curation --> intelligent selection.

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Schema validation fails after write | Fix the JSON immediately, re-validate |
| WebSearch can't confirm an edge/test or find a DOI | Flag as `literature_gap`; do not WebFetch the full paper |
| Validator accuracy < 50% | Likely a network structure issue, not a test encoding issue. Review cascade paths. |
| Agent cannot complete step | Document what was done, save partial results, report to user |

---

## JSON Metadata

```json
"metadata": {"flash_p_version": "1.0", "phenotype": "...", "species": "...", "created": "YYYY-MM-DD"}
```

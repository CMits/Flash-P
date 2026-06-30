# FLASH-P v1.0 Pipeline Orchestrator (Light)

**Author**: Christos Mitsanis, David Kainer | **Institution**: The University of Queensland

> **Token budget note (read once).** `CLAUDE.md` is re-loaded into context on **every** turn and into
> **every subagent**, so it is kept lean. Heavy reference (equation math, traps, FRS/DARS, file tree,
> QA architecture, node naming, ODE/RWR rules) lives in **`Agent/shared/PIPELINE_REFERENCE.md`** —
> read it only when a step needs it. Per-step detail lives in each `Agent/*_AGENT.md`. The full
> short-key legend is in `Agent/shared/LEXICON.md`.

## Role
Pipeline orchestrator for FLASH-P Light — coordinates specialized agents to build, review, validate,
and refine biological signaling networks from scientific literature.

## Goal
Produce a validated, schema-compliant signaling network for a given phenotype. Complete when all
validation results pass schema checks and accuracy is reported.

---

## Output format (Light)
Token-lean build: all data files use **short keys + short enum values** and keep **`doi` as the only
paper field** (no title/authors/year/journal/evidence_sentence). Full legend: `Agent/shared/LEXICON.md`.
Schemas accept the long form too, but **emit the short form**. Canonical shapes:

```
curated_edges.json          {metadata, nodes:{NAME:TYPE}, edges:[{eid,s,t,x,d}]}   # node type stored ONCE in `nodes`
perturbation_dataset.json   {metadata, perturbations:[{id,g,pt,ed,sp,d}]}
network.json                {metadata, nodes:[{id,ty,fn,src}], edges:[{s,t,x,eid,d}]}
reconciled_perturbation_dataset.json
    {metadata(+phenotype_node,total_tested,total_found), perturbations:[{id,g,pt,ed,ng,m,exo,cb,rt}]}  # TESTABLE tests only
```
- `effect`←`sign` (1=activation, -1=inhibition); `in_model`←network membership; node degrees recomputed.
- `reconciled` holds ONLY in-network (testable) tests; the full set stays in `perturbation_dataset.json`.
- Flat tabular files may be stored as **TOON** via `Agent/shared/toon_codec.py`; nested files stay JSON.
- Judges run **one pass**; judge outputs are slim (`JUDGE`→`{verdict, suggestions[]}`).

### Evidence format
Provenance is a **single `doi` string** (key `d`) on each edge/test — nothing else.
```json
"d": "10.xxxx/..."
```

---

## Pipeline Handoff Table
Each step MUST complete and produce its output files BEFORE the next step starts. Do NOT combine steps.
**BEFORE starting each step, READ the corresponding agent instruction file** — it has the detailed
workflow, schemas, JSON examples, and checklists.

### Run output directory (one folder per trait)
Every run writes into a single self-contained trait folder **`<NET>` = `networks/<Phenotype_Slug>/`**
(e.g. `networks/Shoot_Branching/`). The orchestrator **creates `<NET>` at the start of the run** and
passes it to every step/subagent. **All** `data/`, `network/`, `validation/`, `refinement/`, and
`supplementary/` paths in the table below and in every `Agent/*_AGENT.md` file are **relative to `<NET>`**
— they are NOT at the project root. `<Phenotype_Slug>` = the phenotype in `Title_Case_With_Underscores`.
Scripts live at the project root and take the dir as an argument:
QA = `python Agent/shared/check_network_structure.py <NET> --dry-run`;
schema (one trait) = `python Agent/shared/validate_schema.py --network <NET>`;
schema (all traits) = `python Agent/shared/validate_schema.py --all networks`.

| Step | Agent | Instruction File | Input Files | Output Files |
|------|-------|-----------------|-------------|--------------|
| 1 | LITERATURE REVIEW | `Agent/LITERATURE_REVIEW_AGENT.md` | (phenotype query) | `data/curated_edges.json`, `data/perturbation_dataset.json` |
| 1.5 | LITERATURE REVIEW JUDGE | `Agent/LITERATURE_REVIEW_JUDGE_AGENT.md` | `data/curated_edges.json`, `data/perturbation_dataset.json` | updated in-place (append-only) + `data/literature_judge_report.json` |
| 2 | BUILDER | `Agent/BUILDER_AGENT.md` | `data/curated_edges.json`, `data/literature_judge_report.json` | `network/network.json`, `algebraic_equations.json`, `ode_equations.json`, `node_annotations.json` (QA: `check_network_structure.py --dry-run`) |
| 2.5 | JUDGE | `Agent/JUDGE_AGENT.md` | `network/*.json`, `data/curated_edges.json` (NEVER perturbations/validation) | `network/judge_review_iteration_1.json` (slim). **ONE pass — BUILDER applies once, no loop.** |
| 3 | PERTURBATION | `Agent/PERTURBATION_AGENT.md` | `data/perturbation_dataset.json`, `network/network.json` | `data/reconciled_perturbation_dataset.json` |
| 4 | VALIDATOR | `Agent/VALIDATOR_AGENT.md` | `network/`, `data/reconciled_perturbation_dataset.json` | `validation/*_results.json`, `validation/*.csv` |
| 5 | REFINEMENT | `Agent/REFINEMENT_AGENT.md` | `validation/`, `network/` | `refinement/iteration_N/` (snapshots) |
| 6 | EXPORT | `Agent/EXPORT_AGENT.md` | best `network/`, best `validation/` | `supplementary/Table_S*.csv`, `master_test_level.csv`, `Fig_Data/`, `network/cytoscape/` |

**Rules per step:**
1. LITERATURE REVIEW: Extract EVERYTHING. No network building. No test selection. Single agent, knowledge-first → WebSearch verify (DOI from the search hit), **no subagents, no WebFetch**. Unconfirmed edges → `literature_gap`.
1.5. LITERATURE REVIEW JUDGE: Audit Step 1 against a canonical biology checklist. Close gaps with **WebSearch only, single pass**; append in place (append-only). Do NOT read perturbation outcomes or `validation/`/`refinement/`. Runs ONCE.
2. BUILDER: Use ONLY edges from `curated_edges.json` (merged Step 1 + 1.5). Do NOT read perturbation results. Generate BOTH algebraic AND ODE equations. Apply the JUDGE's `suggestions[]` once (no loop).
2.5. JUDGE: Biological quality review. Do NOT read perturbations/validation. Suggests only; BUILDER applies. ONE pass. WRITE only slim `{verdict, suggestions[]}`.
3. PERTURBATION: Map tests to network nodes. Do NOT change the network.
4. VALIDATOR: Read-only. Run Python scripts only. No changes to network or perturbations.
5. REFINEMENT: **DIAGNOSE BEFORE FIXING** (`REFINEMENT_AGENT.md §1.5`). Pull algebraic ratio per failure; cluster by mechanism; bundle 2–5 cluster-level fixes per iteration; re-diagnose after each (**max 2 iterations**). Document every change with biological justification + DOI + predicted ratio impact. Save before/after snapshots.
6. EXPORT: Generate supplementary tables + Cytoscape from the BEST model.

**CRITICAL: LITERATURE REVIEW JUDGE, BUILDER, and JUDGE must NOT see perturbation test results.** The
repository is audited, the network is built, and the network is biologically reviewed — all from
literature only. Perturbation-driven changes belong in REFINEMENT (Step 5).

### Key data flow
`curated_edges.json` + `perturbation_dataset.json` (Step 1) → append (1.5) → `network.json` + equations
(2) → slim judge review (2.5) → `reconciled_perturbation_dataset.json` (3) → validation results (4) →
iteration snapshots (5) → supplementary + Cytoscape (6). No `candidate_papers.json` — DOIs live on edges/tests.

---

## Non-Negotiable Rules
1. **Evidence standard**: every edge needs a verified DOI (`d`). No fabricated references. Light keeps the DOI only.
2. **Equation formulas are FIXED** (geometric-mean activation, bounded-inverse inhibition; same for all node types). Full math + ODE/RWR + parameters: `PIPELINE_REFERENCE.md` → *Equation Formulas*. WT baseline = 1.0 when all inputs = 1.0.
3. **Validation scripts only**: use the Python validators. Never compute results yourself.
4. **WT baseline = 1.0** for all nodes.
5. **Provenance carry-through**: the `doi` (`d`) only, in all output files.
6. **No overwrite**: each refinement iteration saved to `iteration_N/`. Never overwritten.
7. **Python only for validation**: only the 3 validators + export scripts use Python. Everything else is reasoning.
8. **No disconnected nodes**: every node MUST have ≥1 edge.
9. **Comprehensive testing**: include ALL known consensus perturbations (100+ for well-studied phenotypes). Never cherry-pick.
10. **`curated_edges.json` is the full repository**: ALL edges found go in, each with a `doi`. BUILDER selects which to use.
11. **No `candidate_papers.json`**: every DOI lives on its edge/test.
12. **No floating / knowledge-graph nodes**: every node in `network.json` must reach the PHENOTYPE via a directed edge path. Verified by `check_network_structure.py` (check 1). If biology exists but isn't on the cascade, document it in `curated_edges.json` only.

## Comparison Rules
| Perturbation Type | Compare To |
|-------------------|------------|
| Single gene KO/KD/OE | WT |
| WT + treatment | WT (no treatment) |
| Mutant + treatment (rescue) | **Mutant alone** (`comparison_baseline: mutant`) |
| Double mutant | WT |

## Schema Compliance
ALL output files MUST conform to Pydantic schemas in `Agent/shared/schemas/`. A **non-blocking** schema
hook checks every pipeline JSON on write (see `.claude/settings.json`); it surfaces errors so they get
fixed immediately but never silently corrupts a file. Validate manually:
```bash
python Agent/shared/validate_schema.py --network {dir}
```
Critical field rules (list of common violations): `PIPELINE_REFERENCE.md` → *Schema Compliance*.

## JSON Metadata
```json
"metadata": {"flash_p_version": "light-1.0-debiasing", "build_variant": "debiasing", "phenotype": "...", "species": "...", "created": "YYYY-MM-DD"}
```
**Build-variant tag (MANDATORY this build).** EVERY metadata block in EVERY output file MUST set
`"flash_p_version": "light-1.0-debiasing"` and include `"build_variant": "debiasing"`. This **overrides**
the `"1.0"` / `"light-1.0"` literals shown in the per-agent `*_AGENT.md` example JSON — use the tagged
values so the generated network self-documents that it came from the de-biasing variant of the specs.

## Error Handling
| Situation | Action |
|-----------|--------|
| Schema validation fails after write | Fix the JSON immediately, re-validate |
| WebSearch can't confirm an edge/test or find a DOI | Flag as `literature_gap`; do not WebFetch |
| Validator accuracy < 50% | Likely a network structure issue. Review cascade paths. |
| Agent cannot complete a step | Document what was done, save partial results, report to user |

---

## Running the pipeline cost-efficiently (subagents + compaction)

The pipeline is **file-driven** — each step reads what the previous step wrote to disk, not the chat
history. That makes the conversation history disposable between steps, which is the key to keeping
token cost down on long (~30 min) runs.

**Default orchestration (recommended).** Run each heavy step as a **subagent** (defined in
`.claude/agents/flashp-*.md`). A subagent has its own isolated context: its big file reads (the
`Agent/*_AGENT.md`, scripts, web-search results) stay in *its* context and only a slim summary returns
to the main thread, so the main conversation never balloons. Invoke them in order:

| Step | Subagent (`subagent_type`) | Model |
|------|----------------------------|-------|
| 1 | *(run in main thread — single agent, per Light design; NOT a subagent)* | **sonnet** (session default) |
| 1.5 | `flashp-literature-judge` | **sonnet** |
| 2 | `flashp-builder` | **opus** (biology-heavy network construction) |
| 2.5 | `flashp-judge` | **sonnet** |
| 3 | `flashp-perturbation` | **sonnet** |
| 4 | `flashp-validator` | **haiku** (just runs scripts) |
| 5 | `flashp-refinement` | **sonnet** |
| 6 | `flashp-export` | **haiku** (just runs scripts) |

After Step 1 (which runs in the main thread and pulls in many WebSearch results), run `/compact` once to
shed that search clutter before delegating Step 1.5 onward. `.claude/settings.json` also sets
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` so the session auto-compacts earlier than the default.

**Session model = Sonnet (cost posture).** `.claude/settings.json` pins the session default to **sonnet**,
so the main-thread Step 1 (literature review) and all orchestration run on Sonnet. Only **BUILDER escalates
to Opus** (its subagent pins `model: opus`) for biology-heavy network construction; the script-runner steps
(VALIDATOR, EXPORT) drop to Haiku; the remaining reasoning steps stay on Sonnet. To run a session on
Opus/fast for other work, use `/model` or `/fast` — it does not change the pinned subagent tiers.

**Per-step models in one run.** Each subagent pins its own `model:`, so the pipeline uses different
models for different steps automatically (Opus for BUILDER, Sonnet for the other reasoning steps, Haiku
for the script-runner steps) without any manual `/model` switching and without disturbing the main cache.

**Targeted reads.** Once a file is Read it stays in context and re-sends every turn. Prefer `Grep` to
locate a symbol over reading a whole file; use Read `offset`/`limit` for large files; don't re-read a
file already in context. Validator/Export subagents pipe script stdout through `tail`/`grep` and read
only the summary JSON fields, never the full dumps.

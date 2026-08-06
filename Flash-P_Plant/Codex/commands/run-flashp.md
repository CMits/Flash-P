# Codex Recipe: run-flashp

Use when the user asks for `/run-flashp <phenotype> in <species>` or says to run the full FLASH-P pipeline.

Target: `<phenotype> in <species>`

## What To Do

Run the full FLASH-P Light pipeline in this Codex workspace. Codex does not use Claude subagents, so execute the steps sequentially in the current Codex session. The pipeline is file-driven: each step writes files under one trait folder and the next step reads those files from disk.

## Codex Token Discipline

Do not read `AGENTS.md` wholesale with `Get-Content -Raw`. It is large and can push a Codex batch child
past the model context window. Use this recipe as the command contract, and read only the next step's
`Agent/*_AGENT.md` file when that step starts. Prefer `rg`, `Select-String`, and short targeted reads
over dumping whole instruction files, Python scripts, validation output, or WebSearch results into chat.

## Required Working Directory

Run from `Flash-P_Plant/Codex`.

## Output Directory

Create and use:

```text
networks/<Phenotype_Slug>/
```

All step outputs must stay inside that directory except cross-network figure summaries explicitly produced by export scripts.

## Steps

1. Read `Agent/LITERATURE_REVIEW_AGENT.md` with targeted/section reads.
2. Step 1 literature review: write `data/curated_edges.json` and `data/perturbation_dataset.json`. Use DOI-grounded evidence. Do not build the network yet.
3. Step 1.5 literature judge: read `Agent/LITERATURE_REVIEW_JUDGE_AGENT.md`; append missing edges/tests in place and write `data/literature_judge_report.json`.
4. Step 2 builder: read `Agent/BUILDER_AGENT.md`; create `network/network.json`, `network/algebraic_equations.json`, `network/ode_equations.json`, and `network/node_annotations.json`.
5. Step 2.5 judge: read `Agent/JUDGE_AGENT.md`; write one slim judge review and apply necessary suggestions once.
6. Step 3 perturbation: read `Agent/PERTURBATION_AGENT.md`; write `data/reconciled_perturbation_dataset.json`.
7. Step 4 validation: read `Agent/VALIDATOR_AGENT.md`; run the Python validators, do not hand-compute metrics.
8. Step 5 refinement: read `Agent/REFINEMENT_AGENT.md`; diagnose failures before changing equations/network files.
9. Step 6 export: read `Agent/EXPORT_AGENT.md`; generate supplementary tables, Cytoscape files, visual output, and Studio refresh when supported.
10. Run final schema validation:

```bash
python Agent/shared/validate_schema.py --network networks/<Phenotype_Slug>
```

## Final Response

Report the network directory, node/edge/test counts, best method, accuracy, kappa/MCC if available, failures, FRS/DARS if available, and export status.

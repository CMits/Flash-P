---
description: Merge completed same-species FLASH-P trait networks into one unified pleiotropic network.
argument-hint: <species or network dirs>  e.g. "Arabidopsis_thaliana networks/A networks/B"
model: opus
---

# FLASH-P same-species network merge

Merge request: **$ARGUMENTS**

You are the merge orchestrator. This is NOT `/run-flashp`; do not build new individual networks and
do not refine existing ones. Merge completed networks only.

Use the `flashp-merge` subagent for the heavy work. The subagent must read and follow
`Agent/MERGE_AGENT.md`.

## Supported examples

```text
/merge-flashp networks/Days_To_Flowering networks/Plant_Height
/merge-flashp Sorghum_bicolor
/merge-flashp Arabidopsis_thaliana --all
/merge-flashp --species "Arabidopsis thaliana" --output networks/merged_arabidopsis_network networks/Flowering_Time networks/Seed_Size
```

## Parse Rules

1. If `$ARGUMENTS` contains two or more path-like tokens (`networks/...`, absolute paths, or existing
   directories), treat them as the source networks.
2. If `$ARGUMENTS` contains a species and `--all`, discover all completed networks under `networks/`
   with matching species metadata.
3. If `$ARGUMENTS` contains only a species, discover completed networks under `networks/` with matching
   species metadata.
4. If a network name is given without `networks/`, resolve it as `networks/<name>`.
5. If `--output <dir>` is present, use that output directory. Otherwise use
   `networks/merged_<species_slug>_network`.

## Preflight Before Dispatch

Do a light preflight in this main thread:

1. List candidate directories.
2. Confirm at least two candidate networks exist.
3. For each candidate, confirm these files exist:
   - `network/network.json`
   - `network/algebraic_equations.json`
   - `data/reconciled_perturbation_dataset.json`
4. Check species metadata if cheaply available. If species appear mismatched, stop and report the
   mismatch; do not dispatch the merge.

Keep preflight output terse. Do not read full network JSONs into context.

## Dispatch

Dispatch `flashp-merge` with:

- source network directories;
- detected or requested species;
- output directory if supplied;
- instruction to keep source networks read-only;
- instruction to write `merge_log.json` with every normalization/conflict decision;
- instruction to run schema validation and standard validators where supported;
- instruction to export Cytoscape files with `python Agent/shared/network_to_cytoscape.py "<merged_network>"`
  and verify `network/cytoscape/network.graphml`, `network.sif`, `node_attributes.txt`, and
  `edge_attributes.txt`.

Wait for the subagent to finish, then report its slim summary.

## Safety

- Same species only for normal use.
- Do not modify source networks.
- Do not silently merge fewer than two networks.
- Do not invent missing pleiotropic literature. If evidence is uncertain, mark the test assumption in
  `pleiotropic_perturbation_dataset.json` and `merge_log.json`.

Begin with the preflight now.

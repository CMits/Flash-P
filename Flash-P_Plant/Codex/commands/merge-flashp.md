# Codex Recipe: merge-flashp

Use when the user asks for `/merge-flashp`, merged FLASH-P networks, within-species network merging, or creating a merged multi-trait network.

## Instructions

Read and follow:

```text
Agent/MERGE_AGENT.md
```

Run in the current Codex session. Do not build new individual networks. Only merge existing validated trait networks.

## Expected Outputs

Write the merged network under a folder such as:

```text
networks/merged_<species>_network/
```

Required outputs include:

- `network/network.json`
- `network/algebraic_equations.json`
- `data/merge_log.json`
- `data/reconciled_perturbation_dataset.json`
- `data/pleiotropic_perturbation_dataset.json`
- `network/cytoscape/network.graphml`
- `network/cytoscape/network.sif`
- `network/cytoscape/node_attributes.txt`
- `network/cytoscape/edge_attributes.txt`

Generate Cytoscape files with:

```bash
python Agent/shared/network_to_cytoscape.py <merged_network_dir>
```


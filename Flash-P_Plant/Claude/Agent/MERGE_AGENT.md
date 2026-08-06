# MERGE AGENT v1.0

## Role
Network integration specialist responsible for merging multiple individual trait networks of the same species into a single unified multi-trait network.

## Goal
Produce a merged network that preserves all individual trait predictions while enabling cross-trait (pleiotropic) predictions. Your work is complete when the merged network passes schema validation and pleiotropic perturbation tests are defined.

## Scope
**You handle:**
- Merging multiple {trait}_network directories into one merged_{species}_network
- Node normalization (resolving naming differences across trait networks)
- Edge conflict resolution (when same edge appears with different signs)
- Creating pleiotropic perturbation tests (genes that affect multiple traits)
- Running validators on the merged network

**You do NOT:**
- Build individual networks (that's BUILDER)
- Reconcile perturbations for individual networks (that's PERTURBATION agent)
- Refine individual networks (that's REFINEMENT agent)

## Pipeline Position
- **Runs after:** ALL individual trait networks are validated and refined (Steps 1-5 complete for each)
- **Runs before:** Final validation of merged network
- **Prerequisites:** At least 2 validated individual networks of the same species

## Input Files
| File | Path | Schema | Description |
|------|------|--------|-------------|
| Individual networks | `{species}/{trait}_network/network/network.json` | `NetworkFile` | One per trait (6 for Arabidopsis) |
| Individual equations | `{species}/{trait}_network/network/algebraic_equations.json` | `AlgebraicEquationsFile` | One per trait |
| Individual reconciled perturbations | `{species}/{trait}_network/data/reconciled_perturbation_dataset.json` | `ReconciledPerturbationFile` | One per trait |

## Output Files
| File | Path | Schema | Validate with |
|------|------|--------|---------------|
| Merged network | `{species}/merged_{species}_network/network/network.json` | `NetworkFile` | `python Agent/shared/validate_schema.py {file}` |
| Merged equations | `{species}/merged_{species}_network/network/algebraic_equations.json` | `AlgebraicEquationsFile` | same |
| Merge log | `{species}/merged_{species}_network/data/merge_log.json` | `MergeLogFile` | same |
| Accumulated perturbations | `{species}/merged_{species}_network/data/reconciled_perturbation_dataset.json` | `ReconciledPerturbationFile` | same |
| Pleiotropic perturbations | `{species}/merged_{species}_network/data/pleiotropic_perturbation_dataset.json` | `PleiotropicPerturbationFile` | same |
| Cytoscape export | `{species}/merged_{species}_network/network/cytoscape/` | GraphML/SIF/tables | `python Agent/shared/network_to_cytoscape.py <merged_network>` |

## Workflow (numbered steps)

### Step 1: Inventory individual networks
1. List all {trait}_network directories for the target species
2. For each: verify network.json, algebraic_equations.json, reconciled_perturbation_dataset.json exist
3. Record: node count, edge count, test count per network
4. If fewer than 2 networks: abort (merge requires at least 2)

### Step 2: Node normalization
For each node across all networks:
1. If same ID exists in multiple networks, treat as the same node (exact match)
2. If different IDs refer to the same gene, normalize to ONE ID using this priority:
   - Arabidopsis standard gene name (AGI locus if available)
   - Most common name across networks
   - Alphabetically first
3. Record all normalization decisions in merge_log.json under normalization_map
4. Rules for normalization:
   - Same gene, different case (FLC vs flc): uppercase wins
   - Same gene, different prefix (At/Os prefix): keep species prefix for cross-species, drop for same-species
   - Composite nodes: SMXL678 in one network, SMXL6/7/8 separate in another: keep composite
   - Environment nodes (Photoperiod, Temperature): merge if same biological signal

### Step 3: Edge merging
For each edge (source -> target):
1. If same edge appears in multiple networks with SAME sign: include once, combine evidence DOIs
2. If same edge appears with DIFFERENT signs: keep the one with MORE unique DOIs. If tied, flag as conflict in merge_log
3. If edge appears in only one network: include it
4. Count unique DOIs per edge for evidence strength

### Step 4: Build merged network
1. Combine all normalized nodes
2. Add ALL phenotype nodes (one per trait: Flowering_Time, Shoot_Branching, etc.)
3. Add all merged edges
4. Ensure every node is connected (no disconnected nodes)
5. Write merged network.json
6. Generate merged algebraic_equations.json (one equation per node)

### Step 5: Accumulate perturbation tests
1. Gather all reconciled perturbation tests from individual networks
2. Prefix each test_id with trait abbreviation: FT_T001, SB_T001, etc.
3. Keep gene_modifiers and exogenous_supply as-is (they reference normalized node names)
4. Write accumulated reconciled_perturbation_dataset.json

### Step 6: Create pleiotropic perturbation tests
Pleiotropic tests target genes that affect MULTIPLE traits:
1. For each gene that appears as a perturbation target in 2 or more trait networks:
   - Create a pleiotropic test with expected outcomes for each affected trait
   - Test ID format: PLEIO_001, PLEIO_002, ...
2. For genes known from literature to have pleiotropic effects:
   - Search for cross-trait experimental evidence
   - Each expected_outcome has: phenotype_node + expected_direction
3. Write pleiotropic_perturbation_dataset.json

### Step 7: Validate merged network
1. Run all 3 validators on the merged network
2. Run validators on pleiotropic tests separately
3. Compare: merged accuracy vs individual accuracies (expect slight decrease)

### Step 8: Export Cytoscape files
Run:

```bash
python Agent/shared/network_to_cytoscape.py <merged_network>
```

Verify that `network/cytoscape/` contains:
- `network.graphml`
- `network.sif`
- `node_attributes.txt`
- `edge_attributes.txt`

### Step 9: Write merge log
Record in merge_log.json:
- source_networks: list of input networks with stats
- normalization_map: all name changes
- shared_nodes: nodes appearing in multiple networks
- merged_stats: final node/edge counts

## Output Format (JSON examples)

### merge_log.json
```json
{
  "metadata": {
    "flash_p_version": "<run: python Agent/shared/flashp_version.py --bare>",
    "phenotype": "merged_arabidopsis",
    "species": "Arabidopsis thaliana",
    "created": "2026-04-15"
  },
  "source_networks": [
    {"name": "flowering_time_network", "phenotype_node": "Flowering_Time", "nodes": 66, "edges": 107, "tests": 219}
  ],
  "normalization_map": {"old_name": "new_name"},
  "merged_stats": {"total_nodes": 200, "total_edges": 350, "shared_nodes": 45},
  "shared_nodes": {"FLC": ["flowering_time_network", "seed_size_network"]}
}
```

### pleiotropic_perturbation_dataset.json
```json
{
  "metadata": {
    "flash_p_version": "<run: python Agent/shared/flashp_version.py --bare>",
    "phenotype": "merged_arabidopsis",
    "species": "Arabidopsis thaliana",
    "created": "2026-04-15",
    "network_type": "merged_pleiotropic",
    "total_pleiotropic_tests": 35,
    "total_outcome_pairs": 70
  },
  "pleiotropic_tests": [
    {
      "test_id": "PLEIO_001",
      "gene": "FLC",
      "perturbation_type": "knockout",
      "gene_modifiers": {"FLC": 0.0},
      "exogenous_supply": {},
      "expected_outcomes": [
        {"phenotype_node": "Flowering_Time", "expected_direction": "decreased"},
        {"phenotype_node": "Seed_Size", "expected_direction": "increased"}
      ],
      "evidence": [{"doi": "10.1234/...", "title": "...", "authors": "...", "year": 2020, "journal": "...", "evidence_sentence": "..."}],
      "source_network": "flowering_time_network"
    }
  ]
}
```

## Schema Reference (from Agent/shared/schemas/merged.py)

### MergeLogFile
- `metadata`: MergeLogMetadata (inherits FlashPMetadata: flash_p_version, phenotype, species, created)
- `source_networks`: list of SourceNetworkEntry (name, phenotype_node, nodes, edges, tests)
- `normalization_map`: dict mapping old node names to new names
- `merged_stats`: dict with total_nodes, total_edges, shared_nodes count
- `shared_nodes`: dict mapping node name to list of source network names

### PleiotropicPerturbationFile
- `metadata`: PleiotropicMetadata (inherits FlashPMetadata + network_type, description, total_pleiotropic_tests, total_outcome_pairs)
- `pleiotropic_tests`: list of PleiotropicPerturbation entries, each with:
  - test_id (PLEIO_001 format), gene, perturbation_type, description
  - gene_modifiers (dict), exogenous_supply (dict)
  - expected_outcomes: list of ExpectedOutcome (phenotype_node, expected_direction)
  - evidence: list of EvidenceEntry (doi, title, authors, year, journal, evidence_sentence)
  - source_network: which individual network this originated from

## Error Handling
| Situation | Action |
|-----------|--------|
| Node name conflict (same gene, different meaning) | Keep both with suffix: FT_PIF4, HL_PIF4 |
| Edge sign conflict (same source->target, opposite signs) | More DOIs wins. If tied: flag in merge_log, keep activation |
| Merged network has disconnected component | Add bridging edge from literature or remove disconnected nodes |
| Merged accuracy much lower than individual | Expected (up to 5% drop). If >10% drop: review merged equations |
| No pleiotropic genes found | At minimum, hormones (GA, Auxin) should affect multiple phenotypes |

## Quality Checklist
- [ ] All output files pass `python Agent/shared/validate_schema.py`
- [ ] merge_log.json records ALL normalization decisions
- [ ] Every shared node is documented in shared_nodes
- [ ] Pleiotropic tests have at least 2 expected_outcomes each
- [ ] Merged network has no disconnected nodes
- [ ] All edges have sign (1 or -1), never missing
- [ ] Merged accuracy within 5% of mean individual accuracy
- [ ] `network/cytoscape/` exists with GraphML, SIF, node attributes, and edge attributes

## Handoff
When complete, the merged network is ready for final validation and figure generation.
Files produced: merged network.json, algebraic_equations.json, merge_log.json, reconciled_perturbation_dataset.json, pleiotropic_perturbation_dataset.json, and network/cytoscape exports.

*MERGE AGENT v1.0 -- Part of Flash-P v1.0*

# FLASH-P Light — Abbreviation Lexicon

Single source of truth for the short keys / enum codes used in the **Light** JSON (and
TOON) files. Schemas accept BOTH the short and the long form on input
(`populate_by_name=True` for keys; `LexEnum._missing_` for enum values), but the Light
files are written in the **short** form. `metadata` and `parameters` blocks keep readable
keys (they appear once per file).

## Field keys (record-level)

| short | full | used in |
|---|---|---|
| `id` | test_id | perturbation, reconciled, results |
| `g` | gene | perturbation, reconciled, results |
| `pt` | perturbation_type | perturbation, reconciled, results |
| `ed` | expected_direction | perturbation, reconciled, results |
| `pd` | predicted_direction | results |
| `ng` | network_gene | reconciled |
| `m` | gene_modifiers | reconciled |
| `exo` | exogenous_supply | reconciled |
| `cb` | comparison_baseline | reconciled, results |
| `rt` | reconciliation_type | reconciled |
| `eid` | edge_id | curated_edges, network |
| `s` | source | curated_edges, network |
| `t` | target | curated_edges, network |
| `x` | sign | curated_edges, network |
| `d` | doi | all (single provenance field) |
| `v` | verification | curated_edges, perturbation — written by evidence verification **only** when the claim could not be grounded (`"q"`). Absent = grounded. Never hand-edited. |
| `sp` | species | perturbation |
| `n` | node | equations, annotations |
| `ty` | type | network nodes, equations, annotations |
| `fn` | full_name | network nodes, annotations |
| `src` | is_source | network nodes, equations, annotations |
| `desc` | description | annotations |
| `a` | activators | equations |
| `inh` | inhibitors | equations |
| `f` | formula | equations |

Top-level keys (`metadata`, `nodes`, `edges`, `papers`, `perturbations`, `parameters`,
`annotations`, `equations`) stay readable.

## Enum values

**node type (`ty`, and the `nodes` map values)**

| short | full |
|---|---|
| `G` | GENE |
| `H` | HORMONE |
| `M` | METABOLITE |
| `E` | ENVIRONMENT |
| `PC` | PROTEIN_COMPLEX |
| `R` | REGULATORY_RNA |
| `P` | PHENOTYPE |
| `PR` | PROCESS |

**expected_direction / predicted_direction (`ed`/`pd`)**

| short | full |
|---|---|
| `up` | increased |
| `dn` | decreased |
| `nc` | unchanged |

**reconciliation_type (`rt`)**

| short | full |
|---|---|
| `em` | exact_match |
| `ci` | case_insensitive |
| `fm` | family_member |
| `cc` | composite_collapse |
| `cm` | composite_member |
| `ta` | treatment_analog |
| `mm` | mechanism_mapping |
| `nin` | not_in_network |
| `ctl` | control |

**perturbation_type (`pt`)** — free string; preferred short codes:

| short | full |
|---|---|
| `ko` | knockout |
| `kd` | knockdown |
| `oe` | overexpression |
| `dko` | double_knockout |
| `tko` | triple_knockout |
| `gof` | gain_of_function |
| `lof` | loss_of_function |
| `rsc` | rescue |
| `trt` | treatment |
| `dm` | double_mutant |
| `cmb` | combined |
| `ep` | epistasis |

**Cattle treatment `pt` codes** (also free string; long form accepted):

| short | full |
|---|---|
| `gh` | exogenous_GH (bovine somatotropin / bST) |
| `igf` | exogenous_IGF1 |
| `tst` | exogenous_testosterone |
| `ba` | beta_agonist_treatment (ractopamine, zilpaterol) |
| `nla` | natural_LoF_allele (e.g. MSTN nt821del, MC1R e allele) |
| `imstn` | inhibitor_MSTN (ACE-031 / bimagrumab analogues) |
| `ko+gh` | knockout_plus_GH |
| `kd@mstn` | knockdown_in_MSTN_background |
| `hs` | heat_stress |
| `cs` | cold_stress |
| `fr` | feed_restriction |

## Derived (not stored)

| field | how it's recovered |
|---|---|
| `effect` | from `sign` (`1`→activation, `-1`→inhibition) |
| `in_model` (curated edge) | edge appears in `network.json` (match source/target/sign) |
| node degrees (in/out/total, n_activators/n_inhibitors) | recomputed from edges |
| `phenotype_node` (per test) | from `metadata.phenotype_node` |
| bibliography (title/authors/year/journal) | not kept — Light keeps `doi` only |

# Gene identifier mapping

Maps the gene nodes of a FLASH-P network to stable gene model identifiers, using the
literature evidence FLASH-P collects when it builds the network.

Run it with **`/run-flashp-idmapping <NET>`**. This file documents the machinery underneath
that command.

## Why it is not a lookup

Node names are literature symbols — `SBMATE`, `PIF4`, `NAM_B1`, `BTR1_BTR2` — and turning
them into identifiers runs into three things at once.

**Reference annotations name very few genes.** In the offline cache, *Sorghum bicolor*
carries a gene symbol for 1.2% of its genes, wheat 1.5%, tomato 4.4%; *Arabidopsis
thaliana* manages 37%. A cache miss for a crop is close to uninformative.

**Many names are borrowed from another organism.** Across the FLASH-P networks that carry
evidence, 43% of gene nodes have supporting literature drawn *exclusively* from a species
other than the network's own. A sorghum node called `PIF4` is an Arabidopsis symbol;
querying sorghum's annotation for it cannot succeed. FLASH-P records the species of every
supporting sentence, which is what makes this detectable.

**No single source is reliable.** Papers sometimes print an accession from an older
annotation release, or simply the wrong one. Many-to-many ortholog projections are right
about a third of the time. Functional descriptions are protein-family assignments, so they
identify a family and never a gene.

What works is agreement between independent routes, and judgement where they disagree.
Hence a subagent, with deterministic tools underneath it.

## Use

```bash
python Agent/shared/idmap/prepare.py networks/Grain_Yield_Sorghum
```

That runs every mechanical step and stops. The `flashp-gene-id-mapper` subagent then reads
`node_dossiers.json`. Where a node carries `gathered.anchors_for_review`, it first rules on
the identifiers cited papers name near that node's gene name — the gathering step
deliberately projects none of them, because whether a paper pairs an accession with a name
is reading comprehension rather than string geometry:

```bash
python Agent/shared/idmap/project_anchor.py \
    --dossiers networks/Grain_Yield_Sorghum/idmapping/node_dossiers.json \
    --node CGA1 --gene Sobic.010G173300 --from sorghum_bicolor --verdict accept \
    --why "the accession sits inside the parenthetical defining CGA1"
```

It then judges each node, writes `judgements.jsonl`, and runs:

```bash
python Agent/shared/idmap/emit_mapping.py \
    --dossiers networks/<NET>/idmapping/node_dossiers.json \
    --judgements networks/<NET>/idmapping/judgements.jsonl \
    --outdir networks/<NET>/idmapping
python Agent/shared/idmap/build_report.py --outdir networks/<NET>/idmapping
```

Requires network access for everything but `--offline`. Only networks with
`data/evidence.json` are in scope; earlier FLASH-P versions did not produce it, and
`backfill_evidence.py` reconstructs one for those.

## Outputs

Everything lands in `<NET>/idmapping/`. **`<NET>/network/network.json` is never modified** —
FLASH-P's schema drops keys it does not know on the next round-trip, so an identifier
written there would be silently lost. `network.idmapped.json` is the annotated copy.

| file | what it holds |
|---|---|
| `node_dossiers.json` | the working file: evidence, routes and candidates per node |
| `judgements.jsonl` | the subagent's decision per node |
| `mapping.tsv` | 19 columns, one row per mappable node |
| `unresolved.tsv` | the same schema, unresolved and ambiguous rows only |
| `network.idmapped.json` | the network plus `gid`, `gid_relation`, `gid_confidence` |
| `anchor_agreement.tsv` | the subagent's anchor rulings vs the character-distance rule |
| `report.html` | a readable report for one run |

`relation` is one of `one2one`, `homoeolog_set`, `family_set`, `complex_members`, `proxy`,
`ambiguous`, `unresolved`. `confidence` is `high` (two or more independent routes agreeing —
nothing else earns it), `medium`, `low`, or `none`.

## The scripts

| script | what it does |
|---|---|
| `prepare.py` | runs the four steps below in order; the entry point |
| `build_dossiers.py` | joins `network.json` to `evidence.json`, grouping supporting sentences, DOIs and study species by node; decides where each name comes from |
| `mine_evidence_ids.py` | extracts gene identifiers stated in the node's own cited papers, including the cached open-access full texts |
| `route_node.py` | plans which routes to try, from measured cache coverage rather than a species list |
| `gather_candidates.py` | runs the routes and scores candidates by how many independent routes agree |
| `project_orthologs.py` | Ensembl Compara projection with a reciprocal check, plus Gramene and PLAZA as independent second opinions; cached to disk |
| `plaza_orthologs.py` | PLAZA ortholog pairs, streamed from its bulk download and distilled per species pair |
| `describe_genes.py` | functional descriptions for candidates, and a rarity-weighted family shortlist search |
| `project_anchor.py` | projects an identifier the subagent has ruled a paper genuinely pairs with the node's name, and records the ruling |
| `emit_mapping.py` | merges the judgements with the evidence, enforces the output rules, writes the mapping |
| `build_report.py` | the readable report for one run |
| `prefetch_compara.py` | fills the ortholog cache for a species pair from the Compara bulk dumps |
| `build_ncbi_layer.py` | builds the NCBI description layer for a species |
| `backfill_evidence.py` | reconstructs an `evidence.json` for a pre-Step-1.6 network |

## Orthology sources

Three, deliberately, because they disagree in informative ways:

- **Ensembl Compara** — gene trees, and the only source that labels a pair one-to-one,
  one-to-many or many-to-many. That label is worth more than the projection itself: a
  one-to-one top candidate is right about 95% of the time, a many-to-many one about 37%.
- **Gramene** — its own compara build on a different Ensembl release.
- **PLAZA** — gene family trees, best-hit families and genome collinearity. A genuinely
  different method, and it grades its own belief across four evidence types (TROG, BHIF,
  ORTHO, anchor_point) rather than emitting one label.

A candidate is also projected back to check for a reciprocal best hit.

PLAZA's web API returns server errors, so it is read from the bulk download instead. Its
per-species files are 70–110 MB, so they are streamed and filtered in flight: only rows for
the requested target species are kept, and the distilled pair table is around a megabyte.
The Arabidopsis→sorghum table holds 200,108 ortholog pairs in 886 KB. Species are matched to
PLAZA by NCBI taxon id, so no species list is hard-coded. Pass `--no-plaza` to skip the
one-off download.

## Disk

Two cache roots, and the split matters.

**`resolver/cache/` is committed** (~9.7 MB, 267 species): the offline gene-name cache, the
species manifest and the PANTHER description layers. It ships with the repo so a fresh clone
can map a network without downloading anything, and it is never written to at run time.

**`.flashp_cache/idmap/` is not committed**: NCBI description layers, ortholog projections
and PLAZA pair tables, built per species on first use. Set `FLASHP_IDMAP_CACHE` to put it
elsewhere, and `FLASHP_IDMAP_RESOLVER` to point at a different resolver library.

Anything rebuilt goes to the second root and shadows the first, so refreshing a species
never means editing a tracked file.

## What it does not do

No accuracy figure is claimed. Scoring one needs a hand-adjudicated reference set, which
lives outside this repo and has not been completed; until it is, the reports give coverage
and route agreement only. Coverage should never be read without the species' symbol coverage
next to it — 30% on a species with 475 named genes is not the same result as 30% on
Arabidopsis.

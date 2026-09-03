# Cache contents

Two independent caches live here. They answer different questions.

## `genes/<species>.tsv.gz` — name to identifier

The main cache. One file per Ensembl Plants species, built by joining three layers:

| Layer | Source | Contributes |
|---|---|---|
| spine | Ensembl Plants GFF3 gene lines | `gene_id` + `Name`/`Alias`, from **one assembly** |
| bridge | Ensembl `*.entrez.tsv.gz` | Ensembl gene ID <-> NCBI GeneID |
| alias | NCBI `All_Plants.gene_info.gz` | GeneID -> Symbol + Synonyms |

Columns: `name_norm`, `name_raw`, `gene_id`, `sources`.

Rows are **source-attributed raw facts**, never a resolved verdict — "this source, at this
release, said name X is gene Y". Caching a verdict instead would make a single bad answer
permanent and invisible. Candidate counts and confidence are computed at lookup time by
`scripts/lookup_cache.py`.

Two things that will silently break a rebuild:

- **Identifiers need canonicalising, not just names.** Some builds write `gene-Solyc01g005000.3`;
  most write `Solyc01g005000`. Comparing naively scored 0/40 on tomato with 35 apparent wrong
  answers — a failure that looks exactly like a broken cache rather than a formatting mismatch.
- **Join on GeneID, not LocusTag.** NCBI carries a LocusTag for under 1% of rows in most crops.

`manifest.tsv` records per species: assembly, Ensembl release, gene and name counts, how many
names are ambiguous, and whether the GeneID bridge existed. Consult it before trusting a species —
`bridge = no` means the alias layer is missing and coverage is the GFF3 display names only.

Rebuild or extend:

```bash
curl -O https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Plants/All_Plants.gene_info.gz
python3 ~/.claude/skills/gene-id-resolver/scripts/build_cache.py \
  --gene-info All_Plants.gene_info.gz --workers 8 --resume
```

Ensembl Plants ships roughly two releases a year. `manifest.tsv` carries the release each file was
built from; refresh when it falls behind the assembly you are reporting against.

## `<species>_<release>.txt.gz` — annotation release dating

A different job: one sorted list of gene identifiers per annotation *release*, used by
`scripts/infer_annotation_release.py` to date a document by set membership. Needed only where the
identifier prefix did not change between releases, so a prefix rule cannot date it — tomato
`Solyc` and rice `LOC_Os`. Maize needs none, because `GRMZM` -> `Zm00001d` -> `Zm00001eb` dates
itself.

Extend with:

```bash
python3 ../scripts/infer_annotation_release.py --species tomato --build
```

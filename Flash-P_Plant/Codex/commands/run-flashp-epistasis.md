# Codex Recipe: run-flashp-epistasis

Use when the user asks for `/run-flashp-epistasis` or gene-by-gene interaction analysis on an existing FLASH-P network.

## Script

```bash
python Agent/shared/scan_epistasis.py <NET> --epistasis --classify --out <NET>/epistasis/epistasis_doubles.tsv
```

`<NET>` is a built network directory, for example `networks/Days_To_Flowering`.

## Report

Summarize WT baselines, single/double counts, class breakdown, and the top genuine interactions. Treat algebraic classification as primary; ODE is corroboration only.

Do not edit the network. This analysis only writes under `<NET>/epistasis/`.


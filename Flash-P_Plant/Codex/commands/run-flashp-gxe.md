# Codex Recipe: run-flashp-gxe

Use when the user asks for `/run-flashp-gxe` or gene-by-environment analysis on an existing FLASH-P network.

## Script

```bash
python Agent/shared/gxe_report.py <NET> --modes KO,OE --doses 0.25,0.5,1,2
```

The script writes:

```text
<NET>/gxe/gxe_anchored.tsv
<NET>/gxe/gxe_cross.tsv
<NET>/gxe/GXE_REPORT.md
```

Read `GXE_REPORT.md` and summarize env levers, warnings, dose saturation, algebraic-vs-ODE agreement, and top GxE hits.

Do not edit the network except validation files if a required validator step is explicitly run first.


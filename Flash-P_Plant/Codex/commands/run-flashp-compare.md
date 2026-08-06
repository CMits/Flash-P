# Codex Recipe: run-flashp-compare

Use when the user asks for `/run-flashp-compare` or wants two built FLASH-P networks compared side by side.

## Script

```bash
python Agent/shared/network_to_compare.py <netA> <netB>
```

Optional:

```bash
python Agent/shared/network_to_compare.py <netA> <netB> --out <path>
```

The report is a self-contained HTML file with validation comparison, conserved mechanisms, divergent regulation, unique nodes, and unique edges.

Do not edit either network.


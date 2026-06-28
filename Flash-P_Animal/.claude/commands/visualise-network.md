---
description: Render a built FLASH-P network as a website-faithful plot — interactive HTML (clickable, DOI links) + static SVG + PNG.
argument-hint: <network_dir> (e.g. networks/Flowering_Time)
model: haiku
---

# Visualise network

Generate a publication-quality, **website-faithful** plot of an already-built FLASH-P
network. Pure script execution — no network edits, no recomputation.

Run:

```bash
python Agent/shared/network_to_visual.py $ARGUMENTS
```

(Append `--dark`, `--transparent`, `--no-static`, or `--scale N` if the user asks.)

This reads `<network_dir>/network/network.json` (+ optional `node_annotations.json`),
styles it by node type from `Agent/shared/visual/assets/flashp_style.json` (the website
vizmap), and writes into `<network_dir>/network/visual/`:

- **`network.html`** — interactive, clickable; click a node to see its function and the
  DOIs of its edges (linked to doi.org). Single self-contained shareable file.
- **`network.svg`** — static vector (figures).
- **`network.png`** — static raster (preview/thumbnail).

The static SVG/PNG are rendered by the real Cytoscape.js via headless Chrome, so they
match the interactive view. They require the Node toolchain:

```bash
cd Agent/shared/visual && npm install
```

If Node / `node_modules` / Chromium are missing, the script still writes `network.html`
(using CDN-loaded libraries) and prints how to enable SVG/PNG — it never fails.

**Report**: the three output paths actually written, the node/edge counts, and — if
static rendering was skipped — the one-line reason and the `npm install` hint.

---
description: Render a built FLASH-P network as a website-faithful plot — interactive HTML (clickable, DOI links) + static SVG + PNG.
argument-hint: <network dir>  e.g. "networks/Stomatal_Conductance"
model: claude-sonnet-4-6
---

# FLASH-P network visualisation

Target network directory: **$ARGUMENTS**  (the `<NET>` for this run; resolve a bare trait name to `networks/<Trait>/`).

You are generating a **publication-quality, website-faithful** plot of an already-built FLASH-P network
(this command does NOT build a network — use `/run-flashp` for that). The heavy, deterministic work lives
in `Agent/shared/network_to_visual.py`, which reads `network.json` and the style map and writes three
output files. Your job is to sanity-check the network, invoke the script, and relay the output paths and
any warnings. Keep it token-lean: pipe script output through `tail`, read only what you need to report.

## What the script writes (already baked in)
- **`<NET>/network/visual/network.html`** — interactive, clickable; click a node to see its function and
  the DOIs of its edges (linked to doi.org). Single self-contained shareable file.
- **`<NET>/network/visual/network.svg`** — static vector (figures).
- **`<NET>/network/visual/network.png`** — static raster (preview/thumbnail).

The static SVG/PNG are rendered by the real Cytoscape.js via headless Chrome, so they match the interactive
view. They require the Node toolchain (`cd Agent/shared/visual && npm install`). If Node / `node_modules` /
Chromium are missing the script still writes `network.html` and says how to enable SVG/PNG — it never fails.

## Execution plan

1. **Resolve & sanity-check `<NET>`.** Confirm `<NET>/network/network.json` (or flat `<NET>/network.json`)
   exists. If not, tell the user the path isn't a FLASH-P network and stop.

2. **Run the script:**
   ```
   python Agent/shared/network_to_visual.py <NET>
   ```
   Append any user-requested flags: `--dark`, `--transparent`, `--no-static`, `--scale N`.

3. **Relay the output** concisely:
   - The three output paths actually written.
   - Node and edge counts rendered.
   - If static SVG/PNG were skipped — the one-line reason and the `npm install` hint.

## Guard rails
- Do not modify `network/`, `data/`, equations, or any pipeline files — visualisation is **read-only**
  with respect to the network; it only writes under `<NET>/network/visual/`.
- Do not invent or modify node styles; `Agent/shared/visual/assets/flashp_style.json` is the single source
  of truth for colours and shapes.

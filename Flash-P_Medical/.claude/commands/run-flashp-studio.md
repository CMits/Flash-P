---
description: Build the FLASH-P Studio — a self-contained HTML app to browse, view (DOIs), export (PNG/SVG) and perturbate ALL your built networks. No server, no install.
argument-hint: <networks dir>  e.g. "networks" (the folder that contains your trait networks)
model: claude-sonnet-4-6
---

# FLASH-P Studio (browse + view + export + simulate)

Target networks directory: **$ARGUMENTS**  (the folder that **contains** your trait networks, e.g.
`networks` or `Networks_Flash-P` — NOT a single trait folder). Defaults to `networks` if no argument given.

You are generating the **FLASH-P Studio**: one self-contained HTML file that embeds **every** built
network under the given directory and lets the user browse them, view each interactive graph (click a
node for its function + edge DOIs), **export** the on-screen network as a PNG or SVG image (the Export
buttons in the view toolbar — this replaces the old `/run-flashp-visualise` command), and **perturbate**
them (KO/KD/OE + treatments) with the three solvers (Algebraic / RWR / ODE), live convergence chart and
node table. It is a
faithful local port of the website's simulate page. The heavy work lives in
`Agent/shared/network_to_studio.py`; your job is to invoke it and relay the result. Keep it token-lean:
pipe script output through `tail`, read only what you need to report.

## What the script writes (already baked in)
- **`<networks_dir>/Flash-P_Studio.html`** — a single self-contained, offline file. All network data, the
  Cytoscape libraries, the solver engine and the chart are embedded — no server, no install, no upload.
  The script **auto-opens it in your default browser** as soon as it's built; double-click the file to
  reopen it later. Re-run to refresh after building new networks.
- **PNG / SVG export is built into the Studio itself** — when viewing a network, the toolbar's `⭳ PNG`
  and `⭳ SVG` buttons download a publication-quality image (3× PNG, vector SVG) of the current graph,
  client-side in the browser. No extra command or toolchain is needed.

## Why embedded (not a server)
Browsers block a `file://` page from reading other files on disk, so a "pick a network" dropdown that reads
many networks at runtime would need a local server. The script instead **embeds** all networks at generation
time (the same trick `network.html` uses), so the dropdown works offline by double-click.

## Execution plan

1. **Resolve the directory.** If no argument, use `networks`. Confirm it exists and contains at least one
   `*/network/network.json`. If not, tell the user there are no built networks there and stop.

2. **Run the script:**
   ```
   python Agent/shared/network_to_studio.py <networks_dir>
   ```
   It builds the file **and auto-opens it in the default browser**. Add `--no-open` if the user only wants
   the file regenerated without a browser window (e.g. headless / scripted runs).

3. **Relay the output** concisely:
   - Confirm it opened in the browser, and give the output path (`<networks_dir>/Flash-P_Studio.html`) so
     the user can reopen or share it (double-click).
   - How many networks were embedded (and any that were skipped, with the one-line reason).

## Guard rails
- Read-only with respect to the networks — the script only writes `Flash-P_Studio.html` at the directory
  root; it never modifies `network/`, `data/`, equations, or any pipeline files.
- Do not invent or modify node styles or solver parameters; `Agent/shared/visual/assets/flashp_style.json`
  is the single source of truth for colours/shapes, and the per-network best parameters come from each
  network's `validation/accuracy_metrics.json`.

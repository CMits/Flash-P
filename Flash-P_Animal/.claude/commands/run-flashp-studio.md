---
description: Build the FLASH-P Studio — a self-contained HTML app to browse, view (DOIs), export (PNG/SVG), perturbate and WATCH the propagation animate across ALL your built networks. No server, no install.
argument-hint: <networks dir>  e.g. "networks" (the folder that contains your trait networks)
model: claude-sonnet-4-6
---

# FLASH-P Studio (browse + view + export + simulate + visual propagation)

Target networks directory: **$ARGUMENTS**  (the folder that **contains** your trait networks, e.g.
`networks` or `Networks_Flash-P` — NOT a single trait folder). Defaults to `networks` if no argument given.

You are generating the **FLASH-P Studio**: one self-contained HTML file that embeds **every** built
network under the given directory and lets the user browse them, view each interactive graph (click a
node for its function + edge DOIs), **export** the on-screen network as a PNG or SVG image (the Export
buttons in the view toolbar — this replaces the old `/run-flashp-visualise` command), and **perturbate**
them (KO/KD/OE + treatments) with the three solvers (Algebraic / RWR / ODE), live convergence chart and
node table — then **watch that perturbation propagate**, iteration by iteration, in the Visual
Propagation view. It is a
faithful local port of the website's simulate and propagation pages. The heavy work lives in
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
- **Visual Propagation is built in too** — after a run, the results panel gains a `▶ Watch propagation`
  button (and a nav tab appears). It replays *that same run*, stepping through the solver iterations:
  comets travel down the edges that actually carried the change, nodes recolour on a fixed red↔blue
  scale, a chart tracks each node against the chosen baseline, and a narration panel shows the
  arithmetic — including the damping line — behind every node that moved. The run is **never
  re-solved**, so the animation cannot disagree with the table and chart beside it. Edges that exist in
  `network.json` but are not used by the equations are drawn dashed and never animate.

## What the propagation view is honest about
Worth knowing, because it is a deliberate design constraint rather than a limitation to fix:
- The solver updates **every node at once** from the previous iteration's values. The animation shows
  all live edges firing simultaneously because that is what the equations do — it is not a relay.
- The run is chaptered into two labelled phases: **Spread** (the change is still reaching new nodes, so
  a travelling comet is truthful) and **Settle** (nothing new is recruited; everything relaxes together).
- The step readout always shows the **true solver iteration**, never a renumbered fiction.
- The colour domain is **fixed**, never fitted to the run, so "this node got redder" can only mean the
  data moved.

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

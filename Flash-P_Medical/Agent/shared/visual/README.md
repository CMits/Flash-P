# FLASH-P visual — standalone network plots

Generates a website-faithful plot of a built FLASH-P network in three forms:

- `network.html` — interactive, clickable (click a node → its function + edge DOIs), single shareable file.
- `network.svg` — static vector (for figures).
- `network.png` — static raster (preview/thumbnail).

The look is a faithful port of the website renderer
(`FLASHP_WEBSITE/Flash-P-AI/src/components/traits/NetworkGraph.tsx`): same Cytoscape
stylesheet, `NODE_SCALE`, and hand-rolled ELK orthogonal layout. Styling is keyed by
**node type** via `assets/flashp_style.json` (a vendored copy of the website `style.json`)
— not the older colours embedded in the GraphML.

## Run

```bash
# from the variant root (e.g. Flash-P_Plant/Claude)
python Agent/shared/network_to_visual.py <network_dir>
```

`<network_dir>` is a trait folder containing `network/network.json` (+ optional
`network/node_annotations.json`). Outputs land in `<network_dir>/network/visual/`.

Options: `--dark`, `--transparent`, `--no-static` (HTML only), `--scale N` (PNG DPI).

## Static images (SVG/PNG)

The static images are rendered by the **real Cytoscape.js** through headless Chrome, so
they match the interactive view exactly. This needs Node + this package's dependencies:

```bash
cd Agent/shared/visual
npm install
```

If the bundled Chromium download is blocked, install Chrome/Edge and set
`PUPPETEER_EXECUTABLE_PATH` (and optionally `PUPPETEER_SKIP_DOWNLOAD=1` before `npm install`).

If Node / `node_modules` / Chromium are missing, `network_to_visual.py` still writes
`network.html` (falling back to CDN-loaded libraries) and prints how to enable SVG/PNG —
it never fails the export.

## Notes

- The HTML is self-contained and offline (~2 MB, dominated by the ELK bundle) when
  `node_modules` is present at generation time; otherwise it loads pinned libraries from a CDN.
- The dot-grid background is a CSS layer on the canvas container, so it is intentionally
  absent from the SVG/PNG (which carry a flat `#fbfdfc` / transparent background).

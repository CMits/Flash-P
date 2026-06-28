#!/usr/bin/env python3
"""
Render a built FLASH-P network as a website-faithful plot in three forms:

  - network.html : interactive, clickable (click a node -> its function + edge DOIs),
                   single self-contained shareable file.
  - network.svg  : static vector (publication figures).
  - network.png  : static raster (preview / thumbnail).

The look is a faithful port of the website renderer
(FLASHP_WEBSITE/Flash-P-AI/src/components/traits/NetworkGraph.tsx): same Cytoscape
stylesheet, NODE_SCALE, and hand-rolled ELK orthogonal layout. Styling is keyed by
**node type** via Agent/shared/visual/assets/flashp_style.json (a vendored copy of the
website style.json) — NOT the older colours embedded in the GraphML.

The interactive HTML is written by Python (no Node needed). The static SVG/PNG are
rendered by the REAL Cytoscape.js through headless Chrome (Node + Puppeteer) so they
match the interactive view exactly. If Node / node_modules / Chromium are missing, the
HTML is still written (falling back to CDN-loaded libraries) and an actionable message
is printed — the export never fails.

Usage:
    python Agent/shared/network_to_visual.py <network_dir> [--dark] [--transparent]
                                             [--no-static] [--scale 3]

Output: <network_dir>/network/visual/{network.html, network.svg, network.png}
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import light_io  # short-key/TOON -> rich long-key loader (shared with network_to_cytoscape.py)
from flashp_version import get_version

VISUAL_DIR = Path(__file__).parent / "visual"
STYLE_FILE = VISUAL_DIR / "assets" / "flashp_style.json"
TEMPLATE_FILE = VISUAL_DIR / "template.html"
GRAPH_JS_FILE = VISUAL_DIR / "flashp-graph.js"
RENDER_JS = VISUAL_DIR / "render.js"

# Libraries the page needs, in load order (deps before dependents, engine handled
# separately). Each: node_modules candidate paths + a pinned CDN url for fallback.
_CDN = "https://unpkg.com/"
LIBS = [
    (["cytoscape/dist/cytoscape.min.js"], "cytoscape@3.34.0/dist/cytoscape.min.js"),
    (["dagre/dist/dagre.min.js"], "dagre@0.8.5/dist/dagre.min.js"),
    (["cytoscape-dagre/dist/cytoscape-dagre.js", "cytoscape-dagre/cytoscape-dagre.js"],
     "cytoscape-dagre@4.0.0/dist/cytoscape-dagre.js"),
    (["layout-base/layout-base.js"], "layout-base@2.0.1/layout-base.js"),
    (["cose-base/cose-base.js"], "cose-base@2.2.0/cose-base.js"),
    (["cytoscape-fcose/cytoscape-fcose.js"], "cytoscape-fcose@2.2.0/cytoscape-fcose.js"),
    (["elkjs/lib/elk.bundled.js"], "elkjs@0.11.1/lib/elk.bundled.js"),
    (["cytoscape-svg/cytoscape-svg.js", "cytoscape-svg/dist/cytoscape-svg.js"],
     "cytoscape-svg@0.4.0/cytoscape-svg.js"),
]


def _sign_key(sign) -> str:
    try:
        return "negative" if int(sign) < 0 else "positive"
    except (TypeError, ValueError):
        return "positive"


def build_payload(network_dir: Path) -> dict:
    """Load network (+ annotations) and assemble the JS data payload."""
    net_file = network_dir / "network" / "network.json"
    if not net_file.exists():
        raise FileNotFoundError(f"Network file not found: {net_file}")
    net = light_io.load(str(net_file))

    ann_by_id = {}
    ann_file = network_dir / "network" / "node_annotations.json"
    if ann_file.exists():
        try:
            ann = light_io.load(str(ann_file))
            for a in ann.get("annotations", []):
                nid = a.get("node") or a.get("n")
                if nid:
                    ann_by_id[nid] = {
                        "fn": a.get("full_name", "") or "",
                        "desc": a.get("description", "") or "",
                    }
        except Exception as e:  # annotations are optional enrichment, never fatal
            print(f"  (annotations skipped: {e})")

    elements = []
    for n in net.get("nodes", []):
        elements.append({"data": {
            "id": n["id"],
            "label": n["id"],
            "ty": n.get("type") or "GENE",
            "fn": n.get("full_name", "") or "",
            "src": bool(n.get("is_source")),
        }})
    for i, e in enumerate(net.get("edges", [])):
        elements.append({"data": {
            "id": e.get("edge_id") or f"e{i}",
            "source": e["source"],
            "target": e["target"],
            "sign": e.get("sign"),
            "sk": _sign_key(e.get("sign")),
            "doi": e.get("doi", "") or "",
        }})

    meta = net.get("metadata", {})
    style = json.loads(STYLE_FILE.read_text(encoding="utf-8"))
    return {
        "meta": {
            "slug": network_dir.name,
            "phenotype": meta.get("phenotype", network_dir.name),
            "species": meta.get("species", ""),
            "flash_p_version": meta.get("flash_p_version", get_version()),
            "n_nodes": sum(1 for el in elements if "source" not in el["data"]),
            "n_edges": sum(1 for el in elements if "source" in el["data"]),
        },
        "elements": elements,
        "style": style,
        "annById": ann_by_id,
    }


def _resolve_local(candidates) -> Path:
    for c in candidates:
        p = VISUAL_DIR / "node_modules" / c
        if p.exists():
            return p
    return None


def _libs_offline():
    """Return inlined <script> tags if every lib is vendored locally, else None."""
    tags = []
    for candidates, _cdn in LIBS:
        p = _resolve_local(candidates)
        if p is None:
            return None
        js = p.read_text(encoding="utf-8", errors="replace").replace("</script", "<\\/script")
        tags.append(f"<script>{js}</script>")
    return "\n".join(tags)


def _libs_cdn():
    return "\n".join(f'<script src="{_CDN}{cdn}"></script>' for _c, cdn in LIBS)


def write_html(payload: dict, out_html: Path) -> bool:
    """Write the self-contained interactive HTML. Returns True if libs were inlined (offline)."""
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    graph_js = GRAPH_JS_FILE.read_text(encoding="utf-8")

    offline_tags = _libs_offline()
    libs_block = offline_tags if offline_tags is not None else _libs_cdn()

    # Embed data safely inside <script type="application/json"> — neutralise any "</".
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = f"{payload['meta']['phenotype']}"
    if payload["meta"]["species"]:
        title += f" — {payload['meta']['species']}"

    html = (
        template
        .replace("<!--FLASHP_LIBS-->", libs_block)
        .replace("<!--FLASHP_GRAPH_JS-->", graph_js)
        .replace("<!--FLASHP_DATA-->", data_json)
        .replace("<!--FLASHP_TITLE-->", title)
    )
    out_html.write_text(html, encoding="utf-8")
    return offline_tags is not None


def _static_available() -> str:
    """Return '' if static rendering can run, else a human-readable reason it can't."""
    if shutil.which("node") is None:
        return "Node.js not found on PATH"
    if not (VISUAL_DIR / "node_modules" / "cytoscape").exists():
        return f"dependencies not installed (run: cd {VISUAL_DIR} && npm install)"
    if not (VISUAL_DIR / "node_modules" / "puppeteer").exists() and \
       not (VISUAL_DIR / "node_modules" / "puppeteer-core").exists():
        return f"puppeteer not installed (run: cd {VISUAL_DIR} && npm install)"
    return ""


def render_static(payload: dict, out_svg: Path, out_png: Path, args) -> bool:
    """Render SVG + PNG via the headless Node renderer. Returns True on success."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(payload, tf, ensure_ascii=False)
        data_path = tf.name
    cmd = ["node", str(RENDER_JS), "--data", data_path,
           "--out-svg", str(out_svg), "--out-png", str(out_png),
           "--scale", str(args.scale)]
    if args.dark:
        cmd.append("--dark")
    if args.transparent:
        cmd.append("--transparent")
    try:
        res = subprocess.run(cmd, cwd=str(VISUAL_DIR), capture_output=True, text=True, timeout=240)
        if res.returncode != 0:
            print("  Static render failed (HTML is still available):")
            tail = (res.stderr or res.stdout or "").strip().splitlines()[-12:]
            for line in tail:
                print("    " + line)
            return False
        for line in (res.stdout or "").strip().splitlines():
            print("  " + line)
        return True
    except subprocess.TimeoutExpired:
        print("  Static render timed out (HTML is still available).")
        return False
    finally:
        try:
            Path(data_path).unlink()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Render a FLASH-P network as interactive HTML + static SVG/PNG (website look).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python Agent/shared/network_to_visual.py networks/Flowering_Time
    python Agent/shared/network_to_visual.py networks/Shoot_Branching --dark

Output (in <network_dir>/network/visual/):
    network.html  : interactive, clickable, DOI links
    network.svg   : static vector
    network.png   : static raster
        """,
    )
    parser.add_argument("network_dir", help="Trait directory containing network/network.json")
    parser.add_argument("--dark", action="store_true", help="Dark theme")
    parser.add_argument("--transparent", action="store_true", help="Transparent background for SVG/PNG")
    parser.add_argument("--no-static", action="store_true", help="Write only the HTML (skip SVG/PNG)")
    parser.add_argument("--scale", type=float, default=3, help="PNG resolution scale (default 3)")
    args = parser.parse_args()

    network_dir = Path(args.network_dir)
    if not network_dir.is_absolute():
        network_dir = Path.cwd() / network_dir
    if not network_dir.exists():
        print(f"Error: directory not found: {network_dir}")
        sys.exit(1)

    print(f"\nFlash-P v{get_version()} — Network Visualiser")
    print(f"Network: {network_dir}")

    try:
        payload = build_payload(network_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"  Loaded: {payload['meta']['n_nodes']} nodes, {payload['meta']['n_edges']} edges")

    out_dir = network_dir / "network" / "visual"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_html = out_dir / "network.html"
    out_svg = out_dir / "network.svg"
    out_png = out_dir / "network.png"

    offline = write_html(payload, out_html)
    print(f"  HTML saved: {out_html}  ({'offline, self-contained' if offline else 'CDN libraries'})")

    if args.no_static:
        print("  Static SVG/PNG skipped (--no-static).")
        print("\nDone!")
        return

    reason = _static_available()
    if reason:
        print(f"  SVG/PNG skipped: {reason}.")
        print("    The interactive network.html is ready to open/share now.")
        print("\nDone!")
        return

    print("  Rendering static SVG + PNG (headless Cytoscape)...")
    ok = render_static(payload, out_svg, out_png, args)
    if ok:
        print(f"  SVG saved: {out_svg}")
        print(f"  PNG saved: {out_png}")
    print("\nDone!")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Build the **FLASH-P Studio** — a single self-contained HTML app embedding ALL of
your built networks, with three views:

  - Browse   : pick a network
  - View     : interactive Cytoscape graph (click a node -> function + edge DOIs)
  - Simulate : perturbate the network (KO/KD/OE + treatments), three solvers
               (Algebraic / RWR / ODE), live convergence chart + node table.

It is a faithful local port of the website's simulate page
(FLASHP_WEBSITE/Flash-P-AI). Like network.html the data, style and JS are embedded,
so the file works by double-click — no server, no install, no upload. Re-run this
to refresh after building new networks (the Export step does this automatically).

Usage:
    python Agent/shared/network_to_studio.py <networks_dir> [--no-open]

  <networks_dir> is the folder that CONTAINS your trait networks (e.g. `networks`
  or `Networks_Flash-P`). Every `*/network/network.json` beneath it is embedded.
  By default the finished Studio auto-opens in your default browser; pass
  --no-open to just write the file (e.g. for headless / scripted runs).

Output: <networks_dir>/Flash-P_Studio.html
"""

import argparse
import datetime
import json
import sys
import webbrowser
from pathlib import Path

import light_io
from flashp_version import get_version

# Reuse the visualiser's plumbing (payload build, library inlining, style load).
import network_to_visual as viz

VISUAL_DIR = viz.VISUAL_DIR
TEMPLATE_FILE = VISUAL_DIR / "studio_template.html"
GRAPH_JS_FILE = VISUAL_DIR / "flashp-graph.js"
SIM_JS_FILE = VISUAL_DIR / "flashp-sim.js"
CHART_JS_FILE = VISUAL_DIR / "flashp-chart.js"
PROP_CORE_JS_FILE = VISUAL_DIR / "flashp-prop-core.js"
PROP_VIEW_JS_FILE = VISUAL_DIR / "flashp-prop-view.js"


def _best_method(acc: dict):
    """Pick the headline method (highest accuracy, then MCC) from accuracy_metrics."""
    best, best_key = None, None
    for key in ("algebraic", "ode", "rwr"):
        m = acc.get(key)
        if not isinstance(m, dict) or m.get("accuracy") is None:
            continue
        score = (float(m.get("accuracy", 0)), float(m.get("mcc", 0) or 0))
        if best is None or score > best:
            best, best_key = score, key
    return best_key or "algebraic"


def _solver_inputs(network_dir: Path):
    """Compact network + equation + best-param payload the in-browser solvers expect."""
    net = light_io.load(str(network_dir / "network" / "network.json"))
    nodes = [{"id": n["id"], "ty": n.get("type") or "GENE", "src": bool(n.get("is_source"))}
             for n in net.get("nodes", [])]
    node_ids = {n["id"] for n in net.get("nodes", [])}
    edges = [{"s": e["source"], "t": e["target"], "x": e.get("sign")}
             for e in net.get("edges", [])
             if e["source"] in node_ids and e["target"] in node_ids]

    alg = None
    alg_file = network_dir / "network" / "algebraic_equations.json"
    if alg_file.exists():
        try:
            loaded = light_io.load(str(alg_file))
            # `f` is the published equation string; the Visual Propagation view shows it
            # under each node's arithmetic so the shown sum can be checked against the paper.
            alg = {"equations": [{"n": q.get("node"),
                                  "a": q.get("activators", []) or [],
                                  "inh": q.get("inhibitors", []) or [],
                                  "f": q.get("formula") or q.get("f") or ""}
                                 for q in loaded.get("equations", []) if q.get("node")]}
        except Exception as e:
            print(f"    (equations skipped for {network_dir.name}: {e})")

    acc, best, params = {}, "algebraic", {}
    acc_file = network_dir / "validation" / "accuracy_metrics.json"
    if acc_file.exists():
        try:
            acc = json.loads(acc_file.read_text(encoding="utf-8"))
            best = _best_method(acc)
            params = {
                "alpha": (acc.get("rwr") or {}).get("best_alpha"),
                "K": (acc.get("ode") or {}).get("best_K"),
                "n": (acc.get("ode") or {}).get("best_n"),
            }
        except Exception as e:
            print(f"    (accuracy_metrics skipped for {network_dir.name}: {e})")

    return {"net": {"nodes": nodes, "edges": edges}, "algEq": alg,
            "accuracy": acc, "bestMethod": best, "params": params}


def build_network_entry(network_dir: Path) -> dict:
    """One embedded network: viewer payload (reused) + solver payload."""
    base = viz.build_payload(network_dir)  # {meta, elements, style, annById}
    meta = base["meta"]
    entry = {
        "slug": meta["slug"],
        "name": meta["phenotype"],
        "species": meta["species"],
        "version": meta["flash_p_version"],
        "nNodes": meta["n_nodes"],
        "nEdges": meta["n_edges"],
        "elements": base["elements"],
        "annById": base["annById"],
    }
    entry.update(_solver_inputs(network_dir))
    return entry


def find_networks(root: Path):
    """All network dirs (folders containing network/network.json) beneath root."""
    seen = []
    for nf in sorted(root.glob("**/network/network.json")):
        network_dir = nf.parent.parent
        if network_dir not in seen:
            seen.append(network_dir)
    return seen


def _script(path: Path) -> str:
    js = path.read_text(encoding="utf-8", errors="replace").replace("</script", "<\\/script")
    return f"<script>{js}</script>"


def write_studio(networks: list, out_html: Path) -> bool:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    style = json.loads(viz.STYLE_FILE.read_text(encoding="utf-8"))
    payload = {
        "generated": datetime.date.today().isoformat(),
        "style": style,
        "networks": networks,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    offline = viz._libs_offline()
    libs_block = offline if offline is not None else viz._libs_cdn()
    title = networks[0]["name"] if len(networks) == 1 else f"{len(networks)} networks"

    html = (
        template
        .replace("<!--FLASHP_LIBS-->", libs_block)
        .replace("<!--FLASHP_GRAPH_JS-->", _script(GRAPH_JS_FILE))
        .replace("<!--FLASHP_SIM_JS-->", _script(SIM_JS_FILE))
        .replace("<!--FLASHP_CHART_JS-->", _script(CHART_JS_FILE))
        .replace("<!--FLASHP_PROP_CORE_JS-->", _script(PROP_CORE_JS_FILE))
        .replace("<!--FLASHP_PROP_VIEW_JS-->", _script(PROP_VIEW_JS_FILE))
        .replace("<!--FLASHP_DATA-->", data_json)
        .replace("<!--FLASHP_TITLE-->", title)
    )
    out_html.write_text(html, encoding="utf-8")
    return offline is not None


def main():
    parser = argparse.ArgumentParser(
        description="Build the FLASH-P Studio (browse + view + simulate) for all networks in a directory.")
    parser.add_argument("networks_dir", help="Directory containing trait networks (e.g. 'networks')")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not auto-open the Studio in a browser after building.")
    args = parser.parse_args()

    root = Path(args.networks_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.exists():
        print(f"Error: directory not found: {root}")
        sys.exit(1)

    print(f"\nFlash-P v{get_version()} — Studio builder")
    print(f"Scanning: {root}")

    dirs = find_networks(root)
    if not dirs:
        print("  No networks found (looked for */network/network.json). Nothing to build.")
        sys.exit(1)

    entries = []
    for d in dirs:
        try:
            entries.append(build_network_entry(d))
            print(f"  + {entries[-1]['name']}  ({entries[-1]['nNodes']} nodes, {entries[-1]['nEdges']} edges)")
        except Exception as e:
            print(f"  ! skipped {d.name}: {e}")

    if not entries:
        print("  No networks could be loaded.")
        sys.exit(1)

    out_html = root / "Flash-P_Studio.html"
    offline = write_studio(entries, out_html)
    print(f"\n  Studio saved: {out_html}  ({'offline, self-contained' if offline else 'CDN libraries'})")
    print(f"  {len(entries)} network(s) embedded.")
    if not args.no_open:
        try:
            webbrowser.open(out_html.resolve().as_uri())
            print("  Opening in your default browser… (re-open any time by double-clicking the file).")
        except Exception as e:
            print(f"  (Could not auto-open — open it yourself by double-click: {out_html})  [{e}]")
    else:
        print("  --no-open set; open it by double-click when you want it — no server needed.")
    print("\nDone!")


if __name__ == "__main__":
    main()

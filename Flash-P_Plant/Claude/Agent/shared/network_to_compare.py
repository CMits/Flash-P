#!/usr/bin/env python3
"""
network_to_compare.py -- side-by-side comparison of two FLASH-P networks.

Given two built trait networks (same trait across species, or two traits), this
reads each network's network.json + validation/accuracy_metrics.json and emits a
single self-contained HTML report:

  * summary cards (nodes / edges / best accuracy for each side)
  * accuracy table (algebraic / ODE / RWR, both networks)
  * CONSERVED mechanisms .... shared edges with the SAME sign
  * DIVERGENT regulation ..... shared edges whose sign FLIPPED between networks
  * species-specific nodes .... present in only one network
  * species-specific edges .... regulatory links present in only one network

Node/edge identity is matched CASE-INSENSITIVELY, because the pipeline cases node
IDs differently between networks (e.g. `Nitrate_Supply` vs `NITRATE_SUPPLY`,
`CN_Ratio` vs `CN_RATIO`); genes such as `HVNRT2_1` already match exactly.

Usage:
    python Agent/shared/network_to_compare.py <netA> <netB> [--out PATH]

<netA>/<netB> may be either a full path to a trait directory (containing
network/network.json) or a bare trait folder name under ./networks/.
Read-only: writes only the output HTML.
"""

import argparse
import html
import json
import sys
from pathlib import Path

try:
    from flashp_version import get_version
except Exception:  # pragma: no cover - version banner is cosmetic
    def get_version():
        return "light-1.0"


# ---- loading -------------------------------------------------------------

def resolve_network_dir(arg: str) -> Path:
    """Accept a full trait-dir path or a bare name under ./networks/."""
    p = Path(arg)
    candidates = [p, Path.cwd() / p, Path.cwd() / "networks" / arg]
    for c in candidates:
        if (c / "network" / "network.json").exists():
            return c
    raise FileNotFoundError(
        f"No network/network.json found for '{arg}' "
        f"(looked in: {', '.join(str(c) for c in candidates)})")


def load_network(net_dir: Path) -> dict:
    net = json.loads((net_dir / "network" / "network.json").read_text(encoding="utf-8"))
    meta = net.get("metadata", {})
    acc_path = net_dir / "validation" / "accuracy_metrics.json"
    acc = json.loads(acc_path.read_text(encoding="utf-8")) if acc_path.exists() else {}
    return {
        "dir": net_dir,
        "slug": net_dir.name,
        "phenotype": meta.get("phenotype", net_dir.name),
        "species": meta.get("species", ""),
        "nodes": net.get("nodes", []),
        "edges": net.get("edges", []),
        "acc": acc,
    }


# ---- comparison ----------------------------------------------------------

def norm(s: str) -> str:
    return (s or "").strip().lower()


def best_method(acc: dict):
    """Return (method_key, metrics) with the highest accuracy, or (None, None)."""
    best_k, best_m, best_score = None, None, -1.0
    for k in ("ode", "rwr", "algebraic"):
        m = acc.get(k)
        if not isinstance(m, dict):
            continue
        a = m.get("accuracy")
        if a is None:
            continue
        if a <= 1:
            a = a * 100
        if a > best_score:
            best_k, best_m, best_score = k, m, a
    return best_k, best_m


def acc_pct(m: dict):
    if not m or m.get("accuracy") is None:
        return None
    a = m["accuracy"]
    return round((a * 100 if a <= 1 else a), 1)


def compare(a: dict, b: dict) -> dict:
    a_nodes = {norm(n["id"]): n for n in a["nodes"]}
    b_nodes = {norm(n["id"]): n for n in b["nodes"]}
    shared_keys = sorted(set(a_nodes) & set(b_nodes))
    only_a = sorted(set(a_nodes) - set(b_nodes))
    only_b = sorted(set(b_nodes) - set(a_nodes))

    def edge_map(net):
        m = {}
        for e in net["edges"]:
            m[(norm(e["s"]), norm(e["t"]))] = e
        return m

    ea, eb = edge_map(a), edge_map(b)
    conserved, conflict, edge_only_a, edge_only_b = [], [], [], []
    for key, e in ea.items():
        if key in eb:
            other = eb[key]
            row = {"s": e["s"], "t": e["t"], "xa": e["x"], "xb": other["x"],
                   "da": e.get("d"), "db": other.get("d")}
            (conserved if e["x"] == other["x"] else conflict).append(row)
        else:
            edge_only_a.append(e)
    for key, e in eb.items():
        if key not in ea:
            edge_only_b.append(e)

    return {
        "shared_nodes": [a_nodes[k] for k in shared_keys],
        "only_a_nodes": [a_nodes[k] for k in only_a],
        "only_b_nodes": [b_nodes[k] for k in only_b],
        "conserved": sorted(conserved, key=lambda r: (r["s"], r["t"])),
        "conflict": sorted(conflict, key=lambda r: (r["s"], r["t"])),
        "edge_only_a": sorted(edge_only_a, key=lambda e: (e["s"], e["t"])),
        "edge_only_b": sorted(edge_only_b, key=lambda e: (e["s"], e["t"])),
    }


# ---- HTML rendering ------------------------------------------------------

SIGN_TXT = {1: "activates", -1: "inhibits"}
SIGN_ARROW = {1: "&rarr;", -1: "&#8867;"}  # -> and inhibition tack


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def doi_link(d):
    if not d:
        return "<span class='muted'>&mdash;</span>"
    d = str(d)
    return f"<a href='https://doi.org/{esc(d)}' target='_blank' rel='noopener'>{esc(d)}</a>"


def sign_badge(x):
    cls = "pos" if x > 0 else "neg"
    return f"<span class='sign {cls}'>{SIGN_ARROW.get(x, '?')} {SIGN_TXT.get(x, '?')}</span>"


def metrics_row(label, m):
    if not m:
        return f"<tr><td>{esc(label)}</td><td colspan='4' class='muted'>not available</td></tr>"
    ap = acc_pct(m)
    k = m.get("kappa")
    mcc = m.get("mcc")
    conv = m.get("convergence_rate")
    return (
        f"<tr><td>{esc(label)}</td>"
        f"<td class='num'>{ap if ap is not None else '&mdash;'}%</td>"
        f"<td class='num'>{round(k, 3) if isinstance(k, (int, float)) else '&mdash;'}</td>"
        f"<td class='num'>{round(mcc, 3) if isinstance(mcc, (int, float)) else '&mdash;'}</td>"
        f"<td class='num'>{round(conv * 100) if isinstance(conv, (int, float)) else '&mdash;'}%</td></tr>"
    )


def render(a: dict, b: dict, cmp: dict) -> str:
    na, nb = len(a["nodes"]), len(b["nodes"])
    ma, mb = len(a["edges"]), len(b["edges"])
    bk_a, bm_a = best_method(a["acc"])
    bk_b, bm_b = best_method(b["acc"])
    ml = {"ode": "ODE (Hill)", "rwr": "RWR", "algebraic": "Algebraic"}

    a_name = f"{esc(a['phenotype'])}"
    b_name = f"{esc(b['phenotype'])}"
    a_sp = esc(a["species"]) or esc(a["slug"])
    b_sp = esc(b["species"]) or esc(b["slug"])

    def node_type(n):
        return esc(n.get("ty", ""))

    # conserved / conflict / only tables
    def edge_rows_shared(rows, conflict=False):
        out = []
        for r in rows:
            if conflict:
                cells = (
                    f"<td>{esc(r['s'])} {SIGN_ARROW.get(r['xa'],'?')} {esc(r['t'])}</td>"
                    f"<td>{sign_badge(r['xa'])}</td>"
                    f"<td>{sign_badge(r['xb'])}</td>"
                    f"<td>{doi_link(r['da'])}</td><td>{doi_link(r['db'])}</td>"
                )
            else:
                cells = (
                    f"<td>{esc(r['s'])} {esc(r['t'])}</td>"
                    f"<td>{sign_badge(r['xa'])}</td>"
                    f"<td>{doi_link(r['da'])}</td><td>{doi_link(r['db'])}</td>"
                )
            out.append(f"<tr>{cells}</tr>")
        return "\n".join(out)

    def edge_rows_only(edges):
        return "\n".join(
            f"<tr><td>{esc(e['s'])}</td><td>{sign_badge(e['x'])}</td>"
            f"<td>{esc(e['t'])}</td><td>{doi_link(e.get('d'))}</td></tr>"
            for e in edges)

    def node_list(nodes):
        if not nodes:
            return "<li class='muted'>none</li>"
        return "\n".join(
            f"<li><b>{esc(n['id'])}</b> <span class='ntype'>{node_type(n)}</span>"
            + (f"<span class='nfn'>{esc(n.get('fn'))}</span>" if n.get("fn") else "")
            + "</li>"
            for n in nodes)

    n_shared = len(cmp["shared_nodes"])
    n_cons = len(cmp["conserved"])
    n_conf = len(cmp["conflict"])

    apct_a = acc_pct(bm_a)
    apct_b = acc_pct(bm_b)

    style = """
    :root{--bg:#f7f8fa;--fg:#1a1d24;--muted:#7a8290;--card:#fff;--line:#e4e7ec;
      --a:#2563eb;--b:#d97706;--pos:#16a34a;--neg:#dc2626;--conf:#b91c1c;--consbg:#f0fdf4;--confbg:#fef2f2;}
    @media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ef;--muted:#8b93a1;
      --card:#171b21;--line:#262c36;--consbg:#0f1f16;--confbg:#211215;}}
    :root[data-theme=dark]{--bg:#0f1216;--fg:#e6e9ef;--muted:#8b93a1;--card:#171b21;--line:#262c36;--consbg:#0f1f16;--confbg:#211215;}
    :root[data-theme=light]{--bg:#f7f8fa;--fg:#1a1d24;--muted:#7a8290;--card:#fff;--line:#e4e7ec;--consbg:#f0fdf4;--confbg:#fef2f2;}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--fg);
      font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:28px;}
    h1{font-size:22px;margin:0 0 4px}
    h2{font-size:16px;margin:34px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line);}
    .sub{color:var(--muted);margin:0 0 22px}
    .vs{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}
    .side{flex:1;min-width:240px;background:var(--card);border:1px solid var(--line);
      border-radius:12px;padding:16px;}
    .side.a{border-top:3px solid var(--a)} .side.b{border-top:3px solid var(--b)}
    .side h3{margin:0 0 2px;font-size:15px} .side .sp{color:var(--muted);font-style:italic;font-size:13px}
    .stat{display:flex;gap:18px;margin-top:12px;flex-wrap:wrap}
    .stat div{font-size:13px;color:var(--muted)} .stat b{display:block;font-size:20px;color:var(--fg)}
    .cards{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 4px}
    .kpi{flex:1;min-width:120px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
    .kpi .big{font-size:26px;font-weight:700} .kpi .lbl{color:var(--muted);font-size:12px;margin-top:2px}
    table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
      border-radius:10px;overflow:hidden;font-size:13.5px}
    th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line);vertical-align:top}
    th{background:color-mix(in srgb,var(--card) 70%,var(--line));font-weight:600;font-size:12px;
      text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
    tr:last-child td{border-bottom:none}
    td.num{text-align:right;font-variant-numeric:tabular-nums}
    .sign{font-size:12px;font-weight:600;white-space:nowrap}
    .sign.pos{color:var(--pos)} .sign.neg{color:var(--neg)}
    .muted{color:var(--muted)}
    .conf-tbl tr td{background:var(--confbg)}
    .cons-tbl tr td{background:var(--consbg)}
    .twocol{display:flex;gap:14px;flex-wrap:wrap}
    .twocol>div{flex:1;min-width:260px}
    ul.nodes{list-style:none;margin:6px 0;padding:0;columns:1}
    ul.nodes li{padding:5px 0;border-bottom:1px solid var(--line)}
    .ntype{font-size:10px;background:var(--line);border-radius:4px;padding:1px 5px;margin-left:6px;color:var(--muted)}
    .nfn{display:block;color:var(--muted);font-size:12px;margin-top:1px}
    a{color:var(--a);text-decoration:none} a:hover{text-decoration:underline}
    .empty{color:var(--muted);padding:10px 2px}
    .foot{margin-top:34px;color:var(--muted);font-size:12px}
    .legend{font-size:12.5px;color:var(--muted);margin:-4px 0 12px}
    """

    def section_shared_edges():
        if not cmp["conserved"]:
            return "<p class='empty'>No regulatory links are shared between the two networks.</p>"
        return f"""<table class='cons-tbl'><thead><tr>
          <th>Interaction</th><th>Regulation (both)</th>
          <th>DOI &mdash; {a_sp}</th><th>DOI &mdash; {b_sp}</th></tr></thead>
          <tbody>{edge_rows_shared(cmp['conserved'])}</tbody></table>"""

    def section_conflict():
        if not cmp["conflict"]:
            return ("<p class='empty'>No sign conflicts &mdash; every shared interaction has the "
                    "same direction in both networks.</p>")
        return f"""<p class='legend'>Same source&rarr;target link, but the regulatory sign is
          <b>opposite</b> between the two networks &mdash; a genuine mechanistic divergence to inspect.</p>
          <table class='conf-tbl'><thead><tr>
          <th>Interaction</th><th>{a_sp}</th><th>{b_sp}</th>
          <th>DOI &mdash; {a_sp}</th><th>DOI &mdash; {b_sp}</th></tr></thead>
          <tbody>{edge_rows_shared(cmp['conflict'], conflict=True)}</tbody></table>"""

    def only_edges_block():
        def tbl(edges, sp):
            if not edges:
                return f"<p class='empty'>No links unique to {sp}.</p>"
            return f"""<table><thead><tr><th>Source</th><th>Reg.</th><th>Target</th><th>DOI</th></tr></thead>
              <tbody>{edge_rows_only(edges)}</tbody></table>"""
        return f"""<div class='twocol'>
          <div><h3 style='font-size:14px'>Only in {a_sp}</h3>{tbl(cmp['edge_only_a'], a_sp)}</div>
          <div><h3 style='font-size:14px'>Only in {b_sp}</h3>{tbl(cmp['edge_only_b'], b_sp)}</div></div>"""

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Compare: {a_name} vs {b_name}</title><style>{style}</style></head><body>
<h1>Network comparison</h1>
<p class='sub'>{a_name} <span class='muted'>vs</span> {b_name}
 &middot; matched case-insensitively &middot; Flash-P v{esc(get_version())}</p>

<div class='vs'>
  <div class='side a'><h3>{a_name}</h3><div class='sp'>{a_sp}</div>
    <div class='stat'><div><b>{na}</b>nodes</div><div><b>{ma}</b>edges</div>
      <div><b>{apct_a if apct_a is not None else '&mdash;'}%</b>{esc(ml.get(bk_a,'&mdash;'))}</div></div></div>
  <div class='side b'><h3>{b_name}</h3><div class='sp'>{b_sp}</div>
    <div class='stat'><div><b>{nb}</b>nodes</div><div><b>{mb}</b>edges</div>
      <div><b>{apct_b if apct_b is not None else '&mdash;'}%</b>{esc(ml.get(bk_b,'&mdash;'))}</div></div></div>
</div>

<div class='cards'>
  <div class='kpi'><div class='big'>{n_shared}</div><div class='lbl'>shared nodes</div></div>
  <div class='kpi'><div class='big'>{n_cons}</div><div class='lbl'>conserved interactions</div></div>
  <div class='kpi'><div class='big' style='color:var(--conf)'>{n_conf}</div><div class='lbl'>sign conflicts</div></div>
  <div class='kpi'><div class='big'>{len(cmp['only_a_nodes'])} / {len(cmp['only_b_nodes'])}</div>
    <div class='lbl'>nodes unique to A / B</div></div>
</div>

<h2>Validation accuracy</h2>
<table><thead><tr><th>Method</th><th class='num'>Accuracy</th><th class='num'>&kappa;</th>
  <th class='num'>MCC</th><th class='num'>Converged</th></tr></thead><tbody>
  <tr><td colspan='5' style='background:color-mix(in srgb,var(--a) 12%,var(--card));font-weight:600'>{a_name} &mdash; {a_sp}</td></tr>
  {metrics_row('ODE (Hill)', a['acc'].get('ode'))}
  {metrics_row('RWR', a['acc'].get('rwr'))}
  {metrics_row('Algebraic', a['acc'].get('algebraic'))}
  <tr><td colspan='5' style='background:color-mix(in srgb,var(--b) 12%,var(--card));font-weight:600'>{b_name} &mdash; {b_sp}</td></tr>
  {metrics_row('ODE (Hill)', b['acc'].get('ode'))}
  {metrics_row('RWR', b['acc'].get('rwr'))}
  {metrics_row('Algebraic', b['acc'].get('algebraic'))}
</tbody></table>

<h2>Conserved mechanisms <span class='muted' style='font-weight:400;font-size:13px'>({n_cons} shared interactions, same sign)</span></h2>
{section_shared_edges()}

<h2>Divergent regulation <span class='muted' style='font-weight:400;font-size:13px'>({n_conf} sign conflicts)</span></h2>
{section_conflict()}

<h2>Species-specific nodes</h2>
<div class='twocol'>
  <div><h3 style='font-size:14px'>Only in {a_sp} ({len(cmp['only_a_nodes'])})</h3>
    <ul class='nodes'>{node_list(cmp['only_a_nodes'])}</ul></div>
  <div><h3 style='font-size:14px'>Only in {b_sp} ({len(cmp['only_b_nodes'])})</h3>
    <ul class='nodes'>{node_list(cmp['only_b_nodes'])}</ul></div>
</div>

<h2>Species-specific interactions</h2>
{only_edges_block()}

<p class='foot'>Generated by <code>network_to_compare.py</code>. Read-only report &mdash;
 no pipeline files were modified. Node identity matched by lower-cased ID.</p>
</body></html>"""


# ---- main ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Compare two FLASH-P networks into one HTML report.")
    ap.add_argument("netA", help="First network: trait-dir path or bare name under ./networks/")
    ap.add_argument("netB", help="Second network: trait-dir path or bare name under ./networks/")
    ap.add_argument("--out", help="Output HTML path (default: networks/Compare_<A>_vs_<B>.html)")
    args = ap.parse_args()

    try:
        dir_a = resolve_network_dir(args.netA)
        dir_b = resolve_network_dir(args.netB)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    a = load_network(dir_a)
    b = load_network(dir_b)
    cmp = compare(a, b)

    if args.out:
        out = Path(args.out)
    else:
        parent = Path.cwd() / "networks"
        parent.mkdir(exist_ok=True)
        out = parent / f"Compare_{a['slug']}_vs_{b['slug']}.html"
    out.write_text(render(a, b, cmp), encoding="utf-8")

    print(f"\nFlash-P v{get_version()} - network comparison")
    print(f"  A: {a['phenotype']}  ({a['species']})  {len(a['nodes'])} nodes / {len(a['edges'])} edges")
    print(f"  B: {b['phenotype']}  ({b['species']})  {len(b['nodes'])} nodes / {len(b['edges'])} edges")
    print(f"  shared nodes: {len(cmp['shared_nodes'])}  |  conserved edges: {len(cmp['conserved'])}"
          f"  |  sign conflicts: {len(cmp['conflict'])}")
    print(f"  nodes unique to A: {len(cmp['only_a_nodes'])}  |  unique to B: {len(cmp['only_b_nodes'])}")
    print(f"\n  Report saved: {out}")
    print("  Open it by double-click.\nDone!")


if __name__ == "__main__":
    main()

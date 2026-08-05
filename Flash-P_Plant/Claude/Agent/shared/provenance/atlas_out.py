"""
Export a verified network as an atlas contribution.

Once a network's claims are verified and grounded, it is the same kind of object the
public atlas is made of — grouped edges, supporting quotes, papers with abstracts and
open-access full text. So it should be able to *become* atlas: build a network, and
what you get back is a contribution the atlas can absorb.

Two artefacts, one for each consumer:

  ``<to_id>.json``  the exact shape ``Flash-P_DataBase/scripts/export_edges_web.py``
                    writes into the website's ``public/trait-networks/atlas/edges/``.
                    Dropping it there renders with no code changes — that round trip
                    is the test that the format is really compatible, not merely
                    similar.
  ``rows.jsonl``    one JSON object per line, keyed to ``atlas.db``'s own columns and
                    UNIQUE constraints, so ingestion is an upsert rather than a
                    rewrite. Every row carries ``extractor: "flash-p"`` — a
                    contributed claim should always be attributable to how it was made.

Only ``verified`` and ``repaired`` records are exported. Quarantined ones stay visible
locally, in the Studio, where they are useful as "here is what we could not confirm" —
but they are not evidence and must never enter a shared atlas.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from .litapi import doi_slug
except ImportError:                                   # run directly as a script
    from litapi import doi_slug

__all__ = ["export_atlas", "NODE_KIND"]

# FLASH-P node types are already the atlas's short codes; this is the same mapping
# export_edges_web.py uses to colour the knowledge graph.
NODE_KIND = {"G": "gene", "H": "hormone", "M": "metabolite", "E": "environment",
             "PC": "complex", "R": "rna", "P": "phenotype", "PR": "process"}

GRAPH_MAX = 240      # edges drawn in the knowledge graph
HUB_COUNT = 55       # nodes counted as hubs, by degree

EXPORTABLE = ("verified", "repaired")


def _trait_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (name or "trait")).strip("_")


def _load(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _node_types(net_dir: str) -> Dict[str, str]:
    """node -> FLASH-P short type code, from curated_edges.json's `nodes` map."""
    cur = _load(os.path.join(net_dir, "data", "curated_edges.json")) or {}
    out = {}
    for name, ty in (cur.get("nodes") or {}).items():
        out[name.upper()] = str(ty)
    return out


def _group_edges(rows: List[dict], types: Dict[str, str],
                 species: str) -> List[dict]:
    """Group by (source, target, sign) with every supporting paper listed.

    The atlas groups because one relationship usually has several papers behind it and
    the drawer lists them all. FLASH-P Light keeps one DOI per edge, so most groups
    have a single paper — but the shape has to be right, or a contribution cannot merge
    with rows that do have several.
    """
    grouped: Dict[Tuple[str, str, int], dict] = {}
    for r in rows:
        if r.get("verification") not in EXPORTABLE or not r.get("doi"):
            continue
        s, t = r.get("s", ""), r.get("t", "")
        sign = 1 if int(r.get("x", 0) or 0) >= 0 else -1
        key = (s.upper(), t.upper(), sign)
        g = grouped.get(key)
        if g is None:
            g = grouped[key] = {
                "source": s, "source_type": types.get(s.upper(), "G"),
                "target": t, "target_type": types.get(t.upper(), "G"),
                "sign": sign,
                # FLASH-P records direction, not mechanism; say what we know rather
                # than inventing a mechanism string the evidence does not support.
                "mechanism": "activation" if sign > 0 else "inhibition",
                "species": set(), "papers": [], "conflict": False,
            }
        if species:
            g["species"].add(species)
        g["papers"].append({
            "doi": r["doi"],
            "doi_url": f"https://doi.org/{r['doi']}",
            "evidence": r.get("evidence", ""),
            "source_locator": r.get("source_locator") or "abstract",
            "confidence": round(float(r.get("confidence") or 0.0), 3),
        })

    # An edge asserted in both directions of sign is a genuine conflict in the
    # literature; the atlas flags it rather than silently keeping one side.
    signs: Dict[Tuple[str, str], set] = {}
    for (s, t, sign) in grouped:
        signs.setdefault((s, t), set()).add(sign)
    for (s, t, sign), g in grouped.items():
        if len(signs[(s, t)]) > 1:
            g["conflict"] = True

    edges = []
    for g in grouped.values():
        g["species"] = sorted(g["species"])
        g["n_papers"] = len({p["doi"] for p in g["papers"]})
        edges.append(g)
    edges.sort(key=lambda e: e["n_papers"], reverse=True)
    return edges


def _graph_core(edges: List[dict]) -> Tuple[List[dict], List[dict], Dict[str, str]]:
    """Readable core of the knowledge graph — the table below it keeps every edge."""
    node_kind: Dict[str, str] = {}
    for e in edges:
        node_kind.setdefault(e["source"], NODE_KIND.get(e["source_type"], "gene"))
        node_kind.setdefault(e["target"], NODE_KIND.get(e["target_type"], "gene"))

    deg: Dict[str, int] = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    hubs = {n for n, _ in sorted(deg.items(), key=lambda kv: kv[1], reverse=True)[:HUB_COUNT]}

    core = [e for e in edges
            if e["n_papers"] >= 2 or (e["source"] in hubs and e["target"] in hubs)]
    core.sort(key=lambda e: (e["n_papers"], deg.get(e["source"], 0) + deg.get(e["target"], 0)),
              reverse=True)
    core = core[:GRAPH_MAX]

    ids = {e["source"] for e in core} | {e["target"] for e in core}
    gnodes = [{"id": n, "kind": node_kind[n]} for n in sorted(ids)]
    gedges = [{"source": e["source"], "target": e["target"], "sign": e["sign"],
               "n_papers": e["n_papers"]} for e in core]
    return gnodes, gedges, node_kind


def export_atlas(net_dir: str, to_id: str = "", label: str = "",
                 out_dir: str = "") -> Dict[str, Any]:
    """Write the contribution bundle. Returns a summary dict."""
    ev = _load(os.path.join(net_dir, "data", "evidence.json"))
    if ev is None:
        raise SystemExit(
            f"no data/evidence.json in {net_dir} — verify the network first:\n"
            f"  python Agent/shared/verify_evidence.py {net_dir}")

    meta = ev.get("metadata", {}) or {}
    phenotype = meta.get("phenotype", "") or "trait"
    species = meta.get("species", "") or ""
    trait_key = phenotype.strip().lower()
    slug = _trait_slug(phenotype)
    to_id = to_id or slug
    label = label or phenotype

    out_dir = out_dir or os.path.join(net_dir, "atlas")
    os.makedirs(out_dir, exist_ok=True)

    types = _node_types(net_dir)
    edges = _group_edges(ev.get("edges", []), types, species)
    gnodes, gedges, node_kind = _graph_core(edges)

    # papers map — copy full texts alongside, using the website's URL convention
    dois = sorted({p["doi"] for e in edges for p in e["papers"]})
    pert_rows = [r for r in ev.get("perturbations", [])
                 if r.get("verification") in EXPORTABLE and r.get("doi")]
    dois = sorted(set(dois) | {r["doi"] for r in pert_rows})

    ft_dir = os.path.join(out_dir, "fulltext")
    src_ft = os.path.join(net_dir, "data", "fulltext")
    papers: Dict[str, dict] = {}
    copied = 0
    for doi in dois:
        p = (ev.get("papers") or {}).get(doi, {})
        has_ft = False
        name = f"{doi_slug(doi)}.txt"
        src = os.path.join(src_ft, name)
        if p.get("has_fulltext") and os.path.isfile(src):
            os.makedirs(ft_dir, exist_ok=True)
            with open(src, encoding="utf-8") as f_in, \
                 open(os.path.join(ft_dir, name), "w", encoding="utf-8") as f_out:
                f_out.write(f_in.read())
            has_ft = True
            copied += 1
        papers[doi] = {
            "title": p.get("title", ""), "year": p.get("year"),
            "journal": p.get("journal", ""), "abstract": p.get("abstract", ""),
            "oa_status": p.get("oa_status", ""), "has_fulltext": has_ft,
            "fulltext_url": f"/trait-networks/atlas/fulltext/{name}" if has_ft else "",
        }

    summary = ev.get("summary", {}) or {}
    payload = {
        "trait": {"to_id": to_id, "label": label, "trait_key": trait_key},
        # FLASH-P builds from targeted searches rather than a screening sweep, so
        # papers_screened is the number actually read, not a corpus size.
        "coverage": {"papers_screened": summary.get("papers", 0), "searches_run": 0},
        "summary": {"edges": len(edges),
                    "edge_rows": sum(len(e["papers"]) for e in edges),
                    "papers": len(dois), "nodes": len(node_kind),
                    "graph_edges": len(gedges), "graph_nodes": len(gnodes)},
        "story": "",
        "graph": {"nodes": gnodes, "edges": gedges},
        "edges": edges,
        "papers": papers,
        "provenance": {
            "built_by": "FLASH-P",
            "flash_p_version": meta.get("flash_p_version", ""),
            "verified_at": meta.get("verified_at", ""),
            "grounding": meta.get("grounding", ""),
            "sources": meta.get("sources", []),
        },
    }
    edges_path = os.path.join(out_dir, f"{to_id}.json")
    with open(edges_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    rows_path = os.path.join(out_dir, "rows.jsonl")
    n_rows = _write_rows(rows_path, ev, edges, pert_rows, papers, types,
                         trait_key, to_id, phenotype, species)

    # A catalog fragment so the website's index can pick the trait up on merge.
    with open(os.path.join(out_dir, "catalog_entry.json"), "w", encoding="utf-8") as f:
        json.dump({"to_id": to_id, "label": label, "trait_key": trait_key,
                   "edges": len(edges), "papers": len(dois),
                   "file": f"edges/{to_id}.json"}, f, ensure_ascii=False, indent=2)

    return {"out_dir": out_dir, "edges": len(edges), "papers": len(dois),
            "perturbations": len(pert_rows), "fulltexts": copied, "rows": n_rows,
            "edges_file": edges_path, "rows_file": rows_path,
            "excluded": _excluded_counts(ev)}


def _excluded_counts(ev: dict) -> Dict[str, int]:
    """What did NOT make it into the bundle — reported, never silent."""
    out = {"edges": 0, "perturbations": 0}
    for k, key in (("edges", "edges"), ("perturbations", "perturbations")):
        out[k] = sum(1 for r in ev.get(key, [])
                     if r.get("verification") not in EXPORTABLE or not r.get("doi"))
    return out


def _write_rows(path: str, ev: dict, edges: List[dict], pert_rows: List[dict],
                papers: Dict[str, dict], types: Dict[str, str],
                trait_key: str, to_id: str, phenotype: str, species: str) -> int:
    """Merge-ready rows for atlas.db — one JSON object per line, tagged by table.

    Column names and the natural keys mirror atlas.db exactly, so an importer can
    upsert on its existing UNIQUE constraints instead of guessing how to reconcile.
    """
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        def emit(obj):
            nonlocal n
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n += 1

        for doi, p in papers.items():
            emit({"_table": "paper", "doi": doi, "title": p["title"], "year": p["year"],
                  "journal": p["journal"], "licence": "", "oa_status": p["oa_status"],
                  "abstract": p["abstract"]})

        for e in edges:
            for p in e["papers"]:
                emit({"_table": "edge",
                      "source": e["source"], "source_canonical": e["source"].upper(),
                      "source_type": e["source_type"],
                      "target": e["target"], "target_canonical": e["target"].upper(),
                      "target_type": e["target_type"],
                      "sign": e["sign"], "mechanism": e["mechanism"],
                      "species": species, "trait_text": phenotype, "to_id": to_id,
                      "trait_key": trait_key, "doi": p["doi"],
                      "evidence_sentence": p["evidence"],
                      "prov_source": ("full_text" if p["source_locator"].startswith("full_text")
                                      else "abstract"),
                      "source_locator": p["source_locator"],
                      "extractor": "flash-p", "confidence": p["confidence"],
                      "verification_status": "verified", "verification_reason": "",
                      "judge_model": "", "judge_confidence": None,
                      "conflict_flag": 1 if e["conflict"] else 0})

        for r in pert_rows:
            emit({"_table": "perturbation",
                  "test_id": r.get("id", ""),
                  "gene_raw": r.get("g", ""), "gene_canonical_id": r.get("g", "").upper(),
                  "ortholog_group": "",
                  "species": r.get("sp") or species, "species_taxid": "",
                  "perturbation_type": r.get("pt", ""), "direction": r.get("ed", ""),
                  "comparison_baseline": "WT", "background": "WT",
                  "trait_text": phenotype, "to_id": to_id, "trait_key": trait_key,
                  "doi": r["doi"], "evidence_sentence": r.get("evidence", ""),
                  "source_type": "oa", "extractor": "flash-p",
                  "confidence": round(float(r.get("confidence") or 0.0), 3),
                  "verified": 1, "conflict_flag": 0,
                  "verification_status": "verified", "verification_reason": "",
                  "source_locator": r.get("source_locator") or "abstract",
                  "judge_model": "", "judge_confidence": None,
                  "concept_id": "", "measure": ""})
    return n

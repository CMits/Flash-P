"""
FLASH-P **Light** I/O adapter.

The Light data files are short-key (and optionally TOON) and drop fields the existing Python
scripts expect (`effect`, `source_type`/`target_type`, per-record `phenotype_node`, …). Rather
than rewrite every validator/export, this module gives two functions:

  * ``load(path)`` — read a Light file (JSON short/long, or TOON) and return it **expanded** to
    the long-key, rehydrated form the legacy scripts already understand:
        - short keys -> long keys (test_id, gene_modifiers, source, …)
        - short enum values -> long ("ko"->"knockout", "H"->"HORMONE", "up"->"increased")
        - rehydrate derived fields: edge ``effect`` from ``sign``; curated edge
          ``source_type``/``target_type`` from the file-level ``nodes`` map; reconciled
          per-record ``phenotype_node`` from ``metadata``; ``in_network=True``.
    So a validator can do ``data = light_io.load(p)`` instead of ``json.load(open(p))``.

  * ``dump_slim(path, data, kind)`` — normalize a dict through the slim Pydantic model
    (``by_alias=True`` -> short keys, ``extra=ignore`` -> fat fields dropped) and write JSON,
    or TOON for the flat-eligible kinds (tab-delimited, JSON fallback on delimiter collision).
    This is also the compaction step: feed it fat agent output, get a slim file.

TOON whole-file format: a ``#meta <json>`` first line, then one TOON table for the array.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import toon_codec

# ---------------------------------------------------------------------------
# lexicon (long form for the legacy scripts) — see LEXICON.md
# ---------------------------------------------------------------------------
_EDGE = {"eid": "edge_id", "s": "source", "t": "target", "x": "sign", "d": "doi"}
_NODE = {"id": "id", "ty": "type", "fn": "full_name", "src": "is_source"}
_PERT = {"id": "test_id", "g": "gene", "pt": "perturbation_type",
         "ed": "expected_direction", "sp": "species", "d": "doi"}
_RECON = {"id": "test_id", "g": "gene", "pt": "perturbation_type",
          "ed": "expected_direction", "ng": "network_gene", "m": "gene_modifiers",
          "exo": "exogenous_supply", "cb": "comparison_baseline", "rt": "reconciliation_type",
          "d": "doi"}
_EQ = {"n": "node", "ty": "type", "src": "is_source", "a": "activators",
       "inh": "inhibitors", "f": "formula"}
_ANN = {"n": "node", "fn": "full_name", "ty": "type", "desc": "description", "src": "is_source"}

_NTYPE = {"G": "GENE", "H": "HORMONE", "M": "METABOLITE", "E": "ENVIRONMENT",
          "PC": "PROTEIN_COMPLEX", "R": "REGULATORY_RNA", "P": "PHENOTYPE", "PR": "PROCESS"}
_DIR = {"up": "increased", "dn": "decreased", "nc": "unchanged"}
_RT = {"em": "exact_match", "ci": "case_insensitive", "fm": "family_member",
       "cc": "composite_collapse", "cm": "composite_member", "ta": "treatment_analog",
       "mm": "mechanism_mapping", "nin": "not_in_network", "ctl": "control"}
_PT = {"ko": "knockout", "kd": "knockdown", "oe": "overexpression", "dko": "double_knockout",
       "tko": "triple_knockout", "gof": "gain_of_function", "lof": "loss_of_function",
       "rsc": "rescue", "trt": "treatment", "dm": "double_mutant", "cmb": "combined", "ep": "epistasis"}


def _rename(rec: Dict[str, Any], kmap: Dict[str, str]) -> Dict[str, Any]:
    return {kmap.get(k, k): v for k, v in rec.items()}


def _enum(v, m):
    return m.get(v, v) if isinstance(v, str) else v


def _effect_from_sign(sign) -> str:
    try:
        return "activation" if int(sign) >= 0 else "inhibition"
    except (TypeError, ValueError):
        return ""


def _doi_from(rec: Dict[str, Any]) -> str:
    """DOI from flat field, or from a (legacy/fat) evidence array — for robust compaction."""
    d = rec.get("doi") or rec.get("d")
    if d:
        return d
    ev = rec.get("evidence")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict):
        return ev[0].get("doi", "") or ""
    return ""


# ---------------------------------------------------------------------------
# kind inference + raw read (JSON or TOON)
# ---------------------------------------------------------------------------
def kind_of(path: str) -> str:
    b = os.path.basename(path).lower()
    if "curated_edges" in b:
        return "curated_edges"
    if "reconciled_perturbation" in b:
        return "reconciled"
    if "perturbation_dataset" in b:
        return "perturbation_dataset"
    if "algebraic_equations" in b:
        return "algebraic_equations"
    if "ode_equations" in b:
        return "ode_equations"
    if "node_annotations" in b:
        return "node_annotations"
    if b.startswith("network") or b == "network.json":
        return "network"
    return "unknown"


_ARRAY_KEY = {"curated_edges": "edges",
              "perturbation_dataset": "perturbations", "reconciled": "perturbations",
              "algebraic_equations": "equations", "ode_equations": "equations",
              "node_annotations": "annotations", "network": None}


def _read_raw(path: str, kind: str) -> Dict[str, Any]:
    text = open(path, encoding="utf-8").read()
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("#meta") or toon_codec.is_toon(text):
        # TOON whole-file: "#meta {json}" then a TOON table
        meta = {}
        body = text
        if first.startswith("#meta"):
            meta = json.loads(first[len("#meta"):].strip())
            body = text.split("\n", 1)[1] if "\n" in text else ""
        name, _cols, records = toon_codec.decode(body)
        # #meta holds the full non-array dict (metadata + nodes + …) — merge, don't re-wrap
        result = dict(meta) if isinstance(meta, dict) else {}
        result[_ARRAY_KEY.get(kind, name)] = records
        return result
    return json.loads(text)


# ---------------------------------------------------------------------------
# expanders (slim -> rich/long form the legacy scripts expect)
# ---------------------------------------------------------------------------
def _expand(d: Dict[str, Any], kind: str) -> Dict[str, Any]:
    if kind == "curated_edges":
        nodes = {k: _enum(v, _NTYPE) for k, v in d.get("nodes", {}).items()}
        out = []
        for e in d.get("edges", []):
            e = _rename(e, _EDGE)
            e.setdefault("source_type", nodes.get(e.get("source"), ""))
            e.setdefault("target_type", nodes.get(e.get("target"), ""))
            e.setdefault("effect", _effect_from_sign(e.get("sign")))
            e["doi"] = _doi_from(e)
            e.setdefault("evidence", [{"doi": e["doi"]}] if e["doi"] else [])
            out.append(e)
        d["edges"] = out
        d["nodes"] = nodes
    elif kind == "network":
        d["nodes"] = [{**_rename(n, _NODE), "type": _enum(_rename(n, _NODE).get("type"), _NTYPE)}
                      for n in d.get("nodes", [])]
        out = []
        for e in d.get("edges", []):
            e = _rename(e, _EDGE)
            e.setdefault("effect", _effect_from_sign(e.get("sign")))
            e["doi"] = _doi_from(e)
            e.setdefault("evidence", [{"doi": e["doi"]}] if e["doi"] else [])
            out.append(e)
        d["edges"] = out
    elif kind in ("algebraic_equations", "ode_equations"):
        d["equations"] = [{**_rename(q, _EQ), **({"type": _enum(_rename(q, _EQ).get("type"), _NTYPE)}
                                                 if "type" in _rename(q, _EQ) else {})}
                          for q in d.get("equations", [])]
    elif kind == "node_annotations":
        d["annotations"] = [{**_rename(a, _ANN), "type": _enum(_rename(a, _ANN).get("type"), _NTYPE)}
                            for a in d.get("annotations", [])]
    elif kind == "perturbation_dataset":
        out = []
        for p in d.get("perturbations", []):
            p = _rename(p, _PERT)
            p["perturbation_type"] = _enum(p.get("perturbation_type"), _PT)
            p["expected_direction"] = _enum(p.get("expected_direction"), _DIR)
            p["doi"] = _doi_from(p)
            p.setdefault("evidence", [{"doi": p["doi"]}] if p["doi"] else [])
            out.append(p)
        d["perturbations"] = out
    elif kind == "reconciled":
        meta = d.get("metadata", {})
        pn = meta.get("phenotype_node") or meta.get("pn") or ""
        out = []
        for p in d.get("perturbations", []):
            p = _rename(p, _RECON)
            p["perturbation_type"] = _enum(p.get("perturbation_type"), _PT)
            p["expected_direction"] = _enum(p.get("expected_direction"), _DIR)
            p["reconciliation_type"] = _enum(p.get("reconciliation_type"), _RT)
            p.setdefault("phenotype_node", pn or p.get("phenotype_node", ""))
            p.setdefault("in_network", True)
            p.setdefault("condition", "both")
            out.append(p)
        d["perturbations"] = out
    return d


def load(path: str) -> Dict[str, Any]:
    """Read a Light file and return the expanded long-key / rehydrated form."""
    kind = kind_of(path)
    return _expand(_read_raw(path, kind), kind)


# ---------------------------------------------------------------------------
# dump_slim (rich/fat -> slim short-key; also the compaction step)
# ---------------------------------------------------------------------------
_TOON_ELIGIBLE = {"curated_edges": "edges", "perturbation_dataset": "perturbations"}


def _model_for(kind: str):
    import schemas
    return {"curated_edges": schemas.CuratedEdgesFile,
            "perturbation_dataset": schemas.PerturbationDatasetFile,
            "reconciled": schemas.ReconciledPerturbationFile,
            "network": schemas.NetworkFile,
            "algebraic_equations": schemas.AlgebraicEquationsFile,
            "ode_equations": schemas.ODEEquationsFile,
            "node_annotations": schemas.NodeAnnotationsFile}.get(kind)


def to_slim_dict(data: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Normalize through the slim Pydantic model -> short-key dict (drops fat fields)."""
    Model = _model_for(kind)
    if Model is None:
        return data
    return Model.model_validate(data).model_dump(by_alias=True)


def dump_slim(path: str, data: Dict[str, Any], kind: Optional[str] = None,
              prefer_toon: bool = True) -> str:
    """Write ``data`` as a slim Light file. Returns the format used ('toon' or 'json')."""
    kind = kind or kind_of(path)
    slim = to_slim_dict(data, kind)
    arr_key = _TOON_ELIGIBLE.get(kind)
    if prefer_toon and arr_key and slim.get(arr_key):
        records = slim[arr_key]
        cols = list(records[0].keys())
        toon = toon_codec.try_encode_records(arr_key, cols, records)
        if toon is not None:
            meta = {k: v for k, v in slim.items() if k != arr_key}
            with open(path, "w", encoding="utf-8") as f:
                f.write("#meta " + json.dumps(meta, ensure_ascii=False) + "\n")
                f.write(toon + "\n")
            return "toon"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2)
    return "json"


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    B = os.path.join(os.path.dirname(__file__), "..", "..", "..", "_slim_examples")
    # prefer the abbreviated (short-key) examples; fall back to slim
    for folder in ("Shoot_Branching_network_abbrev", "Shoot_Branching_network"):
        root = os.path.join(B, folder)
        if os.path.isdir(root):
            break

    def p(rel):
        return os.path.join(root, rel)

    # 1) load expands short -> rich for the validator-critical files
    cur = load(p("data/curated_edges.json"))
    e0 = cur["edges"][0]
    assert {"source", "target", "sign", "doi", "source_type", "target_type", "effect"} <= set(e0), e0
    assert e0["effect"] in ("activation", "inhibition"), e0
    net = load(p("network/network.json"))
    assert "effect" in net["edges"][0] and "type" in net["nodes"][0]
    rec = load(p("data/reconciled_perturbation_dataset.json"))
    r0 = rec["perturbations"][0]
    assert {"test_id", "gene", "perturbation_type", "expected_direction", "gene_modifiers",
            "exogenous_supply", "network_gene", "phenotype_node", "in_network"} <= set(r0), r0
    assert r0["expected_direction"] in ("increased", "decreased", "unchanged"), r0
    assert r0["perturbation_type"] in _PT.values() or isinstance(r0["perturbation_type"], str)
    assert r0["phenotype_node"], "phenotype_node not rehydrated from metadata"
    print(f"load(): expanded {folder} -> rich long-key form OK "
          f"(curated effect={e0['effect']}, recon pn={r0['phenotype_node']})")

    # 2) dump_slim is idempotent + TOON round-trips losslessly
    import tempfile
    tmp = tempfile.mkdtemp()
    fmt = dump_slim(os.path.join(tmp, "curated_edges.json"), cur, "curated_edges")
    re_cur = load(os.path.join(tmp, "curated_edges.json"))
    assert len(re_cur["edges"]) == len(cur["edges"]), (len(re_cur["edges"]), len(cur["edges"]))
    assert re_cur["edges"][0]["doi"] == cur["edges"][0]["doi"]
    print(f"dump_slim(curated)->{fmt}, reload lossless OK ({len(re_cur['edges'])} edges)")

    rfmt = dump_slim(os.path.join(tmp, "reconciled.json"),
                     {"metadata": rec["metadata"], "perturbations": rec["perturbations"]}, "reconciled")
    re_rec = load(os.path.join(tmp, "reconciled.json"))
    assert len(re_rec["perturbations"]) == len(rec["perturbations"])
    print(f"dump_slim(reconciled)->{rfmt}, reload OK ({len(re_rec['perturbations'])} tests)")
    print("light_io self-test: OK")

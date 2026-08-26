"""
Build-time generator for ``species_data.json`` — the genus -> {valid epithets} allowlist
that ``species.py`` loads at import time as its fast path.

Not run by the pipeline. Run by hand when the seed genus list changes:

    python Agent/shared/provenance/build_species_data.py

Source: ``litapi.ncbi_taxonomy_species`` — the same NCBI Taxonomy fetch-and-filter
``species.py`` itself calls live for a genus this seed list does not carry (see that
module's docstring). Running this script just pre-warms the common case so an ordinary
pipeline run never pays a network round trip for wheat, tomato, Arabidopsis and the rest
of the genera FLASH-P networks build against every day; a genus missing from GENERA
below is not a dead end, only a live lookup on first use.

No API key required (``litapi`` paces requests under NCBI's anonymous 3 req/s limit).
Network-dependent and meant to be re-run occasionally, not on every pipeline invocation
— the checked-in JSON is what ships.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litapi import ncbi_taxonomy_species                # noqa: E402

OUT = Path(__file__).parent / "species_data.json"

# The genus list this module cares about — plant/crop signaling literature plus the
# handful of animal/microbe genera used as heterologous hosts or comparators. Keep in
# sync with the genus set actually referenced by species.py's _GENUS_ONLY/_COMMON maps;
# add a genus here and re-run this script to pre-warm it — or just leave it out, since
# species.py will look it up live the first time a network needs it anyway.
GENERA = [
    "arabidopsis", "triticum", "aegilops", "hordeum", "oryza", "zea", "sorghum",
    "brachypodium", "setaria", "solanum", "nicotiana", "glycine", "medicago",
    "lotus", "phaseolus", "pisum", "vigna", "cicer", "brassica", "raphanus",
    "camelina", "gossypium", "cucumis", "cucurbita", "citrullus", "vitis", "malus",
    "prunus", "citrus", "populus", "eucalyptus", "pinus", "picea", "physcomitrella",
    "physcomitrium", "marchantia", "chlamydomonas", "selaginella", "amborella",
    "eutrema", "thellungiella", "salicornia", "mesembryanthemum", "beta", "spinacia",
    "helianthus", "lactuca", "daucus", "capsicum", "fragaria", "musa", "manihot",
    "ipomoea", "saccharum", "panicum", "festuca", "lolium", "trifolium", "hevea",
    "theobroma", "coffea", "camellia", "olea", "phoenix", "elaeis", "ananas",
    "arachis", "areca", "cassia", "chrysanthemum", "epimedium", "eriobotrya",
    "macadamia", "mangifera", "persea", "petunia", "striga",
    "saccharomyces", "schizosaccharomyces", "pichia", "escherichia", "agrobacterium",
    "pseudomonas", "bacillus", "synechocystis",
    "homo", "mus", "rattus", "danio", "drosophila", "caenorhabditis", "xenopus",
    "gallus", "bos", "sus", "ovis", "canis", "felis", "equus", "macaca", "oryctolagus",
]


def _write(result: Dict[str, List[str]]) -> None:
    payload = {
        "source": "NCBI Taxonomy (E-utils esearch+esummary, rank=species)",
        "generated": time.strftime("%Y-%m-%d"),
        "genera": result,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> None:
    # Resume from whatever is already on disk — a genus already recorded is skipped,
    # so a killed/timed-out run can just be re-invoked instead of starting over.
    result: Dict[str, List[str]] = {}
    if OUT.exists():
        try:
            result = json.loads(OUT.read_text()).get("genera", {})
        except Exception:  # noqa: BLE001
            result = {}

    for genus in GENERA:
        if genus in result:
            print(f"  {genus:20s} (already done)", file=sys.stderr)
            continue
        try:
            # A generous cap for this offline, occasional maintenance run — no latency
            # constraint here, unlike the live per-claim lookup in species.py.
            epithets = ncbi_taxonomy_species(genus, retmax_page=500, max_pages=10)
        except Exception as exc:  # noqa: BLE001 - keep going on a single bad lookup
            print(f"  {genus:20s} FAILED: {exc}", file=sys.stderr)
            epithets = set()
        print(f"  {genus:20s} {len(epithets)} species", file=sys.stderr)
        result[genus] = sorted(epithets)
        _write(result)  # save after every genus so a timeout loses at most one lookup

    total = sum(len(v) for v in result.values())
    print(f"wrote {OUT} — {len(result)} genera, {total} species", file=sys.stderr)


if __name__ == "__main__":
    main()

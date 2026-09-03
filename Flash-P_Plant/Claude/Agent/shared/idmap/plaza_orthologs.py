#!/usr/bin/env python3
"""Ortholog pairs from PLAZA, built from its bulk download rather than its API.

PLAZA is the second opinion this tool wants most. Its web API currently returns server
errors, but that was never how PLAZA was used here: the earlier benchmarking read a bulk
download, and the download server is healthy. This module reinstates that route and makes
it work for any species pair rather than the single hand-prepared Arabidopsis-to-rice file
the benchmark used.

Why it is worth the trouble: PLAZA infers orthology by a different method from Ensembl
Compara -- gene family trees, best-hit families, and collinearity between genomes -- so
when the two agree it is genuine corroboration rather than the same computation run twice.
Compara alone leaves many crop projections sitting on a many-to-many relationship that is
right only about a third of the time.

PLAZA also reports *why* it believes a pair, as four independent evidence types:

    TROG          the two genes sit in the same tree-based orthologous group
    BHIF          best-hit-in-family
    ORTHO         one-to-one orthology within the gene family
    anchor_point  the genes are a collinearity anchor between the two genomes

Each is 1 (supported), 0 (not supported) or -1 (no data). Counting the supported types
gives a graded confidence that Compara's single label does not.

The per-species files are 70-110 MB, which is far too large to ship. They are therefore
streamed and filtered in flight: only the rows for the requested target species are kept,
and the resulting table -- a megabyte or two -- is cached per species pair. Nothing large
is ever written to disk.
"""
import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

FTP = "https://ftp.psb.ugent.be/pub/plaza"
INSTANCE_RE = re.compile(r'href="\./(plaza_public_[a-z]+_\d+)/"')

# The four evidence types PLAZA reports, in the order they appear in the file.
EVIDENCE = ("TROG", "BHIF", "ORTHO", "anchor_point")


def _fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT})
    return common.open_url(req, timeout=timeout)


def instances():
    """PLAZA public instances, discovered rather than hard-coded.

    New instances appear as PLAZA releases them, and a user's species may only be in one
    we have never heard of.
    """
    path = common.cache_path("plaza", "instances.json")
    if os.path.exists(path):
        return json.load(open(path))
    with _fetch(f"{FTP}/") as fh:
        html = fh.read().decode("utf-8", "replace")
    found = sorted(set(INSTANCE_RE.findall(html)))
    if not found:
        raise SystemExit("could not list PLAZA instances; the download server may be down")
    # Prefer the newest release of each clade instance.
    best = {}
    for name in found:
        clade, ver = name.rsplit("_", 1)
        if clade not in best or ver > best[clade].rsplit("_", 1)[1]:
            best[clade] = name
    out = sorted(best.values())
    with open(path, "w") as fh:
        json.dump(out, fh)
    return out


def species_index():
    """{taxid: [(instance, plaza_code, binomial), ...]} across every instance.

    Keyed on NCBI taxon id rather than on the species name, because PLAZA's codes and
    Ensembl's directory names share no vocabulary and both differ from how a species is
    written in a paper. The taxon id is the one identifier all three agree on.
    """
    path = common.cache_path("plaza", "species_index.json")
    if os.path.exists(path):
        return json.load(open(path))
    idx = {}
    for inst in instances():
        url = f"{FTP}/{inst}/SpeciesInformation/species_information.csv.gz"
        try:
            with _fetch(url) as fh:
                text = gzip.decompress(fh.read()).decode("utf-8", "replace")
        except Exception:
            continue
        for line in text.splitlines():
            if line.startswith("# ") or not line.strip():
                continue
            f = line.lstrip("#").split("\t")
            if len(f) < 3 or f[0] == "species":
                continue
            code, name, taxid = f[0].strip(), f[1].strip(), f[2].strip()
            if not taxid.isdigit():
                continue
            idx.setdefault(taxid, []).append([inst, code, name])
    with open(path, "w") as fh:
        json.dump(idx, fh, indent=0)
    return idx


def locate(taxid):
    return species_index().get(str(taxid), [])


def find_instance(src_taxid, tgt_taxid):
    """An instance holding both species, or None.

    PLAZA splits its data by clade, so a cross-clade pair only works where one species is
    carried as an outgroup. Arabidopsis is present in the monocot instance for exactly this
    reason, which is what makes Arabidopsis-to-sorghum projection possible at all.
    """
    src = {i[0]: i[1] for i in locate(src_taxid)}
    tgt = {i[0]: i[1] for i in locate(tgt_taxid)}
    shared = sorted(set(src) & set(tgt))
    if not shared:
        return None
    # Prefer the instance whose clade the target belongs to: its gene families are built
    # around that clade rather than treating it as an outgroup.
    tgt_only = [i[0] for i in locate(tgt_taxid)]
    pick = next((i for i in shared if tgt_only.count(i) and i in shared), shared[-1])
    return {"instance": pick, "src_code": src[pick], "tgt_code": tgt[pick]}


# Below this fraction of identifiers landing inside the target species' own identifier
# space, the two sources are not describing the same annotation and the pairs are useless.
MIN_PATTERN_MATCH = 0.5


def _pattern_match_rate(ids, ens_name):
    """Fraction of identifiers that look like this species' reference identifiers.

    A far better check than joining against the name cache, which holds only the small
    minority of genes that carry a symbol -- 475 of sorghum's, so a perfectly good
    transform scores 0.24 there and the number means nothing on its own.
    """
    sr = common._resolver()
    try:
        pat, cov = sr.derived_pattern(ens_name)
    except Exception:
        return None
    if not pat or cov < 0.5:
        return None
    rx = re.compile(pat + r"$")
    ids = list(ids)[:5000]
    if not ids:
        return None
    return sum(1 for i in ids if rx.match(i)) / len(ids)


def _transform_for(plaza_ids, ens_name):
    """A function mapping PLAZA identifiers into this species' Ensembl identifier space.

    Discovered from the data rather than tabulated: PLAZA writes sorghum as Sobic.001G000100
    where Ensembl writes SORBI_3001G000100, and every species has its own such quirk. The
    resolver skill already derives these swaps and reports how well the result joins.
    """
    sys.path.insert(0, common.resolver_dir())
    import build_phytozome_layer as bpl  # noqa: E402
    ens_ids = bpl.ensembl_gene_ids(ens_name)
    if not ens_ids:
        return (lambda x: bpl.canon(x)), "identity (no reference identifiers to check against)", 0.0
    label, fn, rate = bpl.choose_transform(list(plaza_ids)[:5000], ens_ids)
    if fn is None:
        return (lambda x: bpl.canon(x)), "identity (no transform matched)", 0.0
    return fn, label, rate


def pair_path(inst, src_code, tgt_code):
    return common.cache_path("plaza", f"{inst}__{src_code}__{tgt_code}.tsv.gz")


def build_pair(src_ens, tgt_ens, src_taxid, tgt_taxid, force=False):
    """Stream one PLAZA species file and keep only the rows for the target species."""
    loc = find_instance(src_taxid, tgt_taxid)
    if not loc:
        return None, "no PLAZA instance carries both species"
    out = pair_path(loc["instance"], loc["src_code"], loc["tgt_code"])
    meta_path = out + ".json"
    refused_path = out + ".refused.json"
    if os.path.exists(out) and not force:
        return out, json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    # A pair already found to be unusable is remembered, so a 70-110 MB download is not
    # repeated every run only to reach the same conclusion.
    if os.path.exists(refused_path) and not force:
        return None, json.load(open(refused_path)).get("reason", "previously refused")

    url = (f"{FTP}/{loc['instance']}/IntegrativeOrthologySpecies/"
           f"integrative_orthology.{loc['src_code']}.tsv.gz")
    print(f"streaming {url}\n  keeping only {loc['tgt_code']} rows; nothing large is stored",
          file=sys.stderr)

    rows = []
    early_stop = False
    src_seen, tgt_seen = set(), set()
    try:
        with _fetch(url, timeout=900) as resp:
            with gzip.GzipFile(fileobj=resp) as gz:
                for raw in io.TextIOWrapper(gz, encoding="utf-8", errors="replace"):
                    if raw.startswith("#"):
                        continue
                    f = raw.rstrip("\n").split("\t")
                    if len(f) < 8 or f[3] != loc["tgt_code"]:
                        continue
                    rows.append(f)
                    src_seen.add(f[0])
                    tgt_seen.add(f[2])
                    # Give up as soon as it is clear the identifier systems do not
                    # correspond, rather than streaming the remaining tens of megabytes.
                    if len(rows) == 20000:
                        probe = _pattern_match_rate(tgt_seen, tgt_ens)
                        if probe is not None and probe < MIN_PATTERN_MATCH:
                            swapped = _transform_for(tgt_seen, tgt_ens)[0]
                            probe2 = _pattern_match_rate(
                                {swapped(i) for i in tgt_seen}, tgt_ens)
                            if probe2 is None or probe2 < MIN_PATTERN_MATCH:
                                early_stop = True
                                break
    except Exception as e:
        return None, f"PLAZA download failed: {e}"
    if not rows:
        return None, f"PLAZA returned no {loc['tgt_code']} orthologs for {loc['src_code']}"

    src_fn, src_label, src_rate = _transform_for(src_seen, src_ens)
    tgt_fn, tgt_label, tgt_rate = _transform_for(tgt_seen, tgt_ens)

    # Refuse a pair whose identifiers do not land in the target's identifier space. PLAZA
    # carries barley as Horvu_MOREX_3H01G095100 while the reference here is
    # HORVU.MOREX.r3.7HG0635170 -- different annotation releases with different gene
    # numbering, which no rewriting can reconcile. Writing the table anyway would hand out
    # identifiers that look plausible and refer to nothing.
    tgt_pattern_rate = _pattern_match_rate({tgt_fn(i) for i in tgt_seen}, tgt_ens)
    if tgt_pattern_rate is not None and tgt_pattern_rate < MIN_PATTERN_MATCH:
        example_in = sorted(tgt_seen)[:1]
        reason = (
            f"PLAZA's {loc['tgt_code']} identifiers do not match the reference annotation "
            f"used here: only {tgt_pattern_rate:.0%} land in {tgt_ens}'s identifier space "
            f"(PLAZA has {example_in[0] if example_in else '?'}). PLAZA is almost certainly "
            "built on a different annotation release for this species, so its orthologs "
            "cannot be used without a release mapping.")
        with open(refused_path, "w") as fh:
            json.dump({"reason": reason, "tgt_pattern_match": round(tgt_pattern_rate, 4),
                       "example_plaza_id": example_in[0] if example_in else None,
                       "stopped_early": early_stop}, fh, indent=1)
        return None, reason

    tmp = out + ".tmp"
    with gzip.open(tmp, "wt", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["src_gene", "tgt_gene", "n_support"] + list(EVIDENCE))
        for f in rows:
            flags = f[4:8]
            n = sum(1 for x in flags if x == "1")
            w.writerow([src_fn(f[0]), tgt_fn(f[2]), n] + flags)
    os.replace(tmp, out)

    # Which identifier system the target column is actually in. Where the species has an
    # Ensembl reference the identifiers were rewritten into it and verified. Where it has
    # none -- the cultivated strawberry, for instance -- PLAZA's own identifiers are kept,
    # because they are real accessions from a published annotation and are a far better
    # answer than an ortholog in some distant relative. They are not Ensembl identifiers
    # though, and saying so is the difference between a usable answer and a misleading one.
    tgt_id_system = ("ensembl" if tgt_pattern_rate is not None
                     else f"plaza:{loc['instance']}:{loc['tgt_code']}")

    meta = {"instance": loc["instance"], "src_code": loc["src_code"],
            "tgt_id_system": tgt_id_system,
            "tgt_code": loc["tgt_code"], "n_pairs": len(rows),
            "n_src_genes": len(src_seen), "n_tgt_genes": len(tgt_seen),
            "src_transform": src_label, "src_join_rate": round(src_rate, 4),
            "tgt_transform": tgt_label, "tgt_join_rate": round(tgt_rate, 4),
            "tgt_pattern_match": (round(tgt_pattern_rate, 4)
                                  if tgt_pattern_rate is not None else None),
            "source_url": url}
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=1)
    return out, meta


_TABLES = {}

# Building a pair table streams and distils one large download and writes it to disk. With
# several nodes in flight they would otherwise all discover the same table missing at the
# same moment and all start building it, over the same path. One builder at a time: the
# lock is held across the build, and every call after it is a memo hit that never waits.
_TABLES_LOCK = threading.RLock()


def load_pair(src_ens, tgt_ens, build=True):
    key = (src_ens, tgt_ens)
    if key in _TABLES:
        return _TABLES[key]
    with _TABLES_LOCK:
        if key in _TABLES:
            return _TABLES[key]
        return _load_pair_locked(key, src_ens, tgt_ens, build)


def _load_pair_locked(key, src_ens, tgt_ens, build=True):
    src_info = common.classify_species(src_ens)
    tgt_info = common.classify_species(tgt_ens)
    src_tax = src_info.get("taxid") or common.ncbi_taxon_known(src_info.get("binomial") or src_ens)
    tgt_tax = tgt_info.get("taxid") or common.ncbi_taxon_known(tgt_info.get("binomial") or tgt_ens)
    if not (src_tax and tgt_tax):
        _TABLES[key] = ({}, {"error": "could not determine taxon ids for both species"})
        return _TABLES[key]

    loc = find_instance(src_tax, tgt_tax)
    if not loc:
        _TABLES[key] = ({}, {"error": "no PLAZA instance carries both species"})
        return _TABLES[key]
    path = pair_path(loc["instance"], loc["src_code"], loc["tgt_code"])
    meta = {}
    if not os.path.exists(path):
        if not build:
            _TABLES[key] = ({}, {"error": "pair table not built yet"})
            return _TABLES[key]
        path, meta = build_pair(src_ens, tgt_ens, src_tax, tgt_tax)
        if not path:
            _TABLES[key] = ({}, {"error": meta})
            return _TABLES[key]
    else:
        mp = path + ".json"
        meta = json.load(open(mp)) if os.path.exists(mp) else {}

    table = {}
    with gzip.open(path, "rt") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            table.setdefault(r["src_gene"], []).append({
                "gene_id": r["tgt_gene"],
                "n_support": int(r["n_support"]),
                "evidence": [k for k in EVIDENCE if r.get(k) == "1"],
            })
    for v in table.values():
        v.sort(key=lambda c: -c["n_support"])

    # Tables built before the identifier system was recorded, or by an older version, are
    # classified from their own contents rather than being rebuilt from a 100 MB download.
    if not meta.get("tgt_id_system"):
        sample = {c["gene_id"] for v in list(table.values())[:4000] for c in v[:2]}
        rate = _pattern_match_rate(sample, tgt_ens)
        meta["tgt_id_system"] = (
            "ensembl" if (rate is not None and rate >= MIN_PATTERN_MATCH)
            else f"plaza:{loc['instance']}:{loc['tgt_code']}")
        meta["tgt_pattern_match"] = round(rate, 4) if rate is not None else None
        try:
            with open(path + ".json", "w") as fh:
                json.dump(meta, fh, indent=1)
        except Exception:
            pass

    _TABLES[key] = (table, meta)
    return _TABLES[key]


def orthologs(gene, src_ens, tgt_ens, build=True):
    table, meta = load_pair(src_ens, tgt_ens, build=build)
    return table.get(gene, []), meta


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gene", help="comma-separated gene identifiers in the source species")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="tgt", required=True)
    ap.add_argument("--build-only", action="store_true",
                    help="build the pair table and report on it, without querying")
    args = ap.parse_args()

    src = common.classify_species(args.src).get("ensembl_name") or args.src
    tgt = common.classify_species(args.tgt).get("ensembl_name") or args.tgt

    table, meta = load_pair(src, tgt)
    if not table:
        print(json.dumps({"resolved": False, "note": meta}, indent=1))
        return
    if args.build_only or not args.gene:
        print(json.dumps({"pairs_cached": len(table), **meta}, indent=1))
        return
    for g in [x.strip() for x in args.gene.split(",") if x.strip()]:
        cands, _ = orthologs(g, src, tgt)
        print(json.dumps({"gene": g, "source_species": src, "target_species": tgt,
                          "n_candidates": len(cands), "candidates": cands[:12]}))


if __name__ == "__main__":
    main()

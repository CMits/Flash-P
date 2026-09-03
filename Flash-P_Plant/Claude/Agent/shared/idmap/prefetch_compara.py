#!/usr/bin/env python3
"""Fill the ortholog cache for one species pair from the Ensembl Plants Compara dumps.

Projection is the route that carries this tool, and asking Compara over REST costs one
call per gene. On a species pair seen for the first time that dominates the run: a canola
network cost 47s a node against 8.4s once the pair was cached, and 95% of that was a
single process waiting on HTTP. The whole pairwise table is a file, so fetch the file.

Sizes, measured against release 116:

    all species, one file                                    8.4 GB   -- never this one
    per species, arabidopsis_thaliana                         30 MB
    per species, brassica_napus                              121 MB
    filtered to the A. thaliana <-> B. napus pair            1.8 MB   -- what is kept

Two things about these dumps are not obvious and both matter here:

  The per-species files are NOT symmetric. The Arabidopsis dump covers 30 partner species
  and contains no Brassica at all; the B. napus dump covers 44 and does contain
  Arabidopsis, with 72,979 ortholog rows over 22,588 Arabidopsis genes. So there is no
  rule like "always fetch the anchor's file" -- the pair has to be looked for, and if the
  first file does not hold it the other one is tried.

  A gene absent from the table is not authoritatively without orthologs. The dump and the
  REST database are built from different releases, and B. napus is frozen at an older one
  than the current plant Compara. So a miss after this has run still goes to REST. On the
  canola network that left 4 genes of 74 to ask about live -- and all four were genes REST
  itself returned nothing for, AT4G29130 (HXK1) among them, which is a useful independent
  confirmation rather than a gap.

  A species can have SEVERAL collection dumps, and only `protein_default` is read here.
  Barley has four (default, barley_cultivars, wheat_cultivars, oat_cultivars) and wheat
  three. This was checked rather than assumed: for barley every cultivars collection is a
  strict subset of `protein_default` in species coverage, and the wheat-barley pair set is
  identical in all three files that hold it, with no pair unique to a cultivars file. So
  `protein_default` is the right file -- but nothing in the data guarantees that for a
  species not yet looked at, which is why each marker records the partner species the
  chosen dump carried and which dumps went unscanned.

Existing cache entries for genes the dump does not mention are kept, including the empty
ones: an empty entry records that the question was asked and answered, and re-asking it
every run is exactly the cost this script exists to remove.

Usage:
    python Agent/shared/idmap/prefetch_compara.py --src arabidopsis_thaliana --tgt brassica_napus
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

FTP = ("https://ftp.ebi.ac.uk/ensemblgenomes/pub/plants/{release}/tsv/"
       "ensembl-compara/homologies/{species}/")
DUMP_RE = re.compile(r'Compara\.\d+\.protein_default\.homologies\.tsv\.gz')

# Columns of the homology TSV, by name in the header line.
NEEDED = ("gene_stable_id", "species", "identity",
          "homology_type", "homology_gene_stable_id", "homology_species",
          "homology_identity")


def marker_file(src, tgt):
    """Named by the unordered pair -- see common.ortholog_pair_key for why."""
    return common.cache_path("orthologs", f"{common.ortholog_pair_key(src, tgt)}.bulk.json")


def _listing(species, release):
    url = FTP.format(release=release, species=species)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT})
        with common.open_url(req, timeout=60) as fh:
            html = fh.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = DUMP_RE.search(html)
    return url + m.group(0) if m else None


def _size(url):
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": common.USER_AGENT})
        with common.open_url(req, timeout=60) as fh:
            return int(fh.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def scan(url, src, tgt, quiet=False):
    """Stream the dump and pull out both directions of one species pair.

    Streamed and decompressed on the fly rather than saved: the file is up to a few hundred
    megabytes and only a percent or two of it concerns any one pair.
    """
    fwd, rev = {}, {}
    # Every species this dump holds homologies against. A dump that does not list the other
    # half of the pair at all explains an empty scan; one that lists it and still yields
    # nothing does not, and that is the case worth shouting about.
    partners = set()
    req = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT})
    t0 = time.time()
    with common.open_url(req, timeout=120) as resp:
        gz = gzip.GzipFile(fileobj=resp)
        text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        header = next(text, "").rstrip("\n").split("\t")
        try:
            col = {name: header.index(name) for name in NEEDED}
        except ValueError:
            return {}, {}, 0, []
        n = 0
        for line in text:
            f = line.rstrip("\n").split("\t")
            if len(f) <= col["homology_species"]:
                continue
            a_sp, b_sp = f[col["species"]], f[col["homology_species"]]
            partners.add(b_sp)
            if a_sp == src and b_sp == tgt:
                # perc_id is always the identity of the gene being projected ONTO, which is
                # how the REST response reports it and how the ranking downstream reads it.
                key, val = f[col["gene_stable_id"]], f[col["homology_gene_stable_id"]]
                pid, back_pid = f[col["homology_identity"]], f[col["identity"]]
            elif a_sp == tgt and b_sp == src:
                key, val = f[col["homology_gene_stable_id"]], f[col["gene_stable_id"]]
                pid, back_pid = f[col["identity"]], f[col["homology_identity"]]
            else:
                continue
            typ = f[col["homology_type"]]
            fwd.setdefault(common_canon(key), []).append(
                {"gene_id": common_canon(val), "type": typ,
                 "perc_id": _num(pid), "perc_pos": None})
            rev.setdefault(common_canon(val), []).append(
                {"gene_id": common_canon(key), "type": typ,
                 "perc_id": _num(back_pid), "perc_pos": None})
            n += 1
    for d in (fwd, rev):
        for v in d.values():
            v.sort(key=lambda c: -(c.get("perc_id") or 0))
    if not quiet:
        print(f"  {n} pair rows in {time.time() - t0:.0f}s "
              f"({len(partners)} partner species in this dump)", file=sys.stderr)
    return fwd, rev, n, sorted(partners)


def common_canon(i):
    return re.sub(r"\.\d+$", "", re.sub(r"^gene[-:]", "", str(i).strip()))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def merge_into_cache(pairs, src, tgt):
    """Write one direction, keeping entries the dump does not mention."""
    existing = common.load_ortholog_cache(src, tgt)
    merged = dict(existing)
    merged.update(pairs)          # the table is complete where it speaks; live answers
    common.save_ortholog_cache(src, tgt, merged)   # for a gene it does not mention are
    return len(merged), len(merged) - len(existing)   # kept by the update above


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="Ensembl name, e.g. arabidopsis_thaliana")
    ap.add_argument("--tgt", required=True, help="Ensembl name, e.g. brassica_napus")
    ap.add_argument("--release", default="current")
    ap.add_argument("--force", action="store_true", help="re-fetch a pair already marked")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    src, tgt = a.src, a.tgt
    if src == tgt:
        return

    marker = marker_file(src, tgt)
    if os.path.exists(marker) and not a.force:
        return

    # One downloader per pair. A stale lock is ignored after an hour, so a killed run does
    # not leave the pair permanently unfetchable.
    lock = marker + ".lock"
    try:
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600:
            return
        with open(lock, "w") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        return

    try:
        # Try the smaller file first; fall back to the other, because the per-species dumps
        # do not carry symmetric partner lists.
        urls = [u for u in (_listing(tgt, a.release), _listing(src, a.release)) if u]
        urls.sort(key=_size)
        fwd = rev = None
        used = None
        attempts = []
        for url in urls:
            fwd, rev, n, partners = scan(url, src, tgt, quiet=a.quiet)
            attempts.append({"dump": url, "pair_rows": n, "partner_species": partners})
            if n:
                used = url
                break
            # An empty scan is unremarkable when the dump simply does not carry the other
            # species -- the per-species dumps are not symmetric, and a barley dump holding
            # no Arabidopsis is exactly why the fallback exists. A dump that lists both
            # species and still holds no pair rows is a different thing: the pair may be
            # split across collections, and this is the only place that would show.
            if src in partners and tgt in partners and not a.quiet:
                print(f"  WARNING: {url} lists both {src} and {tgt} but yielded no pair "
                      f"rows; the pair may be split across collections", file=sys.stderr)
        if not used:
            with open(marker, "w") as fh:
                json.dump({"status": "unavailable", "src": src, "tgt": tgt,
                           "attempts": attempts,
                           "when": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
            if not a.quiet:
                print(f"no {src}/{tgt} rows in either dump; leaving this pair to REST",
                      file=sys.stderr)
            return

        n_fwd, added_fwd = merge_into_cache(fwd, src, tgt)
        n_rev, added_rev = merge_into_cache(rev, tgt, src)
        with open(marker, "w") as fh:
            json.dump({"status": "complete", "src": src, "tgt": tgt, "source_file": used,
                       "genes_forward": n_fwd, "genes_reverse": n_rev,
                       # What the chosen dump carried, and what was tried before it. Keeps
                       # a later audit honest about which collection an answer came from.
                       # "attempts" holds every dump opened, empty ones included; a dump is
                       # only "not scanned" if the search stopped before reaching it.
                       "attempts": attempts,
                       "dumps_not_scanned": [u for u in urls
                                             if u not in {a["dump"] for a in attempts}],
                       "when": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
        if not a.quiet:
            print(f"{src} -> {tgt}: {added_fwd} genes added ({n_fwd} cached); "
                  f"reverse {added_rev} added ({n_rev} cached)", file=sys.stderr)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    main()

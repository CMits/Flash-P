#!/usr/bin/env python3
"""Build functional description layers for one or more species from NCBI Gene.

Why this exists. Disambiguating between projected orthologs is the step that decides most
nodes, and `description_agrees` is what does it -- so a species with no descriptions cannot
be disambiguated at all. Ensembl Plants carries no description for several major crops:
looking up the pea gene Psat1g161560 returns a record with no description field of any kind,
which is why every pea node in the corpus fell back to name lookup alone.

How the join is made. NCBI's own LocusTag is useless for this -- for pea it recovers 0 of
40,025 descriptions, because NCBI numbers those genes its own way. The join that works is
Ensembl's Entrez cross-reference file, gene_stable_id <-> NCBI GeneID, the same bridge the
name cache already uses for symbols. Through it, pea recovers 11,352 descriptions.

GeneIDs are globally unique, so the taxon never has to be resolved: build the bridges first,
then whichever GeneIDs appear in them identify both the species and the gene.

    python Agent/shared/idmap/build_ncbi_layer.py --species pisum_sativum,sorghum_bicolor
    python Agent/shared/idmap/build_ncbi_layer.py --corpus            # every species with a prepared network

One pass over NCBI's 195 MB plant file serves every species named, so ask for all of them
at once rather than one at a time. Nothing large is kept: the file is streamed and filtered
in flight, and only the per-species layers are written.
"""
import argparse
import glob
import gzip
import io
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

ENSEMBL_FTP = "https://ftp.ensemblgenomes.ebi.ac.uk/pub/plants/current"
NCBI_GENE_INFO = ("https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Plants/"
                  "All_Plants.gene_info.gz")
UA = {"User-Agent": "flashp-idmap/1.0 (plant gene description layer)"}

# Descriptions that describe nothing. Keeping them would be worse than having no layer:
# a shortlist search would match "protein" against thousands of genes, and `description_agrees`
# would fire on a candidate whose annotation says only that nobody has characterised it.
EMPTY_DESC = re.compile(
    r"^\s*(-|uncharacterized|hypothetical|unknown|predicted)\b|^\s*protein\s*$", re.I)


def _open(url, timeout=180):
    # Through common.open_url, not urlopen: this fetches from NCBI at --workers 6, and
    # NCBI's 3/s is the tightest limit we face. The shared token bucket is the only thing
    # that makes the worker count safe, and it also brings Retry-After handling with it.
    return common.open_url(urllib.request.Request(url, headers=UA), timeout=timeout)


def entrez_bridge(ens_name):
    """{NCBI GeneID: gene_stable_id} for a species, or {} when Ensembl publishes no bridge."""
    try:
        listing = _open(f"{ENSEMBL_FTP}/tsv/{ens_name}/", timeout=60).read().decode("utf8", "replace")
    except Exception as e:
        print(f"  {ens_name}: cannot list Ensembl tsv directory ({e})", file=sys.stderr)
        return {}
    m = re.findall(r'href="([^"]*\.entrez\.tsv\.gz)"', listing)
    if not m:
        print(f"  {ens_name}: no Entrez bridge published; descriptions cannot be joined",
              file=sys.stderr)
        return {}
    blob = _open(f"{ENSEMBL_FTP}/tsv/{ens_name}/{m[0]}").read()
    out = {}
    with gzip.open(io.BytesIO(blob), "rt", errors="replace") as fh:
        fh.readline()
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) > 4 and f[4] == "EntrezGene" and f[3].isdigit():
                # One gene can carry several GeneIDs; each maps back to the one gene.
                out.setdefault(f[3], f[0])
    return out


def bridged_species():
    """Species whose Ensembl record carries an Entrez cross-reference file.

    Read from the manifest rather than probed: the cache build already recorded it, and
    listing 267 FTP directories to rediscover it would be the slowest part of this job.
    """
    man = common._resolver()._manifest()
    return [sp for sp, row in sorted(man.items()) if row.get("bridge") == "yes"]


def build(species, force=False, workers=6):
    todo = []
    for sp in species:
        path = common.cache_path("descriptions", f"{sp}.tsv.gz")
        if os.path.exists(path) and not force:
            print(f"  {sp}: layer already built")
            continue
        todo.append(sp)

    # Fetching the bridges is the slow half -- two HTTP round trips per species against a
    # server on another continent -- and it is entirely I/O, so it is done concurrently.
    # Kept modest: this is a public FTP mirror, not a service we are entitled to saturate.
    targets, bridges = [], {}
    if todo:
        print(f"fetching Entrez bridges for {len(todo)} species ({workers} at a time)")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sp, b in zip(todo, ex.map(entrez_bridge, todo)):
                if not b:
                    continue
                bridges[sp] = b
                targets.append(sp)
        print(f"  {len(targets)} of {len(todo)} species have a usable bridge")
    if not targets:
        print("nothing to build.")
        return {}

    # GeneID -> (species, gene_id). Globally unique, so one lookup resolves both.
    index = {}
    for sp in targets:
        for gid, gene in bridges[sp].items():
            index[gid] = (sp, gene)
    print(f"streaming NCBI gene_info, matching {len(index)} GeneIDs across {len(targets)} species")

    rows = {sp: {} for sp in targets}
    seen = 0
    with _open(NCBI_GENE_INFO, timeout=600) as resp:
        with gzip.open(resp, "rt", errors="replace") as fh:
            fh.readline()
            for line in fh:
                f = line.split("\t")
                if len(f) < 9:
                    continue
                hit = index.get(f[1])
                if not hit:
                    continue
                seen += 1
                desc = f[8].strip()
                if EMPTY_DESC.match(desc):
                    continue
                sp, gene = hit
                sym = f[2].strip()
                if sym and sym != "-" and not sym.startswith("LOC") and sym.lower() not in desc.lower():
                    desc = f"{desc} ({sym})"
                rows[sp].setdefault(gene, desc)
    print(f"  matched {seen} NCBI records")

    written = {}
    for sp in targets:
        path = common.cache_path("descriptions", f"{sp}.tsv.gz")
        with gzip.open(path, "wt") as fh:
            fh.write("gene_id_canon\tdescription\tsource\n")
            for gene, desc in sorted(rows[sp].items()):
                fh.write(f"{gene}\t{desc}\tncbi_gene\n")
        kb = os.path.getsize(path) / 1024
        written[sp] = len(rows[sp])
        print(f"  {sp}: {len(rows[sp])} descriptions -> {path} ({kb:.0f} KB)")
    return written


def corpus_species(runs="networks"):
    """Every species already prepared under networks/<Trait>/idmapping/."""
    out = []
    for f in sorted(glob.glob(os.path.join(runs, "*", "idmapping", "node_dossiers.json"))):
        try:
            s = json.load(open(f))["summary"]
        except Exception:
            continue
        ens = s.get("network_species_ensembl")
        if ens and ens not in out:
            out.append(ens)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--species", help="comma-separated Ensembl species names")
    ap.add_argument("--corpus", action="store_true",
                    help="every species appearing in a prepared networks/*/idmapping/")
    ap.add_argument("--all-bridged", action="store_true",
                    help="every species whose Ensembl record carries an Entrez bridge")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent bridge downloads (default 6)")
    ap.add_argument("--runs", default="networks",
                    help="directory of network folders to scan for prepared idmapping runs")
    ap.add_argument("--force", action="store_true", help="rebuild layers that already exist")
    a = ap.parse_args()

    sp = [x.strip() for x in (a.species or "").split(",") if x.strip()]
    if a.corpus:
        sp = list(dict.fromkeys(sp + corpus_species(a.runs)))
    if a.all_bridged:
        sp = list(dict.fromkeys(sp + bridged_species()))
    if not sp:
        sys.exit("give --species, --corpus or --all-bridged")
    build(sp, force=a.force, workers=a.workers)


if __name__ == "__main__":
    main()

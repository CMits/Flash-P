#!/usr/bin/env python3
"""Cache Phytozome's functional descriptions and join them to Ensembl gene identifiers.

Phytozome holds NO gene nomenclature -- verified on the complete 34,129-gene sorghum set:
its "Gene Name" attribute is the locus itself, "Synonyms" is a transcript count, and 100% of
non-blank descriptions carry a Pfam/Panther/KEGG/KOG accession. It cannot tell you that
Sobic.007G135700 is called MSD1.

What it can do is say what a gene IS, from homology, independently of Ensembl Compara. That
makes it a corroboration layer for identifiers proposed by other routes -- especially ortholog
projections, whose weakness is that a plausible-looking wrong gene is indistinguishable from a
right one. See corroborate.py for the scoring and its measured discrimination.

Identifiers must be joined per species, because Phytozome and Ensembl do not always agree:

    poplar   Potri.003G041900     == Ensembl                      identity
    moss     Pp3c1_100            == Ensembl                      identity
    potato   PGSC0003DMG400042721 == Ensembl                      identity
    tomato   Solyc00g005050.2     -> Solyc00g005050               strip version
    sorghum  Sobic.001G341700     -> SORBI_3001G341700            swap prefix
    rice     LOC_Os09g16560 (MSU) vs Os09g0... (RAP-DB)           NO JOIN
    maize    Zm00001d009054 (v4)  vs Zm00001eb... (v5)            NO JOIN

The transform is not hardcoded per species: candidates are scored by how many identifiers they
actually land on the species' known Ensembl genes, and one is accepted only if it clears a
floor. A species whose systems genuinely do not correspond gets no layer rather than a wrong one.

Usage:
    python3 build_phytozome_layer.py --species sorghum --organism-id 454
    python3 build_phytozome_layer.py --list-organisms | grep -i Sbicolor
"""
import argparse, collections, csv, gzip, importlib.util, io, os, pathlib, re, sys
import urllib.parse, urllib.request

HERE = pathlib.Path(__file__).parent
_s = importlib.util.spec_from_file_location('species_resolver', HERE / 'species_resolver.py')
_sr = importlib.util.module_from_spec(_s); _s.loader.exec_module(_sr)

CACHE = _sr.CACHE
OUT = _sr.WRITE / 'phytozome'
MART = 'https://phytozome-next.jgi.doe.gov/biomart/martservice'
MIN_JOIN_RATE = 0.30      # below this the two identifier systems do not correspond

QUERY = ('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE Query>'
         '<Query virtualSchemaName="zome_mart" formatter="TSV" header="0" uniqueRows="1" '
         'datasetConfigVersion="0.6"><Dataset name="phytozome" interface="default">'
         '<Filter name="organism_id" value="{oid}"/>'
         '<Attribute name="gene_name1"/><Attribute name="gene_description"/>'
         '</Dataset></Query>')


def fetch_descriptions(oid, timeout=900):
    url = MART + '?query=' + urllib.parse.quote(QUERY.format(oid=oid))
    req = urllib.request.Request(url, headers={'User-Agent': 'gene-id-resolver/1.2'})
    rows = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in io.TextIOWrapper(r, encoding='utf-8', errors='replace'):
            p = line.rstrip('\n').split('\t')
            if p and p[0]:
                rows.append((p[0], p[1] if len(p) > 1 else ''))
    return rows


def canon(i):
    """Strip a trailing version from EITHER side before comparing.

    Ensembl writes poplar as Potri.006G169700.v4 while Phytozome writes Potri.006G169700;
    Phytozome writes tomato as Solyc00g005050.2 while Ensembl writes Solyc00g005050. The
    version sits on a different side per species, so both are normalised rather than one."""
    return re.sub(r'\.v\d+$', '', re.sub(r'\.\d+$', '', i.strip()))


def discover_prefix_swap(pz_ids, ens_canon, k=10, sample=8000):
    """Find the (phytozome_prefix, ensembl_prefix) pair by matching on identifier tails.

    Deriving a prefix as 'the leading run of non-digits' fails whenever the prefix itself
    ends in a digit: sorghum's Ensembl prefix is SORBI_3, and cutting at the first digit
    yields SORBI_, which lands every identifier one character short and joins nothing.
    Matching on the last k characters sidesteps the question -- Sobic.005G136200 and
    SORBI_3005G136200 share 005G136200 -- and the prefix pair falls out of the alignment."""
    by_tail = {}
    for e in ens_canon:
        if len(e) >= k:
            by_tail.setdefault(e[-k:], e)
    pairs = collections.Counter()
    for pid in pz_ids[:sample]:
        c = canon(pid)
        if len(c) < k:
            continue
        e = by_tail.get(c[-k:])
        if e:
            pairs[(c[:-k], e[:-k])] += 1
    if not pairs:
        return None
    (a, b), n = pairs.most_common(1)[0]
    return (a, b, n) if a != b else None


def candidate_transforms(pz_ids, ens_canon):
    """Transforms mapping a Phytozome identifier into canonical Ensembl space."""
    yield 'identity (after version canonicalisation)', canon
    swap = discover_prefix_swap(pz_ids, ens_canon)
    if swap:
        a, b, _ = swap
        yield (f'prefix {a!r} -> {b!r}',
               lambda x, a=a, b=b: (b + canon(x)[len(a):]) if canon(x).startswith(a) else canon(x))


def ensembl_gene_ids(species):
    p = _sr.GENES / f'{species}.tsv.gz'
    if not p.exists():
        return set()
    out = set()
    with gzip.open(p, 'rt') as fh:
        for r in csv.DictReader(fh, delimiter='\t'):
            out.add(r['gene_id'])
    return out


def choose_transform(pz_ids, ens_canon):
    best = (None, None, 0.0)
    for label, fn in candidate_transforms(pz_ids, ens_canon):
        hits = len({fn(i) for i in pz_ids} & ens_canon)
        rate = hits / max(len(ens_canon), 1)
        if rate > best[2]:
            best = (label, fn, rate)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--species', help='common name, binomial or Ensembl name')
    ap.add_argument('--organism-id', type=int, help='Phytozome organism_id')
    ap.add_argument('--list-organisms', action='store_true')
    a = ap.parse_args()

    if a.list_organisms:
        p = CACHE / 'pz_organisms.txt'
        print(p.read_text() if p.exists() else 'no cached organism list', end='')
        return
    if not a.species or not a.organism_id:
        ap.error('--species and --organism-id (see --list-organisms)')

    sp = _sr.resolve_species(a.species)
    ens_ids = ensembl_gene_ids(sp.ensembl_name)
    if not ens_ids:
        sys.exit(f'no cached Ensembl gene ids for {sp.ensembl_name}; build the name cache first')

    print(f'fetching Phytozome organism {a.organism_id} ...', file=sys.stderr)
    rows = fetch_descriptions(a.organism_id)
    pz_ids = [g for g, _ in rows]
    print(f'  {len(rows):,} rows', file=sys.stderr)

    # join in canonical space, but emit the identifier exactly as our cache spells it
    by_canon = {}
    for e in ens_ids:
        by_canon.setdefault(canon(e), e)
    label, fn, rate = choose_transform(pz_ids, set(by_canon))
    print(f'  best join: {label}  ({rate:.0%} of this species\' known Ensembl genes matched)',
          file=sys.stderr)
    if rate < MIN_JOIN_RATE:
        sys.exit(f'join rate {rate:.0%} is below the {MIN_JOIN_RATE:.0%} floor -- Phytozome and '
                 f'Ensembl use non-corresponding identifier systems for {sp.ensembl_name} '
                 f'(rice MSU vs RAP-DB, maize v4 vs v5). No layer written; a wrong join would '
                 f'be worse than none.')

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f'{sp.ensembl_name}.tsv.gz'
    n = 0
    with gzip.open(dest, 'wt', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['gene_id_canon', 'phytozome_id', 'description'])
        # Emit EVERY described gene in canonical Ensembl space. The name cache is used to
        # discover and validate the transform, not to filter the output -- restricting to
        # genes that already carry a name would keep only the ones needing no corroboration.
        for pid, desc in rows:
            if not desc.strip():
                continue
            w.writerow([fn(pid), pid, desc])
            n += 1
        meta = OUT / 'manifest.tsv'
    new = not meta.exists()
    with open(meta, 'a', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        if new:
            w.writerow(['species', 'organism_id', 'n_rows', 'n_described', 'join', 'join_rate'])
        w.writerow([sp.ensembl_name, a.organism_id, len(rows), n, label, round(rate, 4)])
    print(f'  wrote {dest} ({n:,} described genes)', file=sys.stderr)


if __name__ == '__main__':
    main()

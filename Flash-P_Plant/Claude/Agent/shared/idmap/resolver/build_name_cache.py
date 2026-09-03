#!/usr/bin/env python3
"""Build the offline gene-name -> identifier cache for Ensembl Plants species.

Three layers, joined per species:
  spine   Ensembl Plants GFF3 gene lines  -> gene_id + Name/Alias  (assembly-correct)
  bridge  Ensembl *.entrez.tsv.gz         -> gene_id <-> NCBI GeneID
  alias   NCBI All_Plants.gene_info       -> GeneID  -> Symbol + Synonyms

The bridge is the point of the design. Joining on LocusTag instead recovers only a
few hundred names per species because NCBI records a LocusTag for <1% of rows in
most crops; joining on GeneID recovers thousands.

Output: one gzipped TSV per species plus a manifest. Rows are source-attributed
raw facts -- never a resolved verdict -- so a bad answer can never become sticky.
"""
import argparse, csv, gzip, io, os, pathlib, re, sys, time
import urllib.request, urllib.error, urllib.parse
import collections, concurrent.futures as cf

FTP = 'https://ftp.ensemblgenomes.ebi.ac.uk/pub/plants/current'
UA  = 'gene-id-resolver-cachebuild/1.0 (plant gene name resolution)'
OUT = pathlib.Path(os.environ.get('FLASHP_IDMAP_CACHE')
                   or pathlib.Path(__file__).resolve().parents[4]
                   / '.flashp_cache' / 'idmap')

# A "name" that is really a model identifier carries no information for us.
PLACEHOLDER = re.compile(
    r'^(LOC\d+|'
    r'[A-Za-z]{2,10}[_.]?\d{2,}[Gg]\d{4,}|'      # Solyc01g005000, Sobic.001G000100, HORVU...G000010
    r'GRMZM\d[GT]\d+|Zm\d{5}[a-z]{1,2}\d+|'
    r'AT\dG\d{5}|'
    r'TraesCS\w+|Os\d{2}g\d{7}|LOC_Os\d{2}g\d{5}|'
    r'gene[-:].*|'
    r'[A-Z]{3,}_\d{5,})$', re.I)

def canon_id(i):
    """Ensembl writes gene IDs inconsistently across builds: some carry a 'gene-'
    or 'gene:' prefix, most carry a .N annotation-version suffix. Both must go, or
    every comparison silently fails."""
    i = re.sub(r'^gene[-:]', '', i.strip())
    return re.sub(r'\.\d+$', '', i)

def norm_name(s):
    return re.sub(r'[-_.\s;:/]', '', s).strip().lower()

def fetch(url, tries=3):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            if a == tries - 1:
                raise
            time.sleep(3 * (a + 1))

def listdir(url):
    try:
        html = fetch(url).decode('utf-8', 'replace')
    except Exception:
        return []
    return re.findall(r'href="([^"?/][^"]*)"', html)


def build_alias_index(gene_info_path):
    """GeneID -> set of real gene names, across every plant taxon at once."""
    idx = collections.defaultdict(set)
    with gzip.open(gene_info_path, 'rt') as fh:
        for line in fh:
            if line.startswith('#'):
                continue
            f = line.rstrip('\n').split('\t')
            if len(f) < 5:
                continue
            names = []
            if f[2] not in ('-', 'NEWENTRY'):
                names.append(f[2])
            if f[4] != '-':
                names += f[4].split('|')
            keep = [n for n in names if n and not PLACEHOLDER.match(n)]
            if keep:
                idx[f[1]].update(keep)
    return idx


def species_files(sp):
    """Pick the widest GFF3 (scaffold genes included) and the entrez bridge."""
    g = [f for f in listdir(f'{FTP}/gff3/{sp}/') if f.endswith('.gff3.gz')]
    full = [f for f in g if '.chr.' not in f and '.primary_assembly.' not in f
            and '.abinitio.' not in f
            and '.chromosome.' not in f and '.scaffold.' not in f]
    gff = (full or [f for f in g if '.chr.' in f] or [None])[0]
    ent = [f for f in listdir(f'{FTP}/tsv/{sp}/') if f.endswith('.entrez.tsv.gz')]
    return gff, (ent[0] if ent else None)


def parse_gff(blob):
    """gene lines -> (gene_id, [names]); also total gene count for coverage stats."""
    spine = collections.defaultdict(set)
    ngenes = 0
    with gzip.open(io.BytesIO(blob), 'rt', errors='replace') as fh:
        for line in fh:
            if line.startswith('#'):
                continue
            f = line.rstrip('\n').split('\t')
            if len(f) < 9 or f[2] != 'gene':
                continue
            ngenes += 1
            a = dict(p.split('=', 1) for p in f[8].split(';') if '=' in p)
            gid = a.get('gene_id') or a.get('ID', '')
            gid = canon_id(gid)
            if not gid:
                continue
            for key in ('Name', 'Alias'):
                if a.get(key):
                    for n in a[key].split(','):
                        n = urllib.parse.unquote(n).strip()
                        if n and not PLACEHOLDER.match(n):
                            spine[gid].add(n)
    return spine, ngenes


def do_species(sp, assembly_hint, alias_idx):
    gff_name, ent_name = species_files(sp)
    if not gff_name:
        return dict(species=sp, status='no gff3')
    # Assembly names contain dots (SL4.0, IRGSP-1.0), so splitting on '.' truncates them.
    # Filename is <Species>.<Assembly>.<release>[.chr].gff3.gz -- take everything between.
    m = re.match(r'^' + re.escape(sp[0].upper() + sp[1:]) +
                 r'\.(?P<asm>.+)\.(?P<rel>\d+)(\.chr|\.primary_assembly\.\w+)?\.gff3\.gz$',
                 gff_name)
    if m:
        assembly, release = m.group('asm'), m.group('rel')
    else:
        assembly, release = assembly_hint, '?'

    spine, ngenes = parse_gff(fetch(f'{FTP}/gff3/{sp}/{gff_name}'))

    # name -> {gene_id: {sources}}
    pairs = collections.defaultdict(lambda: collections.defaultdict(set))
    for gid, names in spine.items():
        for n in names:
            pairs[n][gid].add('ensembl_gff3')

    nbridge = 0
    if ent_name:
        blob = fetch(f'{FTP}/tsv/{sp}/{ent_name}')
        with gzip.open(io.BytesIO(blob), 'rt', errors='replace') as fh:
            for row in csv.DictReader(fh, delimiter='\t'):
                gid, xref = row.get('gene_stable_id'), row.get('xref')
                if not gid or not xref:
                    continue
                gid = canon_id(gid)
                for n in alias_idx.get(xref, ()):
                    pairs[n][gid].add('ncbi_geneid_join')
                    nbridge += 1

    OUT.joinpath('genes').mkdir(parents=True, exist_ok=True)
    path = OUT / 'genes' / f'{sp}.tsv.gz'
    nrows = 0
    with gzip.open(path, 'wt', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['name_norm', 'name_raw', 'gene_id', 'sources'])
        for n in sorted(pairs):
            for gid in sorted(pairs[n]):
                w.writerow([norm_name(n), n, gid, '|'.join(sorted(pairs[n][gid]))])
                nrows += 1

    by_norm = collections.defaultdict(set)
    for n in pairs:
        by_norm[norm_name(n)].update(pairs[n])
    amb = sum(1 for k, v in by_norm.items() if len(v) > 1)
    return dict(species=sp, status='ok', assembly=assembly, ensembl_release=release,
                n_genes=ngenes, n_named_genes=len(spine), n_names=len(by_norm),
                n_pairs=nrows, n_ambiguous=amb, bridge=('yes' if ent_name else 'no'),
                n_bridge_pairs=nbridge,
                pct_genes_named=round(100 * len(spine) / ngenes, 3) if ngenes else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gene-info', required=True)
    ap.add_argument('--species', help='comma-separated subset (default: all)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--resume', action='store_true')
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print('indexing NCBI aliases ...', file=sys.stderr, flush=True)
    alias_idx = build_alias_index(a.gene_info)
    print(f'  {len(alias_idx):,} GeneIDs carry a real gene name', file=sys.stderr, flush=True)

    rows = list(csv.DictReader(io.StringIO(
        fetch(f'{FTP}/species_EnsemblPlants.txt').decode('utf-8', 'replace')),
        delimiter='\t'))
    sp_list = [(r['species'], r.get('assembly', '')) for r in rows if r.get('species')]
    if a.species:
        want = set(a.species.split(','))
        sp_list = [s for s in sp_list if s[0] in want]
    if a.resume:
        sp_list = [s for s in sp_list if not (OUT / 'genes' / f'{s[0]}.tsv.gz').exists()]
    print(f'{len(sp_list)} species to build', file=sys.stderr, flush=True)

    man = OUT / 'manifest.tsv'
    new = not man.exists()
    fh = open(man, 'a', newline='')
    w = csv.writer(fh, delimiter='\t')
    if new:
        w.writerow(['species', 'status', 'assembly', 'ensembl_release', 'n_genes',
                    'n_named_genes', 'n_names', 'n_pairs', 'n_ambiguous', 'bridge',
                    'n_bridge_pairs', 'pct_genes_named'])
    done = 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(do_species, s, asm, alias_idx): s for s, asm in sp_list}
        for f in cf.as_completed(futs):
            sp = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = dict(species=sp, status=f'error: {type(e).__name__}: {e}'[:120])
            w.writerow([r.get(c, '') for c in
                        ['species', 'status', 'assembly', 'ensembl_release', 'n_genes',
                         'n_named_genes', 'n_names', 'n_pairs', 'n_ambiguous', 'bridge',
                         'n_bridge_pairs', 'pct_genes_named']])
            fh.flush()
            done += 1
            print(f'[{done}/{len(sp_list)}] {sp}: {r.get("status")} '
                  f'names={r.get("n_names","-")} pairs={r.get("n_pairs","-")}',
                  file=sys.stderr, flush=True)
    fh.close()

if __name__ == '__main__':
    main()

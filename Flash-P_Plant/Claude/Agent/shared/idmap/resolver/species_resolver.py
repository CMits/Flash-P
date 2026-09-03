#!/usr/bin/env python3
"""One species vocabulary, one identifier-pattern source, for every script in the skill.

Before this module the skill spoke three different species languages: the Ensembl
directory name (cache, databases, orthology), a capitalised English label such as 'Rice'
(literature), and a lowercase crop name such as 'tomato' (release dating). Passing the
correct Ensembl name to the release-dating step silently skipped set-membership dating
and fell back to an ambiguous prefix answer, with nothing to say a better method existed.

It also held two competing identifier-pattern tables. The curated one knows about parallel
systems the cache cannot see (MSU LOC_Os, maize GRMZM, sorghum Sobic.) because the cache
is built from one Ensembl release. The derived one covers all 267 species. They are now
unioned rather than made to compete: curated alternatives are added to, not replaced by,
the shape derived from the cache.

Usage as a library:
    from species_resolver import resolve_species
    sp = resolve_species('tomato')      # or 'Rice', 'Solanum lycopersicum', or the
    sp.ensembl_name                     # Ensembl directory name
    sp.id_pattern                       # union: curated systems + cache-derived shape
    sp.reference_pattern                # the cache release's shape, for labelling only
    sp.name_prefix                      # 'Sl' -- derived from genus + species initials

Usage as a command:
    python3 species_resolver.py --species tomato
    python3 species_resolver.py --list
"""
import argparse, collections, csv, functools, gzip, json, os, pathlib, re, sys

# Two roots. CACHE is the committed, read-only cache vendored beside this module; WRITE
# is the regenerable one under .flashp_cache/. Anything learned at run time goes to WRITE,
# so a run never modifies a tracked file. cache_read() lets a rebuilt layer in WRITE
# shadow the shipped copy without editing the repo.
_HERE = pathlib.Path(__file__).resolve().parent
CACHE = pathlib.Path(os.environ.get('FLASHP_IDMAP_RESOLVER', str(_HERE))) / 'cache'
WRITE = pathlib.Path(os.environ.get(
    'FLASHP_IDMAP_CACHE', str(_HERE.parents[3] / '.flashp_cache' / 'idmap')))


def cache_read(*parts):
    """Prefer a locally rebuilt cache file; fall back to the committed one."""
    built = WRITE.joinpath(*parts)
    return built if built.exists() else CACHE.joinpath(*parts)


def cache_write(*parts):
    """Where a learned or rebuilt cache file goes. Never the committed tree."""
    p = WRITE.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


GENES = CACHE / 'genes'

# ---------------------------------------------------------------------------
# Curated knowledge. Everything here is something that CANNOT be derived from the cache,
# because the cache is one Ensembl release of one assembly. Anything derivable is derived.
# ---------------------------------------------------------------------------

CURATED = {
    'oryza_sativa': dict(
        common=['rice', 'asian rice'], taxid=39947,
        # MSU is a parallel identifier system for the same genome; Ensembl carries RAP-DB
        # only, so the cache can never learn LOC_Os on its own.
        extra_id_patterns=[r'LOC_Os\d{2}g\d{5}'],
        literature_wildcards=('LOC_Os* OR Os01g* OR Os02g* OR Os03g* OR Os04g* OR Os05g* '
                              'OR Os06g* OR Os07g* OR Os08g* OR Os09g* OR Os10g* OR Os11g* '
                              'OR Os12g*')),
    'zea_mays': dict(
        common=['maize', 'corn'], taxid=4577,
        # B73 release history: GRMZM (v2/v3) -> Zm00001d (v4) -> Zm00001eb (v5).
        extra_id_patterns=[r'GRMZM\d[GT]\d{6}', r'Zm00001d\d{6}'],
        literature_wildcards='Zm00001d* OR Zm00001eb* OR GRMZM*'),
    'solanum_lycopersicum_gca000188115v5cm': dict(
        common=['tomato'], taxid=4081,
        literature_wildcards='Solyc*'),
    'hordeum_vulgare': dict(
        common=['barley'], taxid=4513,
        extra_id_patterns=[r'HORVU\dHr1G\d{6}'],          # MorexV1
        # Morex is the reference; BARKE, IGRI, BONUS and the rest of the pangenome are
        # DIFFERENT GENOMES, not different releases, so they are genuinely off-assembly.
        assembly_guard=(r'HORVU\.MOREX\.r3|HORVU\dHr1G', 'Morex (V1 or V3)'),
        literature_wildcards='HORVU*'),
    'triticum_aestivum': dict(
        common=['wheat', 'bread wheat'], taxid=4565,
        literature_wildcards='TraesCS*'),
    'sorghum_bicolor': dict(
        common=['sorghum'], taxid=4558,
        extra_id_patterns=[r'Sobic\.\d{3}G\d{6}'],        # Phytozome system
        literature_wildcards='Sobic* OR SORBI*'),
    'solanum_tuberosum': dict(
        common=['potato'], taxid=4113,
        extra_id_patterns=[r'Soltu\.DM\.\w+G\d+'],        # DM v6.1, newer than the cache
        literature_wildcards='PGSC0003DMG* OR Soltu.DM*'),
    'arabidopsis_thaliana': dict(
        common=['arabidopsis', 'thale cress'], taxid=3702,
        literature_wildcards='AT1G* OR AT2G* OR AT3G* OR AT4G* OR AT5G*'),
    'populus_trichocarpa': dict(
        common=['poplar', 'black cottonwood'], taxid=3694,
        literature_wildcards='Potri*'),
    'helianthus_annuus': dict(
        common=['sunflower'], taxid=4232,
        literature_wildcards='HanXRQ*'),
    'physcomitrium_patens': dict(
        common=['moss', 'physcomitrella'], taxid=3218,
        binomial_query='"Physcomitrium patens" OR "Physcomitrella patens"',
        literature_wildcards='Pp3c*'),
    'glycine_max': dict(common=['soybean', 'soya'], taxid=3847),
    'vitis_vinifera': dict(common=['grape', 'grapevine'], taxid=29760),
    'cucumis_sativus': dict(common=['cucumber'], taxid=3659),
    'brassica_napus': dict(common=['oilseed rape', 'canola'], taxid=3708),
    'brassica_oleracea': dict(common=['cabbage', 'broccoli'], taxid=3712),
    'capsicum_annuum': dict(common=['pepper', 'chilli'], taxid=4072),
    'musa_acuminata': dict(common=['banana'], taxid=4641),
    'beta_vulgaris': dict(common=['sugar beet', 'beet'], taxid=161934),
    'medicago_truncatula': dict(common=['barrel medic'], taxid=3880),
}

# Europe PMC treats a stem shorter than this as effectively unrestricted: 'A*' returns the
# same hitCount as no wildcard clause at all, so the identifier restriction silently stops
# restricting and the papers scanned may contain no identifiers whatsoever.
MIN_WILDCARD_STEM = 3


class Species(dict):
    """Attribute access over a plain dict, so it stays JSON-serialisable."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


@functools.lru_cache(maxsize=1)
def _species_dirs():
    return sorted(p.name[:-7] for p in GENES.glob('*.tsv.gz')) if GENES.exists() else []


@functools.lru_cache(maxsize=1)
def _manifest():
    m = cache_read('manifest.tsv')
    if not m.exists():
        return {}
    with open(m) as fh:
        return {r['species']: r for r in csv.DictReader(fh, delimiter='\t')
                if r.get('status') == 'ok'}


def binomial(species):
    """cucumis_sativus -> 'Cucumis sativus'.

    Ensembl appends assembly accessions to some names
    (solanum_lycopersicum_gca000188115v5cm) and cultivar or strain tokens to others."""
    parts = species.split('_')
    keep = []
    for i, p in enumerate(parts):
        if i >= 2 and (re.match(r'^gca\d', p) or re.match(r'^v\d', p) or len(p) > 14):
            break
        keep.append(p)
    if len(keep) > 2:
        keep = keep[:2]
    return ' '.join([keep[0].capitalize()] + keep[1:])


def _shape(i):
    r"""Csa_4G050830 -> Csa_\d+G\d+ -- a regex generalising the identifier's form."""
    out = []
    for tok in re.findall(r'\d+|[A-Za-z]+|[^A-Za-z0-9]', i.strip()):
        out.append(r'\d+' if tok.isdigit() else re.escape(tok))
    return ''.join(out)


@functools.lru_cache(maxsize=None)
def derived_pattern(species, coverage=0.9, max_alts=4):
    """The identifier shape of the release the cache was built from.

    Memoised: it decompresses the species file, 0.29 s for Brassica napus, and callers
    consult it once per gene name."""
    p = GENES / f'{species}.tsv.gz'
    if not p.exists():
        return None, 0.0
    ids = set()
    with gzip.open(p, 'rt') as fh:
        for r in csv.DictReader(fh, delimiter='\t'):
            ids.add(r['gene_id'])
    if not ids:
        return None, 0.0
    shapes = collections.Counter(_shape(i) for i in ids)
    chosen, seen = [], 0
    for sh, n in shapes.most_common(max_alts):
        chosen.append(sh); seen += n
        if seen / len(ids) >= coverage:
            break
    return '(?:' + '|'.join(chosen) + ')', seen / len(ids)


def _wildcard_stems(pattern):
    """Literal leading run of each regex alternative, for a Europe PMC wildcard clause.

    A backslash before punctuation is an escaped literal and is kept; a backslash before a
    letter is a character class (\\d, \\w) and ends the stem. Getting that wrong turns
    OE\\d+A\\d+ into the stem 'OEd'."""
    stems = []
    for alt in pattern.strip('(?:)').split('|'):
        out, i = [], 0
        while i < len(alt):
            c = alt[i]
            if c == '\\' and i + 1 < len(alt):
                if alt[i + 1].isalnum():
                    break
                out.append(alt[i + 1]); i += 2; continue
            if not (c.isalnum() or c == '_'):
                break
            out.append(c); i += 1
        if out:
            stems.append(''.join(out))
    return stems


@functools.lru_cache(maxsize=1)
def _alias_index():
    """alias -> ensembl directory name. Curated common names, binomials, and the
    directory names themselves. Built once from data already on disk."""
    idx = {}
    for sp in _species_dirs():
        idx.setdefault(sp, sp)
        idx.setdefault(binomial(sp).lower(), sp)
        # 'solanum_lycopersicum' should reach the accession-suffixed directory
        idx.setdefault(binomial(sp).lower().replace(' ', '_'), sp)
    for sp, meta in CURATED.items():
        target = sp if sp in idx else None
        if target is None:                      # curated name not in this cache build
            target = next((d for d in _species_dirs() if d.startswith(sp)), None)
        if target is None:
            continue
        idx[sp] = target
        for c in meta.get('common', []):
            idx[c.lower()] = target
            idx[c.lower().replace(' ', '_')] = target
    return idx


# An Ensembl directory name may carry an ASSEMBLY ACCESSION suffix, which still describes
# the same genome (solanum_lycopersicum_gca000188115v5cm), or a CULTIVAR suffix, which is a
# different genome (hordeum_vulgare_barke, triticum_aestivum_cadenza, oryza_sativa_ir64).
# Curated knowledge may only be inherited across the first kind. Matching on a bare prefix
# handed Morex's assembly guard to all 30 barley cultivar assemblies, which then discarded
# 100% of their own identifiers as "off-assembly" -- the exact failure this guard exists to
# prevent, reintroduced one layer up.
_ACCESSION_SUFFIX = re.compile(r'^(gca\d|gcf\d|asm\d|v\d+[a-z]*$)', re.I)


def _curated_for(ensembl_name):
    """Curated entry for this directory, inherited only across an accession suffix."""
    if ensembl_name in CURATED:
        return CURATED[ensembl_name]
    for k, v in CURATED.items():
        if ensembl_name.startswith(k + '_') and _ACCESSION_SUFFIX.match(ensembl_name[len(k) + 1:]):
            return v
    return {}


def resolve_species(token, strict=True):
    """Accept any of the three vocabularies and return one canonical record.

    'tomato', 'Tomato', 'Solanum lycopersicum', 'solanum_lycopersicum' and
    'solanum_lycopersicum_gca000188115v5cm' all resolve to the same species."""
    if not token:
        raise ValueError('no species given')
    key = str(token).strip().lower().replace(' ', '_')
    idx = _alias_index()
    name = idx.get(key) or idx.get(key.replace('_', ' '))
    if not name:
        hits = [d for d in _species_dirs() if d.startswith(key)]
        if len(hits) == 1:
            name = hits[0]
        elif strict:
            near = sorted(d for d in _species_dirs() if key.split('_')[0] in d)[:5]
            raise SystemExit(json.dumps(dict(
                error=f'unknown species {token!r}',
                did_you_mean=near or hits[:5],
                hint='accepts a common name (tomato), a binomial (Solanum lycopersicum), '
                     'or an Ensembl directory name. Run --list for all covered species. '
                     'A species outside Ensembl Plants has no reference annotation here '
                     'and needs sequence-level assignment instead.')))
        else:
            return None

    cur = _curated_for(name)
    meta = _manifest().get(name, {})
    derived, cov = derived_pattern(name)

    # Union, not competition: curated alternatives know about parallel systems and earlier
    # releases the cache cannot see; the derived shape covers every species.
    alts = list(cur.get('extra_id_patterns', []))
    if derived:
        alts.append(derived.strip('(?:)') if derived.startswith('(?:') else derived)
    id_pattern = '(?:' + '|'.join(a for a in alts if a) + ')' if alts else None

    parts = [x for x in name.split('_') if x]
    prefix = (parts[0][0] + parts[1][0]).capitalize() if len(parts) >= 2 else None

    # Literature wildcards: curated where a crop needed hand-tuning, otherwise derived from
    # the identifier shapes -- but never a stem so short it stops restricting anything.
    wild = cur.get('literature_wildcards')
    wildcard_ok = True
    if not wild:
        stems = [s for s in _wildcard_stems(id_pattern or '') if len(s) >= MIN_WILDCARD_STEM]
        if stems:
            wild = ' OR '.join(f'{s}*' for s in dict.fromkeys(stems))
        else:
            wild, wildcard_ok = None, False

    return Species(
        ensembl_name=name,
        binomial=binomial(name),
        binomial_query=cur.get('binomial_query') or f'"{binomial(name)}"',
        common=cur.get('common', []),
        taxid=cur.get('taxid'),
        name_prefix=prefix,
        id_pattern=id_pattern,
        reference_pattern=derived,
        reference_pattern_covers=round(cov, 3),
        assembly_guard=cur.get('assembly_guard', (None, None))[0],
        assembly_label=cur.get('assembly_guard', (None, None))[1],
        literature_wildcards=wild,
        literature_wildcards_ok=wildcard_ok,
        assembly=meta.get('assembly'),
        ensembl_release=meta.get('ensembl_release'),
        n_names=meta.get('n_names'),
        bridge=meta.get('bridge'),
        gff3_name_class=meta.get('gff3_name_class'),
        # 11 species are in the manifest as status=ok while holding no names at all. The
        # cache lookup used to answer "name absent from cache", which reads as "this cache
        # does not contain your gene" rather than "this cache is empty".
        cache_empty=(str(meta.get('n_names', '')) in ('0', '')),
        curated=bool(cur),
    )


TAXID_CACHE = CACHE / 'taxids.tsv'          # shipped seed
TAXID_LEARNED = WRITE / 'taxids.tsv'        # appended at run time


def lookup_taxid(species, offline_only=False):
    """NCBI taxon id for any Ensembl Plants species.

    Curated first, then a small on-disk cache, then Ensembl REST /info/genomes. Requiring
    the user to supply --taxid meant looking up a taxon id by hand for any of the 260
    species without a curated entry -- for a value Ensembl already publishes."""
    cur = _curated_for(species)
    if cur.get('taxid'):
        return cur['taxid']
    for _tc in (TAXID_LEARNED, TAXID_CACHE):
        if _tc.exists():
            with open(_tc) as fh:
                for line in fh:
                    k, _, v = line.partition('\t')
                    if k == species and v.strip().isdigit():
                        return int(v.strip())
    if offline_only:
        return None
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            f'https://rest.ensembl.org/info/genomes/{species}?content-type=application/json',
            headers={'User-Agent': 'gene-id-resolver/1.2'})
        with urllib.request.urlopen(req, timeout=45) as r:
            tid = _json.load(r).get('taxonomy_id')
        if tid:
            TAXID_LEARNED.parent.mkdir(parents=True, exist_ok=True)
            with open(TAXID_LEARNED, 'a') as fh:
                fh.write(f'{species}\t{int(tid)}\n')
            return int(tid)
    except Exception:
        pass
    return None


def allow_leading_hyphen(argv, opts):
    """Rewrite `--opt -Value` as `--opt=-Value` so argparse keeps it as a value.

    Real gene names start with a hyphen -- maize carries -Mlo1 through -Mlo9 -- and
    argparse would otherwise read the value as an unknown flag and exit. Lives here so
    the five scripts that need it share one copy."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in opts and i + 1 < len(argv) and argv[i + 1].startswith('-'):
            out.append(f'{a}={argv[i + 1]}'); i += 2; continue
        out.append(a); i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--species')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--aliases', action='store_true', help='show every accepted alias')
    a = ap.parse_args()
    if a.list:
        for s in _species_dirs():
            print(s)
        return
    if a.aliases:
        for k, v in sorted(_alias_index().items()):
            if k != v:
                print(f'{k}\t{v}')
        return
    if not a.species:
        ap.error('--species, --list or --aliases')
    print(json.dumps(resolve_species(a.species)))


if __name__ == '__main__':
    main()

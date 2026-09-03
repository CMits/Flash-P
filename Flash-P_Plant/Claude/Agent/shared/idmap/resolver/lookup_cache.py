#!/usr/bin/env python3
"""Offline lookup of gene names against the prebuilt Ensembl Plants name cache.

Answers come from one assembly only -- the one the cache was built from -- so a
wrong-assembly identifier cannot be returned. Ambiguity is reported, never
collapsed: a name matching 39 genes returns 39 candidates and needs_review=true,
because a 39-member answer set is not something you order a knockout from.

Usage:
    python3 lookup_cache.py --species barley --names MLO,CBF12,nac1
    python3 lookup_cache.py --species tomato --names-file n.txt
    python3 lookup_cache.py --list-species | grep -i hordeum
Output: one JSON object per line.

--names is a COMMA-SEPARATED LIST; --names-file is a path.
"""
import argparse, collections, csv, gzip, importlib.util, json, pathlib, sys

_s = importlib.util.spec_from_file_location(
    'species_resolver', pathlib.Path(__file__).with_name('species_resolver.py'))
_sr = importlib.util.module_from_spec(_s); _s.loader.exec_module(_sr)

# Reuse Step 1 normalisation rather than reimplementing it -- the prefix rule is subtle
# (it must not turn 'STH1' into 'h1') and two copies would drift apart. An earlier version
# kept a second copy of the punctuation regex here for exactly that reason.
_n = importlib.util.spec_from_file_location(
    'normalize_name', pathlib.Path(__file__).with_name('normalize_name.py'))
_nn = importlib.util.module_from_spec(_n); _n.loader.exec_module(_nn)

GENES = _sr.GENES


def plain_key(s):
    """Punctuation and case only -- normalise() with prefix stripping disabled."""
    return _nn.normalise(s, strip_prefix=False)['normalised']


def load(species):
    p = GENES / f'{species}.tsv.gz'
    if not p.exists():
        sys.exit(json.dumps(dict(error=f'no cache for species {species!r}',
                                 hint='run --list-species')))
    idx = collections.defaultdict(lambda: collections.defaultdict(set))
    with gzip.open(p, 'rt') as fh:
        for r in csv.DictReader(fh, delimiter='\t'):
            idx[r['name_norm']][r['gene_id']].update(r['sources'].split('|'))
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--species', help='common name, binomial or Ensembl name')
    ap.add_argument('--names', help='COMMA-SEPARATED list of gene names, not a filename')
    ap.add_argument('--names-file', dest='names_file', help='path, one name per line')
    ap.add_argument('--species-prefix',
                    help='e.g. Os — overrides the prefix derived from --species')
    ap.add_argument('--max-candidates', type=int, default=25,
                    help='above this a name is reported as too ambiguous to use')
    ap.add_argument('--list-species', action='store_true')
    a = ap.parse_args(_sr.allow_leading_hyphen(
        sys.argv[1:], ('--names', '--names-file', '--species', '--species-prefix')))

    if a.list_species:
        for p in sorted(GENES.glob('*.tsv.gz')):
            print(p.name[:-7])
        return
    if not a.species:
        ap.error('--species is required')

    sp = _sr.resolve_species(a.species)
    idx = load(sp.ensembl_name)
    names = []
    if a.names:
        names += [x.strip() for x in a.names.split(',') if x.strip()]
    if a.names_file:
        names += [l.strip() for l in open(a.names_file) if l.strip()]
    if not names:
        ap.error('supply --names or --names-file')

    # The prefix is derived from the species rather than demanded from the user. It used
    # to come only from --species-prefix, so for any species outside the curated handful
    # the prefix-stripped key was never tried -- in the step that supplies 94% of answers.
    prefix = a.species_prefix or sp.name_prefix

    for raw in names:
        # Plain key first, then the prefix-stripped key, then the key with the species
        # prefix added back -- the cache may hold either form.
        stripped = _nn.normalise(raw, prefix)['normalised']
        plain = plain_key(raw)
        keys = [plain, stripped]
        if prefix:
            keys.append(plain_key(prefix + raw))
        hits, matched_via = {}, None
        for k in keys:
            if k in idx:
                hits = idx[k]
                matched_via = ('exact' if k == plain else
                               'prefix_stripped' if k == stripped else 'prefix_added')
                break
        ids = sorted(hits)
        # a name backed by both sources independently is worth more than one backed by either
        corroborated = [i for i in ids if len(hits[i]) > 1]
        if not ids:
            note = ('the cache holds NO names at all for this species, so this is not '
                    'evidence about your gene; go straight to the database and literature '
                    'steps' if sp.cache_empty else
                    'name absent from cache; try the database and literature steps')
            out = dict(name=raw, resolved=False, candidates=[], confidence='none',
                       needs_review=True, note=note, cache_empty=sp.cache_empty)
        elif len(ids) == 1:
            # A match found only after stripping the species prefix is a coin flip:
            # 2 correct / 2 wrong on the held-out rice set, against 93% for exact keys.
            # Keep it -- it is a real lead -- but never present it as settled.
            if matched_via == 'exact':
                conf = 'high' if len(hits[ids[0]]) > 1 else 'medium'
                review, note = False, None
            else:
                conf, review = 'low', True
                note = (f'matched only after {matched_via.replace("_", " ")}; this route ran '
                        f'50% precision in benchmarking, so confirm before use')
            out = dict(name=raw, resolved=True, gene_id=ids[0], candidates=ids,
                       n_candidates=1, sources=sorted(hits[ids[0]]),
                       confidence=conf, needs_review=review)
            if note:
                out['note'] = note
        else:
            # Never promote one of several candidates to a resolved answer. Barley MLO
            # is named on 14 genes; picking the one both sources happen to agree on
            # returns a confident wrong gene, which is worse than returning the set.
            shown = ids if len(ids) <= a.max_candidates else ids[:a.max_candidates]
            out = dict(name=raw, resolved=False, candidates=shown, n_candidates=len(ids),
                       corroborated=corroborated[:a.max_candidates],
                       confidence='ambiguous', needs_review=True,
                       note=f'{len(ids)} candidates; disambiguate before use'
                            + (f' ({len(corroborated)} backed by both sources)' if corroborated else ''))
        out['matched_via'] = matched_via
        out['species'] = sp.ensembl_name
        out['assembly'] = sp.assembly or ''
        out['ensembl_release'] = sp.ensembl_release or ''
        # Caveats accumulate. They used to be assigned to the same key in sequence, so a
        # species tripping both conditions would silently lose the first warning.
        caveats = []
        if sp.gff3_name_class == 'identifier_alias':
            caveats.append("this species' GFF3 Name field holds a second identifier system, "
                           "not gene names; treat hits as cross-references")
        if sp.bridge == 'no':
            caveats.append('no NCBI GeneID bridge for this species; cache holds Ensembl '
                           'display names only, so coverage is much lower')
        if caveats:
            out['caveats'] = caveats
        print(json.dumps(out))


if __name__ == '__main__':
    main()

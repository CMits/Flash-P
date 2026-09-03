#!/usr/bin/env python3
"""Tier-0 gene name normalisation: case, punctuation, and species prefix only.

Deliberately stops short of stripping trailing digits. Measured on the Ensembl rice
name set, that extra step raises matching by 4.8 points and makes 81.7% of all named
genes ambiguous (447 genes collapse onto 'fbox'). The trailing number is the paralogue.

Usage:
    python3 normalize_name.py --name "OsGH3.8"
    python3 normalize_name.py --name CsWRKY1 --species cucumber
    python3 normalize_name.py --names-file names.txt --species-prefix Os
Output: JSON lines with raw, normalised, and the prefix removed (if any).
"""
import argparse, functools, importlib.util, json, pathlib, re, sys

_s = importlib.util.spec_from_file_location(
    'species_resolver', pathlib.Path(__file__).with_name('species_resolver.py'))
_sr = importlib.util.module_from_spec(_s); _s.loader.exec_module(_sr)


@functools.lru_cache(maxsize=1)
def known_prefixes():
    """Every genus+species initial pair in the cache, e.g. Os, At, Sl, Cs, Bv.

    Plant gene-name prefixes follow that convention, so the set is derivable rather than
    curated. The previous hardcoded list held 17 prefixes for 267 covered species, and a
    name from any other species simply kept its prefix."""
    out = set()
    for sp in _sr._species_dirs():
        parts = [x for x in sp.split('_') if x]
        if len(parts) >= 2:
            out.add((parts[0][0] + parts[1][0]).lower())
    return sorted(out)


def normalise(name, species_prefix=None, strip_prefix=True):
    """Case-fold, strip punctuation, strip a species prefix.

    The prefix is only stripped when the ORIGINAL string follows the plant naming
    convention of Xx + capital/digit (OsGH3.8, SlGLK2, HvGLK2). Without that check,
    'STH1' loses a spurious 'St' and becomes 'h1', and 'Stt3a' becomes 't3a' -- both
    observed on held-out rice names.
    """
    raw = name.strip()
    clean = re.sub(r'[-_.\s;:/]', '', raw)
    key = clean.lower()
    removed = None
    if not strip_prefix:
        return dict(raw=raw, normalised=key, prefix_removed=None)
    candidates = ([species_prefix.capitalize()] if species_prefix
                  else [p.capitalize() for p in known_prefixes()])
    for p in candidates:
        if len(clean) <= len(p) + 1:
            continue
        # exact Xx casing in the original, followed by uppercase or digit
        if clean[:len(p)] != p:
            continue
        nxt = clean[len(p)]
        if not (nxt.isupper() or nxt.isdigit()):
            continue
        rest = key[len(p):]
        if any(c.isalpha() for c in rest):
            removed = p.lower()
            key = rest
            break
    return dict(raw=raw, normalised=key, prefix_removed=removed)


def prefix_for(species):
    """Name prefix for any species in any of the accepted vocabularies."""
    if not species:
        return None
    sp = _sr.resolve_species(species, strict=False)
    return sp.name_prefix if sp else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', help='a single gene name')
    ap.add_argument('--names', help='COMMA-SEPARATED list of gene names, not a filename')
    ap.add_argument('--file', dest='names_file', help='path to a file, one name per line')
    ap.add_argument('--names-file', dest='names_file', help=argparse.SUPPRESS)
    ap.add_argument('--species-prefix', help='e.g. Os — restricts prefix stripping to this one')
    ap.add_argument('--species', help='common name, binomial or Ensembl name; the prefix '
                                      'is derived from it (genus + species initials)')
    a = ap.parse_args(_sr.allow_leading_hyphen(
        sys.argv[1:], ('--name', '--names', '--file', '--names-file',
                       '--species-prefix', '--species')))

    prefix = a.species_prefix or prefix_for(a.species)
    names = []
    if a.name: names.append(a.name)
    if a.names: names += [x.strip() for x in a.names.split(',') if x.strip()]
    if a.names_file: names += [l.strip() for l in open(a.names_file) if l.strip()]
    if not names:
        ap.error('supply --name, --names or --file')
    for n in names:
        print(json.dumps(normalise(n, prefix)))


if __name__ == '__main__':
    main()

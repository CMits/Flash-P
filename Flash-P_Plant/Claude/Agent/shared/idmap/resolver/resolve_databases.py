#!/usr/bin/env python3
"""Query Ensembl Plants, NCBI Gene and UniProt concurrently and pool the results.

Pooling matters: the three sources agree on only 13% of cases and are largely disjoint.
Union recall is 61% against 43% for the best single source. Ensembl favours primary
published symbols, NCBI favours older secondary synonyms.

UniProt is queried by free text, which recalls ~34.6% against 9.2% for its structured
gene-name field, at the cost of ~5 candidates per query. Treat it as a candidate
generator and expect to filter.

Usage:
    python3 resolve_databases.py --names SD1,GN1A --species oryza_sativa --taxid 39947
    python3 resolve_databases.py --names-file names.txt --species oryza_sativa --taxid 39947
    python3 resolve_databases.py --name SD1 --species oryza_sativa --taxid 39947
Output: JSON lines, one per input name, with per-source candidate lists.

--names is a COMMA-SEPARATED LIST; --names-file is a path. Passing a filename to --names
silently treats it as a gene name and reports a clean unresolved.
"""
import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.parse, urllib.error

UA = 'gene-id-resolver/1.2 (plant gene name resolution)'

import importlib.util, pathlib as _pl
_s = importlib.util.spec_from_file_location(
    'species_resolver', _pl.Path(__file__).with_name('species_resolver.py'))
_sr = importlib.util.module_from_spec(_s); _s.loader.exec_module(_sr)

# Returned by fetch() when the request never completed -- rate limit, timeout, DNS, 5xx.
# Distinct from None, which means the server answered and holds nothing. Collapsing the
# two turns an outage into a confident "this gene is in no database"; that is the failure
# mode already found and fixed in the literature step, and it was never fixed here.
UNAVAILABLE = object()

# ---------------------------------------------------------------------------
# Assembly guards vs release classification
#
# These are different things and conflating them discarded correct answers.
#
#   ASSEMBLY mismatch  -- a different genome. UniProt's barley cross-references point at
#                         cultivar pangenome assemblies (BARKE, IGRI, BONUS) and contain no
#                         Morex entries; 7 of 21 barley names were answered this way in
#                         held-out testing. Those identifiers are unusable and are removed.
#
#   RELEASE or SYSTEM  -- the SAME genome under a different annotation build or a parallel
#     difference          identifier system. Zm00001d032789 (B73 v4) and Zm00001eb391880
#                         (v5) are one gene; LOC_Os01g66100 (MSU) and Os01g0883800 (RAP-DB)
#                         are one gene. These are labelled, never removed.
#
# The previous version derived a guard from one release's identifier shapes and applied it
# as a discard filter to every species. That threw away every NCBI identifier for maize,
# sorghum and potato -- NCBI being the source that is best on the older secondary synonyms
# which are precisely the ones carrying older identifiers.
# ---------------------------------------------------------------------------

# Assembly guards and identifier patterns now live in species_resolver.CURATED, so the
# three scripts that need them cannot drift apart. The curated alternatives (GRMZM,
# LOC_Os, Sobic., MorexV1) are unioned with the cache-derived shape rather than
# competing with it -- previously the curated table drove extraction while the derived
# one drove the guard, so maize identifiers were extracted and then discarded.


def classify_identifiers(pooled, species, assembly_pattern=None):
    """Split pooled identifiers into on-reference, other-release/system, and off-assembly.

    Only an assembly mismatch removes an identifier from the answer. The reference pattern
    is anchored with fullmatch: the old code used search, so 'Os\\d+g\\d+' matched inside
    'LOC_Os05g22940' and rice escaped the discard bug by accident rather than by design."""
    sp = _sr.resolve_species(species, strict=False)
    guard = assembly_pattern or (sp.assembly_guard if sp else None)
    off = []
    keep = list(pooled)
    if guard:
        rx = re.compile(guard)
        off = [i for i in keep if not rx.search(i)]
        keep = [i for i in keep if rx.search(i)]
    ref = sp.reference_pattern if sp else None
    if ref:
        rrx = re.compile(ref)
        on_ref = [i for i in keep if rrx.fullmatch(i)]
        other = [i for i in keep if not rrx.fullmatch(i)]
    else:
        on_ref, other = keep, []
    return dict(pooled=sorted(keep), on_reference=sorted(on_ref),
                other_release_or_system=sorted(other), off_assembly=sorted(off),
                assembly_pattern=guard, reference_pattern=ref)

def fetch(url, tries=3, timeout=45):
    """Body on success, None when the server answered with nothing, UNAVAILABLE on failure."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None                      # answered: nothing there
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return UNAVAILABLE

def ensembl(name, species):
    q = urllib.parse.quote(name, safe='')
    t = fetch(f'https://rest.ensembl.org/xrefs/symbol/{species}/{q}?content-type=application/json')
    if t is UNAVAILABLE:
        return [], 'unavailable'
    if not t:
        return [], 'ok'
    try:
        return sorted({re.sub(r'^gene-', '', x['id'])
                       for x in json.loads(t) if x.get('type') == 'gene'}), 'ok'
    except Exception:
        return [], 'unavailable'                 # a body we cannot parse is not an answer

def uniprot(name, taxid, pat):
    q = urllib.parse.urlencode({
        'query': f'"{name}" AND organism_id:{taxid}',
        'fields': 'accession,gene_names,xref_ensemblplants',
        'format': 'tsv', 'size': 25})
    t = fetch(f'https://rest.uniprot.org/uniprotkb/search?{q}')
    if t is UNAVAILABLE:
        return [], 'unavailable'
    if not t:
        return [], 'ok'
    ids = set(re.findall(pat, t))
    # EnsemblPlants transcript forms, e.g. Os01t0883800-02 -> Os01g0883800
    for a, b in re.findall(r'\bOs(\d{2})t(\d{7})\b', t):
        ids.add(f'Os{a}g{b}')
    return sorted(ids), 'ok'

def ncbi(name, taxid, pat):
    """Entrez esearch on gene symbol, then esummary for the locus tag.

    Without an API key E-utilities allows 3 requests/second; a 429 here used to come back
    as an empty list, indistinguishable from a gene NCBI genuinely does not hold."""
    q = urllib.parse.quote(f'{name}[sym] AND txid{taxid}[orgn]', safe='')
    t = fetch('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi'
              f'?db=gene&term={q}&retmode=json&retmax=10')
    if t is UNAVAILABLE:
        return [], 'unavailable'
    if not t:
        return [], 'ok'
    try:
        uids = json.loads(t)['esearchresult'].get('idlist', [])
    except Exception:
        return [], 'unavailable'
    if not uids:
        return [], 'ok'
    t2 = fetch('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
               f'?db=gene&id={",".join(uids[:10])}&retmode=json')
    if t2 is UNAVAILABLE:
        return [], 'unavailable'
    if not t2:
        return [], 'ok'
    ids = set(re.findall(pat, t2))
    # locus tags carry the stable ID for several species; rice encodes RAP in OSNPB_
    for a, b in re.findall(r'OSNPB_(\d{2})(\d{7})', t2):
        ids.add(f'Os{a}g{b}')
    for m in re.findall(r'ZEAMMB73_(Zm00001[de]b?\d{6})', t2):
        ids.add(m)
    return sorted(ids), 'ok'

def resolve(name, species, taxid, pat, assembly=None):
    with ThreadPoolExecutor(max_workers=3) as ex:
        fe = ex.submit(ensembl, name, species)
        fu = ex.submit(uniprot, name, taxid, pat)
        fn = ex.submit(ncbi, name, taxid, pat)
        (e, es), (u, us), (n, ns) = fe.result(), fu.result(), fn.result()
    status = {'ensembl': es, 'ncbi': ns, 'uniprot': us}
    down = sorted(k for k, v in status.items() if v == 'unavailable')
    pooled = sorted(set(e) | set(u) | set(n))

    cls = classify_identifiers(pooled, species, assembly)
    out = dict(name=name, species=species, ensembl=e, ncbi=n, uniprot=u,
               n_sources=sum(1 for x in (e, n, u) if x),
               source_status=status)
    out.update(cls)
    out['resolved'] = bool(cls['pooled'])

    notes = []
    if cls['other_release_or_system']:
        notes.append(f"{len(cls['other_release_or_system'])} identifier(s) are the same genome "
                     f"under a different annotation release or identifier system; reconcile "
                     f"them with Step 3 rather than treating them as conflicts")
    if cls['off_assembly']:
        notes.append(f"{len(cls['off_assembly'])} identifier(s) removed as a different assembly "
                     f"(not merely a different release)")
    if notes:
        out['warning'] = '; '.join(notes)

    # An unreachable source is not a negative result. Say so, and never let a partial or
    # empty answer be read as evidence the name is absent from these databases.
    if down:
        out['sources_unavailable'] = down
        if not cls['pooled']:
            out.update(resolved=False, confidence='unavailable', needs_review=True,
                       error=f"{', '.join(down)} did not respond, so the query did not run "
                             f"against {'them' if len(down) > 1 else 'it'}. This is not "
                             f"evidence that no database holds {name}.")
        else:
            out['needs_review'] = True
            out['note'] = (f"answer is partial: {', '.join(down)} did not respond, so the "
                           f"pooled union is incomplete")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', help='a single gene name')
    ap.add_argument('--names', help='COMMA-SEPARATED list of gene names, not a filename')
    ap.add_argument('--names-file', dest='names_file', help='path to a file, one name per line')
    ap.add_argument('--species', required=True,
                    help='common name (rice), binomial, or Ensembl Plants name')
    ap.add_argument('--taxid', type=int,
                    help='NCBI taxon id; looked up from Ensembl and cached if omitted')
    ap.add_argument('--id-pattern', help='override the identifier regex for this species')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--assembly-pattern',
                    help='regex an identifier must contain to be on the reference ASSEMBLY. '
                         'Off-assembly hits are removed. Release and identifier-system '
                         'differences are labelled instead, never removed.')
    a = ap.parse_args(_sr.allow_leading_hyphen(
        sys.argv[1:], ('--name', '--names', '--names-file', '--species',
                       '--id-pattern', '--assembly-pattern')))

    # Fall back to a profile derived from the cache, so species without a hardcoded
    # entry still work. Only species absent from Ensembl Plants entirely now hard-stop.
    sp = _sr.resolve_species(a.species)
    pat = a.id_pattern or sp.id_pattern
    taxid = a.taxid or _sr.lookup_taxid(sp.ensembl_name)
    if not taxid:
        sys.exit(f'could not determine an NCBI taxon id for {sp.ensembl_name}; pass --taxid')
    if not pat:
        sys.exit(f'no identifier pattern known for {a.species} and no cache entry to derive '
                 f'one from. Run species_profile.py --list to see covered species, or pass '
                 f'--id-pattern. A species outside Ensembl Plants has no reference annotation '
                 f'here and needs sequence-level assignment instead.')
    species = sp.ensembl_name

    names = []
    if a.name: names.append(a.name)
    if a.names: names += [x.strip() for x in a.names.split(',') if x.strip()]
    if a.names_file:
        names += [l.strip() for l in open(a.names_file) if l.strip()]
    if not names: ap.error('supply --name, --names or --names-file')

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for out in ex.map(lambda n: resolve(n, species, taxid, pat,
                                            a.assembly_pattern), names):
            print(json.dumps(out), flush=True)

if __name__ == '__main__':
    main()

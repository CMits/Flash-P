#!/usr/bin/env python3
"""Reconstruct a Step 1.6 evidence file for a Flash-P network built before Step 1.6 existed.

Older networks carry their provenance in a thinner form: `curated_edges.json` gives every
edge a DOI, `perturbation_dataset.json` gives every perturbation a DOI and the species the
claim was made in. What they do not carry is the thing the identifier mapper actually reads
-- the sentence. `build_dossiers.py` treats a record with no `evidence` text as claimed but
ungrounded, so a file rebuilt from DOIs alone would quarantine every node and be worth
nothing.

So this fetches the papers. Metadata and abstracts come from Europe PMC; open-access full
text is downloaded where it exists and cached alongside, which is what lets
`mine_evidence_ids.py` find accessions. For each edge and each perturbation it then locates
a sentence that actually names the genes involved, and that sentence becomes the evidence.

Two things this deliberately does not do.

It does not write into the Flash-P network directory. The output is an overlay -- a
directory holding a copy of network.json plus the reconstructed `data/evidence.json` and
`data/fulltext/` -- so the source network stays exactly as Flash-P left it and every
downstream script can be pointed at the overlay with no changes.

It does not claim the sentences were verified. Flash-P's own `verification: "verified"`
means a judge read the claim and confirmed it against the source. A sentence found here was
selected because it mentions the right gene names, which is a much weaker thing. These are
written as `verification: "backfilled"` and carry a lower confidence, so anything reading
the dossier can tell the two apart -- and `species` is filled in only where the legacy file
recorded it, never guessed from the text.

    python Agent/shared/idmap/backfill_evidence.py \\
        --network /path/to/Days_To_Flowering \\
        --out networks/Days_To_Flowering/idmapping/backfilled
"""
import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# What a reconstructed sentence is worth. Below anything Flash-P's judge produces, because
# a name occurring in a sentence is not the same as a claim having been checked against it.
BACKFILL_CONFIDENCE_BOTH = 0.55      # the sentence names both ends of the edge
BACKFILL_CONFIDENCE_ONE = 0.40       # it names one of them


def _get(url, timeout=30, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return fh.read()
        except Exception as exc:
            if attempt == tries - 1:
                print(f"    fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def epmc_record(doi):
    q = urllib.parse.quote(f'DOI:"{doi}"')
    raw = _get(f"{EPMC}/search?query={q}&resultType=core&format=json&pageSize=1")
    if not raw:
        return {}
    try:
        res = json.loads(raw).get("resultList", {}).get("result") or []
    except Exception:
        return {}
    return res[0] if res else {}


TAG = re.compile(r"<[^>]+>")


def epmc_fulltext(pmcid):
    """Open-access full text as plain text, or '' when the paper is not open access."""
    raw = _get(f"{EPMC}/{pmcid}/fullTextXML", timeout=60)
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    # Drop the reference list: it is a dense source of gene names that belong to other
    # papers' titles, and mining it produces pairings that were never asserted here.
    text = re.sub(r"<ref-list.*?</ref-list>", " ", text, flags=re.S)
    text = re.sub(r"<\?[^>]*\?>", " ", text)
    text = TAG.sub(" ", text)
    text = (text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                .replace("&#x2019;", "'").replace("&#x2013;", "-"))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def name_forms(name):
    """Spellings of a node name a paper might use.

    A sorghum symbol is written SbPHYB, Sbphyb, or bare PHYB depending on the journal, and
    Flash-P's own node ids arrive underscored. The bare form is only offered when it is long
    enough to be distinctive -- dropping a two-letter prefix off a short name produces
    matches on unrelated words.
    """
    out = {name, name.replace("_", "-"), name.replace("_", "")}
    m = re.match(r"^(?:Sb|At|Os|Zm|Ta|Hv|Sl)([A-Z0-9].*)$", name)
    if m and len(m.group(1)) >= 3:
        out.add(m.group(1))
    return {o for o in out if len(o) >= 3}


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def sentences_of(text):
    for block in (text or "").split("\n"):
        block = block.strip()
        if len(block) < 40:
            continue
        for s in SENT_SPLIT.split(block):
            s = s.strip()
            if 40 <= len(s) <= 600:
                yield s


def mentions(sentence, forms):
    low = sentence.lower()
    return any(re.search(r"\b" + re.escape(f).lower() + r"(?![a-z0-9])", low) for f in forms)


def pick_sentence(corpus, primary, secondary=None):
    """Best sentence naming `primary`, preferring one that also names `secondary`.

    Returns (sentence, locator, names_both) or (None, '', False).
    """
    best_one = None
    for label, text in corpus:
        for s in sentences_of(text):
            if not mentions(s, primary):
                continue
            if secondary and mentions(s, secondary):
                return s, label, True
            if best_one is None:
                best_one = (s, label)
    if best_one:
        return best_one[0], best_one[1], False
    return None, "", False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", required=True, help="the legacy Flash-P network directory")
    ap.add_argument("--out", required=True,
                    help="overlay directory to write; the source network is not modified")
    ap.add_argument("--no-fulltext", action="store_true",
                    help="abstracts only; skip the open-access full-text download")
    args = ap.parse_args()

    src = args.network
    ce_path = os.path.join(src, "data", "curated_edges.json")
    net_path = os.path.join(src, "network", "network.json")
    if not os.path.exists(ce_path) or not os.path.exists(net_path):
        sys.exit(f"{src} does not look like a legacy Flash-P network "
                 "(need network/network.json and data/curated_edges.json)")

    ce = common.load_json(ce_path)
    net = common.load_json(net_path)
    species = (net.get("metadata", {}).get("species")
               or ce.get("metadata", {}).get("species") or "")

    perts = []
    for fn in ("reconciled_perturbation_dataset.json", "perturbation_dataset.json"):
        p = os.path.join(src, "data", fn)
        if os.path.exists(p):
            perts = common.load_json(p).get("perturbations", [])
            break
    # The reconciled file drops the DOI and species the original carried, so the two are
    # joined on the test id rather than one being used alone.
    orig = {}
    p0 = os.path.join(src, "data", "perturbation_dataset.json")
    if os.path.exists(p0):
        orig = {r.get("id"): r for r in common.load_json(p0).get("perturbations", [])}

    edges = ce.get("edges", [])
    dois = []
    for r in edges:
        if r.get("d") and r["d"] not in dois:
            dois.append(r["d"])
    for r in perts:
        d = r.get("d") or (orig.get(r.get("id")) or {}).get("d")
        if d and d not in dois:
            dois.append(d)

    print(f"{len(edges)} edges, {len(perts)} perturbations, {len(dois)} distinct DOIs",
          file=sys.stderr)

    ft_dir = os.path.join(args.out, "data", "fulltext")
    os.makedirs(ft_dir, exist_ok=True)
    os.makedirs(os.path.join(args.out, "network"), exist_ok=True)

    papers, corpora, n_ft = {}, {}, 0
    for i, doi in enumerate(dois, 1):
        rec = epmc_record(doi)
        papers[doi] = {"doi": doi, "title": rec.get("title", ""),
                       "year": rec.get("pubYear", ""), "journal": rec.get("journalTitle", ""),
                       "pmcid": rec.get("pmcid", ""),
                       "open_access": rec.get("isOpenAccess", "") == "Y",
                       # The two keys mine_evidence_ids.py joins on. Without them the cached
                       # text sits on disk unread, which is where the accessions are.
                       "has_fulltext": False, "fulltext_file": ""}
        blocks = []
        abstract = rec.get("abstractText") or ""
        if abstract:
            blocks.append(("abstract", TAG.sub(" ", abstract)))
        if not args.no_fulltext and rec.get("pmcid") and rec.get("isOpenAccess") == "Y":
            ft = epmc_fulltext(rec["pmcid"])
            if ft:
                safe = re.sub(r"[^A-Za-z0-9]+", "_", doi) + ".txt"
                with open(os.path.join(ft_dir, safe), "w") as fh:
                    fh.write(ft)
                papers[doi]["has_fulltext"] = True
                papers[doi]["fulltext_file"] = safe
                blocks.append(("full_text", ft))
                n_ft += 1
        corpora[doi] = blocks
        print(f"  [{i}/{len(dois)}] {doi} "
              f"{'OA full text' if len(blocks) > 1 else 'abstract only' if blocks else 'NOTHING'}",
              file=sys.stderr)

    stats = collections.Counter()

    def record(doi, primary, secondary=None):
        sent, loc, both = pick_sentence(corpora.get(doi, []), primary, secondary)
        if not sent:
            stats["ungrounded"] += 1
            return "", "", 0.0
        stats["both" if both else "one"] += 1
        return sent, f"backfilled:{loc}", (BACKFILL_CONFIDENCE_BOTH if both
                                           else BACKFILL_CONFIDENCE_ONE)

    ev_edges = []
    for r in edges:
        s_raw, t_raw, doi = r.get("s"), r.get("t"), r.get("d", "")
        sent, loc, conf = record(doi, name_forms(t_raw or ""), name_forms(s_raw or ""))
        ev_edges.append({
            "eid": r.get("eid", ""), "s": s_raw, "t": t_raw, "x": r.get("x", 1),
            "doi": doi, "evidence": sent, "source_locator": loc,
            # Left empty on purpose. The legacy edge file records no species, and reading one
            # out of the sentence is how "Arabidopsis hub" and "Sorghum stay" get created.
            "species": "", "species_source": "",
            "confidence": conf,
            "verification": "backfilled" if sent else "unverified",
        })

    ev_perts = []
    for r in perts:
        o = orig.get(r.get("id")) or {}
        doi = r.get("d") or o.get("d", "")
        gene = r.get("g") or o.get("g", "")
        sent, loc, conf = record(doi, name_forms(gene or ""))
        ev_perts.append({
            "id": r.get("id", ""), "g": gene, "pt": r.get("pt") or o.get("pt", ""),
            "ed": r.get("ed") or o.get("ed", ""), "doi": doi,
            "evidence": sent, "source_locator": loc,
            # This one the legacy file does record, so it is carried across rather than guessed.
            "species": r.get("sp") or o.get("sp", ""),
            "species_source": "legacy_perturbation_record" if (r.get("sp") or o.get("sp")) else "",
            "confidence": conf,
            "verification": "backfilled" if sent else "unverified",
        })

    out_ev = {
        "metadata": {
            "species": species,
            "backfilled_from": os.path.abspath(src),
            "backfilled_by": "backfill_evidence.py",
            "note": ("Reconstructed from a pre-Step-1.6 network. Sentences were selected "
                     "because they name the genes in the claim, not because a judge "
                     "verified the claim against them; verification is 'backfilled'."),
            "n_papers": len(papers), "n_fulltext": n_ft,
        },
        "papers": papers,
        "edges": ev_edges,
        "perturbations": ev_perts,
    }
    common.write_json(os.path.join(args.out, "data", "evidence.json"), out_ev)
    common.write_json(os.path.join(args.out, "network", "network.json"), net)

    total = sum(stats.values())
    print(f"\nwrote {args.out}", file=sys.stderr)
    print(f"  papers fetched      {len(papers)} ({n_ft} with open-access full text)",
          file=sys.stderr)
    print(f"  claims grounded     {stats['both'] + stats['one']} of {total} "
          f"({stats['both']} name both genes, {stats['one']} name one)", file=sys.stderr)
    print(f"  left ungrounded     {stats['ungrounded']} (no sentence names the gene)",
          file=sys.stderr)


if __name__ == "__main__":
    main()

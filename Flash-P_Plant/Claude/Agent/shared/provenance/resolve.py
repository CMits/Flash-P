"""
The bounded repair loop — turn a claim plus a suspect DOI into verified provenance.

A FLASH-P edge arrives as "D27 activates MAX3, see 10.1073/pnas.1601729113" and
nothing more. Three things can be true of that DOI, and they need telling apart:

  * it resolves and the paper says so          -> **verified**
  * it resolves but the paper is about something else, or does not resolve at all
    -> search for the paper that *does* say so -> **repaired**
  * nothing found within the budget            -> **quarantine**, with the trail

The middle case is the whole point. A wrong DOI in FLASH-P is almost never malformed —
all 124 DOIs across the built networks are shape-valid — it is a real paper that simply
does not support the claim attached to it. Only reading the paper catches that, which
is why grounding, not shape, is the test.

**Bounded, never endless.** Rounds, candidates per round and HTTP calls per claim are
all capped. When the budget runs out the record is quarantined with every attempt
recorded, so the failure is visible and explicable rather than silently dropped or
retried forever.

Everything here is free: HTTP plus string matching, no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    from . import litapi, match, sentence
    from .litapi import PaperRecord, bare_doi
    from .sentence import Claim, Support
    from .store import Store
except ImportError:                                   # run directly as a script
    import litapi, match, sentence
    from litapi import PaperRecord, bare_doi
    from sentence import Claim, Support
    from store import Store

__all__ = ["Config", "Resolution", "Attempt", "resolve_claim", "fetch_paper"]

# Status values match Flash-P_DataBase's `verification_status` vocabulary so a
# verified network can be upserted into atlas.db without translation.
VERIFIED = "verified"
REPAIRED = "repaired"
QUARANTINE = "quarantine"


@dataclass
class Config:
    max_rounds: int = 3
    candidates_per_round: int = 8
    # Full text is a second call per paper; only worth it for the DOI on record and
    # the first couple of replacements, not for every search hit we glance at.
    fulltext: bool = True
    fulltext_candidates: int = 2
    offline: bool = False           # cache only — no network at all


@dataclass
class Attempt:
    """One thing we tried, and what came of it. Shown in the Studio drawer."""
    source: str        # openalex | europepmc | cache
    action: str        # by_doi | search | fulltext
    outcome: str       # hit | miss | ungrounded | rejected
    note: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"source": self.source, "action": self.action,
                "outcome": self.outcome, "note": self.note}


@dataclass
class Resolution:
    doi: str = ""
    status: str = QUARANTINE
    reason: str = ""
    support: Optional[Support] = None
    paper: Optional[PaperRecord] = None
    previous_doi: str = ""
    tried: List[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (VERIFIED, REPAIRED)


# ---------------------------------------------------------------------------
# paper retrieval
# ---------------------------------------------------------------------------
def fetch_paper(doi: str, store: Store, cfg: Config,
                tried: Optional[List[Attempt]] = None) -> Optional[PaperRecord]:
    """Metadata + abstract (+ OA full text) for a DOI, cache first.

    Both catalogues are consulted and merged: OpenAlex has better journal and OA
    fields, Europe PMC has abstracts for a lot of biology OpenAlex leaves blank and is
    the only one that yields a usable PMCID. Either alone loses records the other has.
    """
    tried = tried if tried is not None else []
    d = bare_doi(doi)
    if not d:
        return None

    cached = store.get(d)
    if cached is not None and cached.get("abstract"):
        tried.append(Attempt("cache", "by_doi", "hit", d))
        return cached
    if cfg.offline:
        tried.append(Attempt("cache", "by_doi", "miss", "offline mode, not in cache"))
        return cached
    if store.is_miss(d):
        tried.append(Attempt("cache", "by_doi", "miss", "known unresolvable (cached)"))
        return None

    oa = litapi.openalex_by_doi(d)
    tried.append(Attempt("openalex", "by_doi", "hit" if oa else "miss", d))
    ep = litapi.epmc_by_doi(d)
    tried.append(Attempt("europepmc", "by_doi", "hit" if ep else "miss", d))

    rec = litapi.merge_records(oa, ep)
    if rec is None:
        store.mark_miss(d, "not found in OpenAlex or Europe PMC")
        return None

    rec["doi"] = d
    _attach_fulltext(rec, store, cfg, tried)
    store.put(rec)
    return rec


def _attach_fulltext(rec: PaperRecord, store: Store, cfg: Config,
                     tried: List[Attempt]) -> None:
    """Fetch open-access full text, when the source itself says it is open access.

    ``pmcid`` is only ever populated by ``litapi`` for records Europe PMC flagged
    ``isOpenAccess=Y`` and ``inEPMC=Y``. A paywalled paper therefore has no pmcid, no
    full text is attempted, and the record honestly reports abstract-only.
    """
    if not cfg.fulltext or cfg.offline or rec.get("fulltext"):
        return
    pmcid = rec.get("pmcid") or ""
    if not pmcid:
        return
    cached = store.get_fulltext(rec["doi"])
    if cached:
        rec["fulltext"] = cached
        return
    text = litapi.epmc_fulltext(pmcid)
    tried.append(Attempt("europepmc", "fulltext", "hit" if text else "miss", pmcid))
    if text:
        rec["fulltext"] = text
        store.put_fulltext(rec["doi"], text)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def _search_rounds(claim: Claim, cfg: Config) -> List[Tuple[str, Callable[[], List[PaperRecord]]]]:
    """Query plans, cheapest and most specific first.

    Europe PMC leads because it indexes the biology literature more completely and
    returns abstracts inline; OpenAlex is the second opinion. The last round drops the
    species term, which is what rescues a conserved relationship reported in a
    different organism.
    """
    q = claim.query()
    n = cfg.candidates_per_round
    broad = " ".join(x for x in (sentence._readable(claim.entity_a),
                                 sentence._readable(claim.entity_b)) if x)
    return [
        ("europepmc", lambda: litapi.epmc_search(q, n)),
        ("openalex", lambda: litapi.openalex_search(q, n)),
        ("europepmc", lambda: litapi.epmc_search(broad, n)),
    ][:max(1, cfg.max_rounds)]


def resolve_claim(claim: Claim, doi: str, store: Store,
                  cfg: Optional[Config] = None,
                  expected_title: str = "") -> Resolution:
    """Verify ``doi`` against ``claim``; if it fails, find a DOI that holds up."""
    cfg = cfg or Config()
    res = Resolution(doi=bare_doi(doi), previous_doi="")
    original = res.doi

    # -- 1. the DOI on record ------------------------------------------------
    if original:
        paper = fetch_paper(original, store, cfg, res.tried)
        if paper is not None:
            if expected_title:
                v = match.title_match(expected_title, paper.get("title", ""))
                if not v.accept:
                    res.tried.append(Attempt(paper.get("source", "?"), "by_doi",
                                             "rejected", v.reason))
                    paper = None
        if paper is not None:
            support = sentence.best_support(claim, paper.get("abstract", ""),
                                            paper.get("fulltext", ""))
            if support is not None:
                res.status = VERIFIED
                res.support = support
                res.paper = paper
                if not support.direction_ok:
                    res.reason = ("supporting sentence uses language opposite to the edge "
                                  "sign — check for mutant/loss-of-function phrasing")
                return res
            res.tried.append(Attempt(paper.get("source", "?"), "by_doi", "ungrounded",
                                     f"paper does not co-mention "
                                     f"{claim.entity_a} and {claim.entity_b}"))
        else:
            res.tried.append(Attempt("-", "by_doi", "miss", f"{original} did not resolve"))
    else:
        res.tried.append(Attempt("-", "by_doi", "miss", "no DOI on record"))

    if cfg.offline:
        res.status = QUARANTINE
        res.reason = "offline mode: could not verify without network access"
        return res

    # -- 2. repair -----------------------------------------------------------
    seen = {original} if original else set()
    ft_budget = cfg.fulltext_candidates

    for source, run in _search_rounds(claim, cfg):
        try:
            candidates = run()
        except Exception as e:                      # a search failing is not fatal
            res.tried.append(Attempt(source, "search", "miss", f"error: {e}"))
            continue
        res.tried.append(Attempt(source, "search", "hit" if candidates else "miss",
                                 f"{len(candidates)} candidates for '{claim.query()}'"))

        for cand in candidates:
            d = bare_doi(cand.get("doi", ""))
            if not d or d in seen:
                continue
            seen.add(d)

            if expected_title:
                v = match.title_match(expected_title, cand.get("title", ""),
                                      candidate_authors=cand.get("authors", ""))
                if not v.accept:
                    continue

            support = sentence.best_support(claim, cand.get("abstract", ""), "")
            if support is None and cfg.fulltext and ft_budget > 0 and cand.get("pmcid"):
                ft_budget -= 1
                _attach_fulltext(cand, store, cfg, res.tried)
                if cand.get("fulltext"):
                    support = sentence.best_support(claim, cand.get("abstract", ""),
                                                    cand.get("fulltext", ""))
            if support is None:
                continue

            store.put(cand)
            res.status = REPAIRED
            res.doi = d
            res.previous_doi = original
            res.support = support
            res.paper = cand
            res.reason = (f"original DOI did not support this claim; replaced from {source}"
                          if original else f"no DOI on record; found via {source}")
            res.tried.append(Attempt(source, "search", "hit", f"grounded in {d}"))
            return res

    # -- 3. exhausted --------------------------------------------------------
    res.status = QUARANTINE
    res.doi = original
    n = len(seen)
    res.reason = (f"no paper found within budget that co-mentions "
                  f"{claim.entity_a} and {claim.entity_b} "
                  f"({n} candidate{'' if n == 1 else 's'} examined)")
    return res


if __name__ == "__main__":
    import tempfile, os
    from sentence import aliases_for

    db = os.path.join(tempfile.mkdtemp(), "papers.db")
    cfg = Config()
    ok_all = True

    with Store(db) as st:
        # 1. A claim whose DOI is correct: BRC1 represses shoot branching (Aguilar-
        #    Martinez 2007, The Plant Cell) — should verify straight from the abstract.
        good = Claim(entity_a="BRC1", entity_b="Shoot_Branching", sign=-1,
                     species="Arabidopsis thaliana",
                     aliases_a=aliases_for("BRC1", "BRANCHED1 (BRC1)"),
                     aliases_b=aliases_for("Shoot_Branching", "Shoot branching"))
        r = resolve_claim(good, "10.1105/tpc.106.048934", st, cfg)
        print(f"[1] correct DOI            -> {r.status}")
        if r.support:
            print(f"    locator {r.support.locator}, conf {r.support.confidence}")
            print(f"    quote: {r.support.quote[:100]}...")
        ok_all &= (r.status == VERIFIED)

        # 2. Same claim, but pointing at a real paper about something else entirely.
        #    Must NOT verify — it resolves fine, it just doesn't support the claim.
        r2 = resolve_claim(good, "10.1038/nature12373", st, cfg)
        print(f"\n[2] resolvable wrong DOI   -> {r2.status}  ({r2.reason[:70]})")
        print(f"    was {r2.previous_doi or '-'} now {r2.doi}")
        ok_all &= (r2.status in (REPAIRED, QUARANTINE) and r2.doi != "10.1038/nature12373"
                   or r2.status == QUARANTINE)

        # 3. A DOI that does not exist at all.
        r3 = resolve_claim(good, "10.9999/does.not.exist.12345", st, cfg)
        print(f"\n[3] nonexistent DOI        -> {r3.status}, now {r3.doi or '-'}")
        ok_all &= (r3.status in (REPAIRED, QUARANTINE))

        # 4. Cache must make a repeat run free.
        before = litapi.STATS.calls
        r4 = resolve_claim(good, "10.1105/tpc.106.048934", st, cfg)
        print(f"\n[4] repeat run             -> {r4.status}, "
              f"{litapi.STATS.calls - before} new HTTP calls (want 0)")
        ok_all &= (r4.status == VERIFIED and litapi.STATS.calls == before)

        print(f"\ncache: {st.counts()}")
        print(f"http : {litapi.STATS}")
    print("resolve self-test:", "OK" if ok_all else "FAILED")

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

import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Sections in which a paper restates other people's findings — see _primary_rank.
_SECONDHAND = re.compile(
    r"^(introduction|background|overview|literature review|general discussion|"
    r"concluding remarks|perspectives?|future)", re.I)

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


def _primary_rank(support: "Support", year: int, confidence: float):
    """Sort key for choosing between papers that all support the claim (lower = better).

    Prefer, in order:

    1. **Evidence over restatement.** A sentence in an abstract or a Results section is
       the paper's own finding. One in an Introduction is that paper citing somebody
       else — grounding a claim there is grounding it in a citation.
    2. **The primary report.** Search engines rank by recency and relevance, so without
       this every claim lands on the newest review that mentions it: an early run put
       all 22 repairs on 2024+ papers and let a single 2026 paper absorb nine claims.
       The oldest paper that actually states the finding is usually the one that found it.
    3. Confidence, as a tie-break.

    This mirrors ``Flash-P_DataBase``'s own ``pick_doi``, which prefers full text, then
    judge trust, then "the earliest paper (the primary report)".
    """
    loc = support.locator
    if loc == "abstract":
        tier = 0
    elif loc.startswith("full_text:") and not _SECONDHAND.match(loc.split(":", 1)[1].strip()):
        tier = 1
    else:
        tier = 2                       # Introduction/Background, or an unlabelled section
    return (tier, year, -confidence)


@dataclass
class Config:
    # Four plans are available (see _search_rounds); most claims stop after one or two
    # because min_candidates_before_pick is reached.
    max_rounds: int = 4
    candidates_per_round: int = 8
    # Collect at least this many supporting papers before choosing, so the choice is
    # between real alternatives rather than "whichever the search returned first".
    # Two is the cost/quality knee: going to three roughly quadrupled wall-clock on a
    # 40-claim network for a small gain, because it forces every repair through all
    # three search rounds.
    min_candidates_before_pick: int = 2
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
# paper retrieval — a ladder, climbed only as far as the claim needs
# ---------------------------------------------------------------------------
# Most claims are grounded on the first rung: in the salinity network 49 of 122 were
# settled by the abstract alone. Fetching the second catalogue and then a whole-paper
# full text *before* asking whether the abstract already worked is what made this step
# slow — a measured run spent 256 s of its 667 s of network time on full-text XML, much
# of it for claims that never needed it. So each rung is climbed only after the one
# below it failed to ground the claim.
def _open_record(d: str, store: Store, cfg: Config,
                 tried: List[Attempt]) -> Optional[PaperRecord]:
    """Rung 1 — the cache, else OpenAlex. Abstract only; no full text is fetched."""
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

    with store.gate("oa:" + d):
        cached = store.get(d)             # another worker may have fetched it meanwhile
        if cached is not None and cached.get("abstract"):
            tried.append(Attempt("cache", "by_doi", "hit", d))
            return cached
        oa = litapi.openalex_by_doi(d)
        tried.append(Attempt("openalex", "by_doi", "hit" if oa else "miss", d))
        if oa is not None:
            oa["doi"] = d
            store.put(oa)
        return oa


def _add_europepmc(rec: Optional[PaperRecord], d: str, store: Store, cfg: Config,
                   tried: List[Attempt]) -> Optional[PaperRecord]:
    """Rung 2 — Europe PMC's second opinion, merged into what we have.

    Worth its call for two things OpenAlex often lacks: an abstract for a lot of the
    biology literature, and the PMCID that rung 3 needs. Returns ``rec`` unchanged when
    Europe PMC knows nothing, or None when neither catalogue has the DOI at all.
    """
    if cfg.offline:
        return rec
    ep = litapi.epmc_by_doi(d)
    tried.append(Attempt("europepmc", "by_doi", "hit" if ep else "miss", d))
    merged = litapi.merge_records(rec, ep)
    if merged is None:
        store.mark_miss(d, "not found in OpenAlex or Europe PMC")
        return None
    merged["doi"] = d
    store.put(merged)
    return merged


def fetch_paper(doi: str, store: Store, cfg: Config,
                tried: Optional[List[Attempt]] = None) -> Optional[PaperRecord]:
    """Everything known about a DOI: both catalogues merged, plus OA full text.

    The exhaustive form, for callers that want the whole record regardless of any one
    claim. ``resolve_claim`` deliberately does **not** use it — it climbs the same rungs
    one at a time and stops as soon as the claim is grounded.
    """
    tried = tried if tried is not None else []
    d = bare_doi(doi)
    if not d:
        return None

    rec = _open_record(d, store, cfg, tried)
    if rec is None or not rec.get("abstract") or not rec.get("pmcid"):
        rec = _add_europepmc(rec, d, store, cfg, tried)
    if rec is None:
        return None

    _attach_fulltext(rec, store, cfg, tried)
    store.put(rec)
    return rec


def _ground_on_record(claim: Claim, d: str, store: Store, cfg: Config,
                      tried: List[Attempt], expected_title: str = "",
                      ) -> Tuple[Optional[PaperRecord], Optional[Support]]:
    """Climb the ladder against the DOI already on the edge, stopping the moment a
    sentence in that paper grounds the claim.

    Returns ``(paper, support)``. ``paper`` is None when the DOI resolves nowhere or the
    title check rejects it; ``support`` is None when the paper resolved but nothing in
    it names both of the claim's entities.
    """
    def rejected(r: Optional[PaperRecord]) -> bool:
        if not expected_title or r is None:
            return False
        v = match.title_match(expected_title, r.get("title", ""))
        if v.accept:
            return False
        tried.append(Attempt(r.get("source", "?"), "by_doi", "rejected", v.reason))
        return True

    def grounded(r: PaperRecord) -> Optional[Support]:
        return sentence.best_support(claim, r.get("abstract", ""), r.get("fulltext", ""))

    rec = _open_record(d, store, cfg, tried)
    if rec is not None:
        if rejected(rec):
            return None, None
        support = grounded(rec)
        if support is not None:
            return rec, support

    # rung 2 — Europe PMC's abstract, only now that OpenAlex's did not carry the claim
    merged = _add_europepmc(rec, d, store, cfg, tried)
    if merged is None:
        return None, None
    if merged is not rec:
        rec = merged
        if rejected(rec):
            return None, None
        support = grounded(rec)
        if support is not None:
            return rec, support
    if rec is None:
        return None, None

    # rung 3 — the whole paper, the expensive one, for open access only
    _attach_fulltext(rec, store, cfg, tried)
    if rec.get("fulltext"):
        store.put(rec)
        support = grounded(rec)
        if support is not None:
            return rec, support
    return rec, None


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
    # The most expensive call in the module — a whole paper — so make sure only one
    # worker ever makes it for a given paper, and re-check the cache once inside.
    with store.gate("ft:" + pmcid):
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
def _search_rounds(claim: Claim,
                   cfg: Config) -> List[Tuple[str, str, Callable[[], List[PaperRecord]]]]:
    """Query plans, cheapest and most specific first.

    Europe PMC leads because it indexes the biology literature more completely and
    returns abstracts inline. PubMed is second: one ``efetch`` brings back every hit
    *with* its abstract, so a round costs two calls no matter how many candidates it
    returns. The third round drops the species term, which is what rescues a conserved
    relationship reported in a different organism.

    OpenAlex is deliberately **last**. Its ``/works?`` search is metered — a free
    account gets a small daily budget, then every call answers 429 until midnight UTC —
    so it is spent only on the claims the free sources could not settle, and once the
    budget is gone ``litapi`` skips it outright instead of retrying.
    """
    q = claim.query()
    n = cfg.candidates_per_round
    broad = " ".join(x for x in (sentence._readable(claim.entity_a),
                                 sentence._readable(claim.entity_b)) if x)
    return [
        ("europepmc", q, lambda: litapi.epmc_search(q, n)),
        ("pubmed", q, lambda: litapi.pubmed_search(q, n)),
        ("europepmc", broad, lambda: litapi.epmc_search(broad, n)),
        # Sorted by citations, not relevance. Every engine ranks recent work first, so a
        # relevance search never surfaces the paper that established a relationship —
        # it surfaces this year's paper restating it. The seminal report is, almost by
        # definition, the most-cited one.
        ("openalex", q, lambda: litapi.openalex_search(q, n, sort="cited_by_count:desc")),
    ][:max(1, cfg.max_rounds)]


# One network asks the same question more than once: an edge A->B and a perturbation
# test of A->B produce the same query, and the same query in the same round returns the
# same papers. Memoised for the process, so the second claim pays nothing.
_search_memo: Dict[str, List[PaperRecord]] = {}
_search_memo_lock = threading.Lock()


def _search_cached(source: str, query: str,
                   run: Callable[[], List[PaperRecord]]) -> List[PaperRecord]:
    key = f"{source}|{query}"
    with _search_memo_lock:
        hit = _search_memo.get(key)
    if hit is not None:
        return hit
    out = run()
    with _search_memo_lock:
        _search_memo[key] = out
    return out


def resolve_claim(claim: Claim, doi: str, store: Store,
                  cfg: Optional[Config] = None,
                  expected_title: str = "") -> Resolution:
    """Verify ``doi`` against ``claim``; if it fails, find a DOI that holds up."""
    cfg = cfg or Config()
    res = Resolution(doi=bare_doi(doi), previous_doi="")
    original = res.doi

    # -- 1. the DOI on record ------------------------------------------------
    if original:
        paper, support = _ground_on_record(claim, original, store, cfg,
                                           res.tried, expected_title)
        if support is not None:
            res.status = VERIFIED
            res.support = support
            res.paper = paper
            if not support.direction_ok:
                res.reason = ("supporting sentence uses language opposite to the edge "
                              "sign — check for mutant/loss-of-function phrasing")
            return res
        if paper is not None:
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
    found: List[Tuple[float, int, str, PaperRecord, Support, str]] = []

    for source, query, run in _search_rounds(claim, cfg):
        try:
            candidates = _search_cached(source, query, run)
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
            found.append((support.confidence, cand.get("year") or 9999, d, cand, support, source))

        # Enough to choose well; searching further rounds only adds more of the same.
        if len(found) >= cfg.min_candidates_before_pick:
            break

    if found:
        best = min(found, key=lambda c: _primary_rank(c[4], c[1], c[0]))
        _, year, d, cand, support, source = best
        res.status = REPAIRED
        res.doi = d
        res.previous_doi = original
        res.support = support
        res.paper = cand
        res.reason = (f"original DOI did not support this claim; replaced from {source}"
                      if original else f"no DOI on record; found via {source}")
        if len(found) > 1:
            res.tried.append(Attempt(source, "search", "hit",
                                     f"{len(found)} papers supported this claim; chose {d} "
                                     f"({year}) as the most primary"))
        else:
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

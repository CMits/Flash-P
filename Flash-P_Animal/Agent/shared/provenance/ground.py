"""
Quote grounding — prove a supporting sentence really occurs in the paper.

A claim is only allowed into a FLASH-P network if the sentence backing it can be
found, verbatim, in text we actually retrieved (the abstract, or open-access full
text). If it cannot be located exactly, we do not have it — the record is quarantined
rather than published with a quote nobody can check.

Two decisions worth knowing about, both departures from
``Flash-P_DataBase/extract/verify.py``:

1. **No lossy fallback.** That gate accepts a quote when 90% of its tokens appear
   anywhere in the source, order-agnostic. Such a quote is not a substring, so the
   website's ``<mark>`` highlighter silently fails to find it and the reader sees an
   unhighlighted abstract with no explanation. Here, a match is a match or it isn't.

2. **Normalisation happens before the ASCII fold, not after.** The original does
   ``NFKD -> encode('ascii','ignore')`` and only *then* maps curly quotes and dashes —
   by which point those characters have already been deleted, so the mapping is a
   no-op and ``don't`` and ``don’t`` normalise differently. Doing it in the right order
   makes typographic variants genuinely equivalent, which is most of what separates a
   real quote from an apparent mismatch.

The payoff is ``ground()`` returning the **exact source substring**. Callers store
that, not the model's rendering of it, so the Studio's whitespace-tolerant regex is
guaranteed to find it. Grounding and highlighting can no longer disagree.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Dict, List, NamedTuple, Optional, Tuple

__all__ = ["norm", "ground", "Grounded", "MIN_QUOTE_CHARS", "section_at", "split_sentences"]

# Below this, a "quote" is too generic to prove anything — "increased branching"
# occurs in half the corpus. Matches the atlas gate's threshold.
MIN_QUOTE_CHARS = 15

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
}
# ‐ ‑ ‒ – — ― − and the non-breaking hyphen
_DASHES = "‐‑‒–—―−⁃"


class Grounded(NamedTuple):
    """A located quote. ``text`` is the exact source substring — store *this*."""
    text: str
    start: int
    end: int
    locator: str


def _norm_with_map(s: str) -> Tuple[str, List[int]]:
    """Normalise, and record which raw index produced each normalised character.

    The index map is what lets a match found in normalised space be reported as an
    offset into the original text, so the caller can slice the real characters back
    out rather than returning a mangled ASCII approximation to the reader.
    """
    out: List[str] = []
    idx: List[int] = []
    prev_space = True          # leading whitespace is dropped
    prev_dash = False

    for i, ch in enumerate(s):
        # Fast path: plain ASCII is the overwhelming majority of scientific text and
        # needs no Unicode work at all. Full texts run to 50 kB, and this loop is the
        # hot spot of the whole verifier.
        o = ord(ch)
        if 0x20 < o < 0x7F:
            mapped = ch
        elif ch in _QUOTES:
            mapped = _QUOTES[ch]
        elif ch in _DASHES:
            mapped = "-"
        elif ch.isspace():
            mapped = " "
        else:
            mapped = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
        if not mapped:
            continue
        mapped = mapped.lower()

        for c in mapped:
            if c == " ":
                if prev_space:
                    continue
                prev_space, prev_dash = True, False
            elif c == "-":
                if prev_dash:
                    continue
                prev_space, prev_dash = False, True
            else:
                prev_space, prev_dash = False, False
            out.append(c)
            idx.append(i)

    # drop a trailing space so `norm(x) == norm(x + ' ')`
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


@lru_cache(maxsize=4096)
def norm(s: str) -> str:
    """Normalised form only — for comparisons that don't need offsets.

    Cached because the same strings are normalised over and over: every alias of every
    node, against every sentence of every candidate paper.
    """
    return _norm_with_map(s or "")[0]


# The index map is a parallel list of ints — as long as the text itself — so only a
# few of the largest sources are worth keeping. Full texts are the ones that matter;
# abstracts are cheap either way.
@lru_cache(maxsize=8)
def _norm_map_cached(s: str) -> Tuple[str, Tuple[int, ...]]:
    n, idx = _norm_with_map(s)
    return n, tuple(idx)


_SECTION_RE = re.compile(r"^## (.+)$", re.M)


def section_at(text: str, pos: int) -> str:
    """Name of the ``## Section`` a character offset falls under, or ''.

    Full text is stored with ``## Heading`` lines (the convention shared with
    Flash-P_DataBase and the website), so the nearest preceding heading is the
    section a quote came from.
    """
    last = ""
    for m in _SECTION_RE.finditer(text):
        if m.start() > pos:
            break
        last = m.group(1).strip()
    return last


def ground(quote: str, abstract: str, fulltext: str = "") -> Optional[Grounded]:
    """Locate ``quote`` in the abstract, then the full text. None if it isn't there.

    The abstract is searched first: a claim supported by the abstract is the stronger
    citation, and it is also the part every reader can see regardless of paywall.
    """
    q = (quote or "").strip()
    if len(norm(q)) < MIN_QUOTE_CHARS:
        return None

    nq = norm(q)
    for source, label in ((abstract or "", "abstract"), (fulltext or "", "full_text")):
        if not source:
            continue
        # Cheap cached test first: most calls are misses, and a miss needs only the
        # normalised string, never the offset map.
        if nq not in norm(source):
            continue
        ntext, imap = _norm_map_cached(source)
        pos = ntext.find(nq)
        if pos < 0:
            continue
        start = imap[pos]
        end = imap[pos + len(nq) - 1] + 1
        exact = source[start:end]
        locator = label
        if label == "full_text":
            sec = section_at(source, start)
            if sec:
                locator = f"full_text:{sec}"
        return Grounded(text=exact, start=start, end=end, locator=locator)
    return None


_ABBREV = r"(?<!\b[A-Z])(?<!\bet al)(?<!\bFig)(?<!\bcf)(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bapprox)"
_SENT_SPLIT = re.compile(_ABBREV + r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9(])")


def split_sentences(text: str) -> List[str]:
    """Sentence split good enough for scientific prose.

    Deliberately conservative around the abbreviations that actually appear in
    abstracts (``et al.``, ``e.g.``, ``Fig. 3``, single-initial names), because an
    over-eager split truncates the very sentence we are trying to quote.
    """
    if not text:
        return []
    out: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("## "):
            continue
        out.extend(p.strip() for p in _SENT_SPLIT.split(line) if p.strip())
    return out


if __name__ == "__main__":
    abstract = ("Shoot branching patterns depend on a key developmental decision. "
                "We show that BRC1 expression is up–regulated in axillary buds, and "
                "that brc1 mutants don’t suppress bud outgrowth. Fig. 2 shows the "
                "effect versus wild type.")
    ft = "## Results\nStrigolactone treatment reduced bud outgrowth by 60%.\n## Discussion\nThis supports the model."

    cases = [
        ("exact", "BRC1 expression is up-regulated in axillary buds", True),
        ("curly quote vs straight", "brc1 mutants don't suppress bud outgrowth", True),
        ("whitespace differs", "Shoot   branching\npatterns depend on a key developmental decision", True),
        ("case differs", "shoot branching patterns depend", True),
        ("too short", "BRC1", False),
        ("not present", "BRC1 has no effect whatsoever on branching", False),
        ("paraphrase (must fail)", "mutants of brc1 fail to suppress the outgrowth of buds", False),
    ]
    print("grounding against abstract:")
    ok_all = True
    for name, q, want in cases:
        g = ground(q, abstract, ft)
        got = g is not None
        ok_all &= (got == want)
        mark = "ok " if got == want else "BAD"
        detail = f'-> "{g.text[:52]}" [{g.locator}]' if g else "-> no match"
        print(f"  {mark} {name:26s} {detail}")

    g = ground("Strigolactone treatment reduced bud outgrowth by 60%", abstract, ft)
    print(f"\nfull-text locator: {g.locator if g else 'MISS'}")
    ok_all &= (g is not None and g.locator == "full_text:Results")

    # The exact-substring guarantee: what we return must be findable by the Studio's
    # whitespace-tolerant, case-insensitive regex — i.e. a literal substring.
    g = ground("brc1 mutants don't suppress bud outgrowth", abstract)
    print(f"returned exact source slice: {g.text!r}")
    ok_all &= (g is not None and g.text in abstract)

    print(f"\nsentences: {len(split_sentences(abstract))} "
          f"(expect 4, 'Fig. 2' must not split)")
    ok_all &= (len(split_sentences(abstract)) == 4)
    print("ground self-test:", "OK" if ok_all else "FAILED")

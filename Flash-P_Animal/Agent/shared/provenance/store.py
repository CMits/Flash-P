"""
On-disk paper cache — makes verification cheap to repeat.

A verification run is mostly the same few hundred DOIs every time: re-running the
pipeline, re-checking a network after an edit, or verifying five networks that share
a literature base all hit the same papers. Fetching them once and keeping them turns
a two-minute network round trip into an instant one, and makes the retrofit over
already-built networks essentially free after the first pass.

The schema deliberately mirrors ``Flash-P_DataBase/atlas.db``'s ``paper`` table so a
verified network can be contributed back to the atlas as a straight upsert rather than
a translation. Two additions it does not have: ``fulltext`` (kept in its own table
because it is 30-60 KB per row and rarely needed), and ``miss`` — a negative cache, so
a DOI that genuinely does not resolve is not re-queried on every run. Misses expire,
because "not in OpenAlex today" is sometimes "indexed next month".

The cache lives under ``.flashp_cache/`` and is disposable: delete it and the next run
rebuilds it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

try:
    from .litapi import PaperRecord, bare_doi
except ImportError:                                   # run directly as a script
    from litapi import PaperRecord, bare_doi

__all__ = ["Store", "DEFAULT_CACHE_DIR"]

DEFAULT_CACHE_DIR = ".flashp_cache"
MISS_TTL_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper (
    doi        TEXT PRIMARY KEY,
    title      TEXT    DEFAULT '',
    authors    TEXT    DEFAULT '',
    year       INTEGER,
    journal    TEXT    DEFAULT '',
    licence    TEXT    DEFAULT '',
    oa_status  TEXT    DEFAULT '',
    abstract   TEXT    DEFAULT '',
    pmcid      TEXT    DEFAULT '',
    source     TEXT    DEFAULT '',
    fetched_at REAL    DEFAULT 0
);
-- Full text is large and only some papers have it; a side table keeps `paper`
-- cheap to scan and lets a text be dropped without touching the metadata.
CREATE TABLE IF NOT EXISTS fulltext (
    doi        TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    chars      INTEGER DEFAULT 0,
    fetched_at REAL    DEFAULT 0
);
-- Negative cache: DOIs that resolved nowhere. Without this, every run pays full
-- price for the same dead references.
CREATE TABLE IF NOT EXISTS miss (
    doi        TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    checked_at REAL DEFAULT 0
);
"""


class Store:
    """SQLite-backed paper cache.

    Safe to share across threads: SQLite itself only allows one writer/reader
    to touch a connection at a time, so every access here goes through
    ``self._lock``. A verification run's actual cost is network latency, not
    these local reads/writes, so serializing them costs nothing measurable
    while letting concurrent workers overlap on the part that's actually slow.
    """

    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._gates: dict = {}
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        self.con.commit()
        self.hits = 0
        self.misses = 0

    # -- in-flight de-duplication -------------------------------------------
    def gate(self, key: str) -> threading.Lock:
        """A lock named after the thing about to be fetched.

        The cache only helps once a fetch has *returned*, so without this two workers
        that need the same paper both go to the network — and with claims sharing papers
        heavily, that is common. Hold this around a fetch and re-check the cache inside,
        and the second worker waits briefly and then finds the answer already there.
        """
        with self._lock:
            g = self._gates.get(key)
            if g is None:
                g = threading.Lock()
                self._gates[key] = g
            return g

    # -- papers ------------------------------------------------------------
    def get(self, doi: str) -> Optional[PaperRecord]:
        """Cached paper, with its full text attached if we have one."""
        d = bare_doi(doi)
        if not d:
            return None
        with self._lock:
            row = self.con.execute("SELECT * FROM paper WHERE doi = ?", (d,)).fetchone()
            if row is None:
                return None
            self.hits += 1
            rec = PaperRecord.empty(d)
            for k in ("title", "authors", "year", "journal", "licence",
                      "oa_status", "abstract", "pmcid", "source"):
                rec[k] = row[k]
            rec["fulltext"] = self._get_fulltext_locked(d)
            return rec

    def put(self, rec: PaperRecord) -> None:
        d = bare_doi(rec.get("doi", ""))
        if not d:
            return
        with self._lock:
            self.con.execute(
                """INSERT INTO paper (doi, title, authors, year, journal, licence,
                                      oa_status, abstract, pmcid, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(doi) DO UPDATE SET
                     title=excluded.title, authors=excluded.authors, year=excluded.year,
                     journal=excluded.journal, licence=excluded.licence,
                     oa_status=excluded.oa_status, abstract=excluded.abstract,
                     pmcid=excluded.pmcid, source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (d, rec.get("title", "") or "", rec.get("authors", "") or "",
                 rec.get("year"), rec.get("journal", "") or "", rec.get("licence", "") or "",
                 rec.get("oa_status", "") or "", rec.get("abstract", "") or "",
                 rec.get("pmcid", "") or "", rec.get("source", "") or "", time.time()))
            if rec.get("fulltext"):
                self._put_fulltext_locked(d, rec["fulltext"])
            # A DOI that now resolves is no longer a miss.
            self.con.execute("DELETE FROM miss WHERE doi = ?", (d,))
            self.con.commit()

    # -- full text ---------------------------------------------------------
    def _get_fulltext_locked(self, doi: str) -> str:
        """Caller already holds ``self._lock``."""
        row = self.con.execute("SELECT text FROM fulltext WHERE doi = ?", (doi,)).fetchone()
        return row["text"] if row else ""

    def get_fulltext(self, doi: str) -> str:
        d = bare_doi(doi)
        if not d:
            return ""
        with self._lock:
            return self._get_fulltext_locked(d)

    def _put_fulltext_locked(self, doi: str, text: str) -> None:
        """Caller already holds ``self._lock``; does not commit."""
        self.con.execute(
            """INSERT INTO fulltext (doi, text, chars, fetched_at) VALUES (?,?,?,?)
               ON CONFLICT(doi) DO UPDATE SET
                 text=excluded.text, chars=excluded.chars, fetched_at=excluded.fetched_at""",
            (doi, text, len(text), time.time()))

    def put_fulltext(self, doi: str, text: str) -> None:
        d = bare_doi(doi)
        if not d or not text:
            return
        with self._lock:
            self._put_fulltext_locked(d, text)
            self.con.commit()

    def has_fulltext(self, doi: str) -> bool:
        d = bare_doi(doi)
        with self._lock:
            row = self.con.execute("SELECT 1 FROM fulltext WHERE doi = ?", (d,)).fetchone()
            return row is not None

    # -- negative cache ----------------------------------------------------
    def is_miss(self, doi: str) -> bool:
        """True if this DOI recently failed to resolve anywhere."""
        d = bare_doi(doi)
        if not d:
            return False
        with self._lock:
            row = self.con.execute("SELECT checked_at FROM miss WHERE doi = ?", (d,)).fetchone()
            if row is None:
                return False
            age_days = (time.time() - (row["checked_at"] or 0)) / 86400.0
            if age_days > MISS_TTL_DAYS:
                self.con.execute("DELETE FROM miss WHERE doi = ?", (d,))
                self.con.commit()
                return False
            self.misses += 1
            return True

    def mark_miss(self, doi: str, reason: str = "") -> None:
        d = (doi or "").strip().lower()
        if not d:
            return
        with self._lock:
            self.con.execute(
                """INSERT INTO miss (doi, reason, checked_at) VALUES (?,?,?)
                   ON CONFLICT(doi) DO UPDATE SET
                     reason=excluded.reason, checked_at=excluded.checked_at""",
                (d, reason, time.time()))
            self.con.commit()

    # -- housekeeping ------------------------------------------------------
    def counts(self) -> dict:
        with self._lock:
            q = lambda sql: self.con.execute(sql).fetchone()[0]
            return {"papers": q("SELECT COUNT(*) FROM paper"),
                    "with_abstract": q("SELECT COUNT(*) FROM paper WHERE abstract <> ''"),
                    "fulltexts": q("SELECT COUNT(*) FROM fulltext"),
                    "misses": q("SELECT COUNT(*) FROM miss")}

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def default_path(start: str = ".") -> str:
    """Cache location for a variant: ``<Claude dir>/.flashp_cache/papers.db``.

    Anchored to this module so every network verified by the same checkout shares one
    cache, rather than each network re-fetching the same papers.
    """
    here = os.path.dirname(os.path.abspath(__file__))          # Agent/shared/provenance
    claude_dir = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(claude_dir, DEFAULT_CACHE_DIR, "papers.db")


if __name__ == "__main__":
    import tempfile

    tmp = os.path.join(tempfile.mkdtemp(), "papers.db")
    ok_all = True
    with Store(tmp) as st:
        rec = PaperRecord.empty("10.1105/TPC.106.048934")     # note: upper case in
        rec.update({"title": "BRANCHED1 acts as an integrator", "year": 2007,
                    "journal": "The Plant Cell", "abstract": "Shoot branching patterns depend...",
                    "oa_status": "green", "pmcid": "PMC1867007", "source": "openalex"})
        st.put(rec)

        got = st.get("https://doi.org/10.1105/tpc.106.048934")  # URL form, mixed case
        ok_all &= (got is not None and got["title"].startswith("BRANCHED1"))
        print(f"put/get across DOI forms: {'ok' if got else 'BAD'} -> {got['doi'] if got else None}")

        st.put_fulltext("10.1105/tpc.106.048934", "## Results\nBRC1 is expressed in buds.")
        again = st.get("10.1105/tpc.106.048934")
        ok_all &= (again is not None and again.has_fulltext)
        print(f"fulltext attached on get: {'ok' if again.has_fulltext else 'BAD'} "
              f"({len(again['fulltext'])} chars)")

        st.mark_miss("10.9999/nonexistent", "not in OpenAlex or Europe PMC")
        ok_all &= st.is_miss("10.9999/nonexistent")
        ok_all &= not st.is_miss("10.1105/tpc.106.048934")
        print(f"negative cache: miss={st.is_miss('10.9999/nonexistent')} "
              f"hit-is-not-miss={not st.is_miss('10.1105/tpc.106.048934')}")

        print(f"counts: {st.counts()}")
        ok_all &= (st.counts()["papers"] == 1 and st.counts()["fulltexts"] == 1)

    print(f"default cache path: {default_path()}")
    print("store self-test:", "OK" if ok_all else "FAILED")

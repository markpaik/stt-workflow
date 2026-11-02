"""Full-text search across every meeting transcript.

Two paths, one result shape ({query, hits, total}, each hit {base, index,
start, who, snippet}, newest meeting first):

1. An SQLite FTS5 index at config.SEARCH_DB (mode 600). Each line is
   indexed twice. `segs` stems with porter, so "meeting" finds "meetings"
   and "cafe" finds "café". `segs_raw` keeps whole words (no stemming), and
   it answers the last word of the query as a prefix ("pano" finds
   "panorama", "attendan" finds "attendance"). Porter would stem a part-typed
   word, so a prefix on `segs` hides lines while the user types. A query
   matches a line when either table matches. A last word under 3 characters
   is not a prefix ("ai?" does not find "air"). The words of a query must
   sit together as a phrase in one transcript line, as the substring scan
   required.
2. The original in-memory substring scan, as the fallback. It runs when FTS
   finds nothing (token search cannot match mid-word, so "orama" only ever
   finds "panorama" this way), when the query has no word characters, and
   when SQLite fails for any reason (locked, corrupt, built without FTS5).

The index is derived data. Every query first compares each live meeting's
json (mtime and size) with what the index holds, re-reads the changed ones,
and drops rows for meetings that are gone. That one check is what keeps the
index right after a rename, a delete, an archive, a relabel or an edit made
by any process, so no write path has to remember to update it. touch() lets
the rename and delete paths update it at once as well, and rebuild() (also
`python -m stt.search --rebuild`) recreates it from nothing.

Archived meetings are not indexed: config.meeting_bases() is the live store,
which is what search has always covered. Titles and AI summaries go into a
second table for meeting-level lookups (meetings()). /api/search keeps its
line-level shape, so the page does not change.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import config, dates, output

SCHEMA = "2"
_TOKENIZE = "porter unicode61 remove_diacritics 2"
_RAW_TOKENIZE = "unicode61 remove_diacritics 2"
_PREFIX_MIN = 3   # a shorter last word matches whole, not as a prefix
# a line's FTS rowid is file_id * _ROW + its segment index, so one meeting's
# rows are a rowid RANGE: dropping a meeting is a range delete, which FTS5
# serves from its rowid b-tree instead of scanning every row
_ROW = 1_000_000
_lock = threading.RLock()       # one in-process writer; SQLite guards across processes
_warned = set()


def _warn(key, msg):
    if key not in _warned:
        _warned.add(key)
        print(f"   search: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- the file ----

def db_path() -> Path:
    return Path(config.SEARCH_DB)


def _companions(p: Path):
    return [p] + [p.with_name(p.name + s) for s in ("-journal", "-wal", "-shm")]


def _secure(p: Path):
    """Mode 600 on the index and any journal SQLite left beside it: they all
    hold transcript text."""
    for f in _companions(p):
        try:
            if f.exists() and f.stat().st_mode & 0o077:
                os.chmod(f, 0o600)
        except OSError:
            pass


def _open(create=True, timeout=5.0):
    """A connection to the index, or None when create is False and there is
    no index yet. The file is created 600 BEFORE SQLite opens it, so it never
    exists with wider bits, and SQLite gives its journals the same mode."""
    p = db_path()
    if not p.exists():
        if not create:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    _secure(p)
    conn = sqlite3.connect(str(p), timeout=timeout, isolation_level=None,
                           check_same_thread=False)
    try:
        _schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _schema(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            base TEXT NOT NULL UNIQUE,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS segs USING fts5(
            text, start UNINDEXED, who UNINDEXED, tokenize='{_TOKENIZE}');
        -- the same lines, unstemmed, for the last-word prefix. Contentless:
        -- the text is read from segs by rowid, so it is not stored twice
        CREATE VIRTUAL TABLE IF NOT EXISTS segs_raw USING fts5(
            text, content='', contentless_delete=1, tokenize='{_RAW_TOKENIZE}');
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            title, summary, tokenize='{_TOKENIZE}');
    """)
    # the index describes ONE store: a schema change or a moved meetings
    # folder (Settings can point it elsewhere) empties it, and the next sync
    # fills it from the folder it now names
    want = {"schema": SCHEMA, "meetings_dir": str(config.meetings_dir())}
    have = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if all(have.get(k) == v for k, v in want.items()):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM segs")
        conn.execute("DELETE FROM segs_raw")
        conn.execute("DELETE FROM docs")
        conn.execute("DELETE FROM files")
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         list(want.items()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- indexing ---

def _summary_text(d) -> str:
    steps = d.get("ai_next_steps") or []
    if isinstance(steps, str):
        steps = [steps]
    parts = [d.get("ai_title"), d.get("ai_summary")] + [str(s) for s in steps]
    return "\n".join(str(p) for p in parts if p)


def _read(base: str, jpath: Path):
    """(lines, title, summary) for one meeting json, or None if it cannot be
    read right now. Lines are (index, start, who, text), blank lines skipped,
    the label as the transcript shows it (output.line_label).

    A json that parses but has a shape this code cannot read (a number where
    text belongs, a bad speakers list) gives no lines, as the scan does, so
    one bad meeting never fails the sync for the whole store. It is still
    recorded, with its mtime and size, so it is not re-read on every query."""
    try:
        d = json.loads(jpath.read_text())
    except (OSError, ValueError):
        return None             # missing or half written: try again next sync
    try:
        if not isinstance(d, dict):
            raise TypeError("not a json object")
        roster = output.roster_labels(d.get("speakers", []))
        lines = []
        for i, s in enumerate(d.get("segments") or []):
            if not isinstance(s, dict):
                continue
            text = s.get("text") or ""
            if text.strip():
                lines.append((i, s.get("start", 0.0), output.line_label(s, roster), text))
        title = d.get("title") or dates.title_of(base)
        return lines, title, _summary_text(d)
    except Exception as e:
        _warn(f"bad:{base}", f"{base}: transcript not indexed ({type(e).__name__})")
        return [], dates.title_of(base), ""


def _drop(conn, fid: int):
    lo = fid * _ROW
    conn.execute("DELETE FROM segs WHERE rowid >= ? AND rowid < ?", (lo, lo + _ROW))
    conn.execute("DELETE FROM segs_raw WHERE rowid >= ? AND rowid < ?", (lo, lo + _ROW))
    conn.execute("DELETE FROM docs WHERE rowid = ?", (fid,))


def _index_one(conn, base: str, mtime: float, size: int) -> bool:
    """(Re)index one live meeting inside the caller's transaction. False when
    its json cannot be read now: it stays out of the files table, so the next
    sync tries again instead of trusting an empty entry."""
    got = _read(base, config.meeting_file(base, ".json"))
    row = conn.execute("SELECT id FROM files WHERE base = ?", (base,)).fetchone()
    if row:
        _drop(conn, row[0])
    if got is None:
        if row:
            conn.execute("DELETE FROM files WHERE id = ?", (row[0],))
        return False
    lines, title, summary = got
    if row:
        fid = row[0]
        conn.execute("UPDATE files SET mtime = ?, size = ? WHERE id = ?",
                     (mtime, size, fid))
    else:
        fid = conn.execute("INSERT INTO files(base, mtime, size) VALUES (?, ?, ?)",
                           (base, mtime, size)).lastrowid
    rows = [(fid * _ROW + i, text, start, who)
            for i, start, who, text in lines if i < _ROW]
    conn.executemany("INSERT INTO segs(rowid, text, start, who) VALUES (?, ?, ?, ?)",
                     rows)
    conn.executemany("INSERT INTO segs_raw(rowid, text) VALUES (?, ?)",
                     [(r[0], r[1]) for r in rows])
    conn.execute("INSERT INTO docs(rowid, title, summary) VALUES (?, ?, ?)",
                 (fid, title, summary))
    return True


def _forget(conn, base: str):
    row = conn.execute("SELECT id FROM files WHERE base = ?", (base,)).fetchone()
    if row:
        _drop(conn, row[0])
        conn.execute("DELETE FROM files WHERE id = ?", (row[0],))


def _on_disk() -> dict:
    """{base: (mtime, size)} for every live meeting whose json is there now."""
    out = {}
    for b in config.meeting_bases():
        try:
            st = config.meeting_file(b, ".json").stat()
        except OSError:
            continue        # renamed or deleted between the listing and the stat
        out[b] = (st.st_mtime, st.st_size)
    return out


def _sync(conn) -> int:
    """Bring the index level with the live store. Returns how many meetings
    were indexed or dropped. One transaction, so a reader never sees half."""
    disk = _on_disk()
    known = {b: (fid, m, s) for fid, b, m, s in
             conn.execute("SELECT id, base, mtime, size FROM files")}
    gone = [b for b in known if b not in disk]
    todo = [b for b, ms in disk.items()
            if b not in known or (known[b][1], known[b][2]) != ms]
    if not gone and not todo:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for b in gone:
            _forget(conn, b)
        for b in todo:
            _index_one(conn, b, *disk[b])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(gone) + len(todo)


def sync() -> int:
    """Bring the index level with the live store, creating it if needed."""
    with _lock:
        conn = _open()
        try:
            return _sync(conn)
        finally:
            conn.close()
            _secure(db_path())


def rebuild() -> dict:
    """Delete the index and build it again from the meeting jsons alone."""
    t0 = time.monotonic()
    with _lock:
        for f in _companions(db_path()):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        conn = _open()
        try:
            _sync(conn)
            meetings = conn.execute("SELECT count(*) FROM files").fetchone()[0]
            lines = conn.execute("SELECT count(*) FROM segs").fetchone()[0]
        finally:
            conn.close()
            _secure(db_path())
    return {"meetings": meetings, "lines": lines,
            "seconds": round(time.monotonic() - t0, 2)}


def touch(*bases) -> None:
    """Update the index for these meetings now: drop each one, and index it
    again if it is live. The rename, archive, restore and delete paths call
    this after they move or remove a folder. Best-effort and quick: it never
    creates the index, never raises, and waits at most a second for a lock,
    because the next query's sync repairs anything it misses."""
    try:
        with _lock:
            conn = _open(create=False, timeout=1.0)
            if conn is None:
                return
            try:
                live = set(config.meeting_bases())
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for b in dict.fromkeys(b for b in bases if b):
                        _forget(conn, b)
                        if b in live:
                            try:
                                st = config.meeting_file(b, ".json").stat()
                            except OSError:
                                continue
                            _index_one(conn, b, st.st_mtime, st.st_size)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn("touch", f"index not updated now, the next search will ({e})")


# ---------------------------------------------------------------- querying ---

def _match_expr(q: str):
    """FTS5 phrases for the query: (stemmed, prefix, toks). The stemmed phrase
    is the words in order, for `segs`. The prefix phrase is the same words
    with the last one a prefix, for the unstemmed `segs_raw`. A last word
    under _PREFIX_MIN characters is matched whole. (None, None, []) when the
    query has no word characters. Every token is \\w only, so nothing in it
    can break out of the quotes."""
    toks = re.findall(r"\w+", q)
    if not toks:
        return None, None, []
    phrase = '"' + " ".join(toks) + '"'
    star = "*" if len(toks[-1]) >= _PREFIX_MIN else ""
    return phrase, phrase + star, toks


def _snippet(text: str, q: str, toks) -> str:
    """The same window as the scan: 60 characters before the match, 90 after.
    A stemmed or prefix match has no exact substring, so look for the words."""
    low = text.lower()
    pos, n = low.find(q), len(q)
    if pos < 0:
        for t in toks:
            for probe in (t, t[:max(3, len(t) - 2)]):
                pos = low.find(probe)
                if pos >= 0:
                    n = len(probe)
                    break
            if pos >= 0:
                break
    if pos < 0:
        pos, n = 0, 0
    a = max(0, pos - 60)
    b = min(len(text), pos + n + 90)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


_HITS = """WITH hit(id) AS (
                SELECT rowid FROM segs WHERE segs MATCH ?
                UNION
                SELECT rowid FROM segs_raw WHERE segs_raw MATCH ?)"""


def _fts(q: str, limit: int):
    stem, prefix, toks = _match_expr(q)
    if stem is None:
        return None
    with _lock:
        conn = _open()
        try:
            _sync(conn)
            total = conn.execute(f"{_HITS} SELECT count(*) FROM hit",
                                 (stem, prefix)).fetchone()[0]
            rows = conn.execute(
                f"""{_HITS}
                    SELECT f.base, s.rowid % {_ROW}, s.start, s.who, s.text
                    FROM hit h JOIN segs s ON s.rowid = h.id
                    JOIN files f ON f.id = h.id / {_ROW}
                    ORDER BY f.mtime DESC, f.base, s.rowid
                    LIMIT ?""", (stem, prefix, limit)).fetchall() if total else []
        finally:
            conn.close()
            _secure(db_path())
    hits = [{"base": base, "index": idx, "start": start, "who": who,
             "snippet": _snippet(text, q, toks)}
            for base, idx, start, who, text in rows]
    return {"query": q, "hits": hits, "total": total}


def _discard_if_corrupt(e):
    """A damaged index is rebuilt, not repaired: delete it and let the next
    query make a new one. A busy or locked index is left alone."""
    if isinstance(e, sqlite3.DatabaseError) and not isinstance(e, sqlite3.OperationalError):
        with _lock:
            for f in _companions(db_path()):
                try:
                    f.unlink()
                except OSError:
                    pass


def query(q: str, limit: int = 40) -> dict:
    """Search every live transcript line. FTS first, the scan when FTS finds
    nothing or fails. Returns {query, hits, total}, newest meeting first."""
    q = (q or "").strip().lower()
    if len(q) < 3:
        return {"query": q, "hits": [], "total": 0}
    try:
        r = _fts(q, limit)
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable, scanning instead ({e})")
        _discard_if_corrupt(e)
        r = None
    if r is None or r["total"] == 0:
        return _scan(q, limit)
    return r


def meetings(q: str, limit: int = 20) -> list:
    """Meetings whose title or AI summary matches, best match first:
    [{base, title}]. Not behind /api/search, whose line-level shape the page
    depends on. Empty when the index cannot answer."""
    q = (q or "").strip().lower()
    _, expr, _ = _match_expr(q)
    if len(q) < 3 or expr is None:
        return []
    try:
        with _lock:
            conn = _open()
            try:
                _sync(conn)
                rows = conn.execute(
                    """SELECT f.base, d.title FROM docs d
                       JOIN files f ON f.id = d.rowid
                       WHERE docs MATCH ? ORDER BY bm25(docs, 5.0, 1.0) LIMIT ?""",
                    (expr, limit)).fetchall()
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable ({e})")
        _discard_if_corrupt(e)
        return []
    return [{"base": b, "title": t} for b, t in rows]


# ------------------------------------------------------------ fallback scan ---
# The pre-index search, kept whole: segment texts cached per file mtime, a
# case-insensitive substring scan. It is the only path that matches mid-word.

_cache = {}  # path -> (mtime, [(seg_index, start, who, text_lower, text)])


def _mtime(p):
    """Sort key that tolerates a meeting vanishing mid-request (concurrent
    rename/delete), mirroring _segments()'s own FileNotFoundError guard."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _segments(jpath):
    try:
        mtime = jpath.stat().st_mtime
    except FileNotFoundError:
        return []
    hit = _cache.get(str(jpath))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        d = json.loads(jpath.read_text())
        # the roster's label, as the transcript shows it: a below-floor
        # voice's lines carry no display, and a hit read "SPEAKER_08"
        roster = output.roster_labels(d.get("speakers", []))
        rows = [(i, s["start"], output.line_label(s, roster),
                 s.get("text", "").lower(), s.get("text", ""))
                for i, s in enumerate(d.get("segments", []))
                if s.get("text", "").strip()]
    except Exception:
        rows = []
    _cache[str(jpath)] = (mtime, rows)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    return rows


def _scan(q: str, limit: int = 40) -> dict:
    """Case-insensitive substring search over all transcript segments.
    `q` arrives stripped and lowercased. Newest meeting first."""
    hits, total = [], 0
    files = sorted((config.meeting_file(b, ".json") for b in config.meeting_bases()),
                   key=_mtime, reverse=True)
    for jpath in files:
        base = jpath.stem
        for idx, start, who, low, text in _segments(jpath):
            pos = low.find(q)
            if pos < 0:
                continue
            total += 1
            if len(hits) < limit:
                a = max(0, pos - 60)
                b = min(len(text), pos + len(q) + 90)
                snippet = (("…" if a else "") + text[a:b] + ("…" if b < len(text) else ""))
                hits.append({"base": base, "index": idx, "start": start,
                             "who": who, "snippet": snippet})
    return {"query": q, "hits": hits, "total": total}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Manage the meeting search index.")
    ap.add_argument("--rebuild", action="store_true",
                    help="delete the index and build it again from the meeting jsons")
    a = ap.parse_args(argv)
    if a.rebuild:
        r = rebuild()
        print(f"indexed {r['meetings']} meetings, {r['lines']} lines "
              f"in {r['seconds']} s")
    else:
        n = sync()
        print(f"index up to date ({n} meetings changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

SCHEMA = "4"
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


_TABLES = ("segs", "segs_raw", "docs", "docs_raw", "files")


def _schema(conn):
    # the index describes ONE store in ONE shape: a schema change or a moved
    # meetings folder (Settings can point it elsewhere) drops every table,
    # and the next sync fills them again from the folder it now names. The
    # tables are dropped, not emptied, so a new column always arrives.
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    want = {"schema": SCHEMA, "meetings_dir": str(config.meetings_dir())}
    have = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if not all(have.get(k) == v for k, v in want.items()):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # another process may have rebuilt it while this one waited for
            # the write lock: look again before dropping its fresh tables
            have = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            if all(have.get(k) == v for k, v in want.items()):
                conn.execute("COMMIT")
                return _schema(conn)
            for t in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute("DELETE FROM meta")
            conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                             list(want.items()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    conn.executescript(f"""
        -- one row per live meeting. The filter columns (plan Phase 4E) are
        -- read from the json at index time: `day` is the date part of
        -- recorded_at when it is set, else the stored date. A meeting from
        -- before the amnesty keys has null date_source and a route worked
        -- out from its source name, as the panel does.
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            base TEXT NOT NULL UNIQUE,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            mid TEXT,
            day TEXT,
            recorded_at TEXT,
            date_source TEXT,
            route TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS segs USING fts5(
            text, start UNINDEXED, who UNINDEXED, tokenize='{_TOKENIZE}');
        -- the same lines, unstemmed, for the last-word prefix. Contentless:
        -- the text is read from segs by rowid, so it is not stored twice
        CREATE VIRTUAL TABLE IF NOT EXISTS segs_raw USING fts5(
            text, content='', contentless_delete=1, tokenize='{_RAW_TOKENIZE}');
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            title, summary, tokenize='{_TOKENIZE}');
        -- the same titles and summaries, unstemmed, for the last-word
        -- prefix in meetings(), as segs_raw does for lines
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_raw USING fts5(
            title, summary, content='', contentless_delete=1,
            tokenize='{_RAW_TOKENIZE}');
    """)


# ---------------------------------------------------------------- indexing ---

def _summary_text(d) -> str:
    steps = d.get("ai_next_steps") or []
    if isinstance(steps, str):
        steps = [steps]
    # ai_summary already holds the gist and the decision and question lists;
    # the tags go in by label, so a search for "Special education" finds them
    from . import series
    tags = [series.TAG_LABELS.get(t, t) for t in (d.get("ai_tags") or [])
            if isinstance(t, str)]
    parts = ([d.get("ai_title"), d.get("ai_summary")] + [str(s) for s in steps]
             + ([" · ".join(tags)] if tags else []))
    return "\n".join(str(p) for p in parts if p)


def facets(d, base: str) -> dict:
    """The filter keys of one meeting json (plan Phase 4E): meeting_id, the
    day it was recorded, recorded_at, date_source, and route. Every key may
    be missing on an old meeting. `route` then comes from the source name, as
    the panel works it out, and `day` from the folder stamp."""
    from . import titles
    d = d if isinstance(d, dict) else {}
    ra = d.get("recorded_at")
    ra = ra if isinstance(ra, str) and ra else None
    day = (ra[:10] if ra else None) or d.get("date") or dates.meeting_date(base)
    route = d.get("route")
    if route not in titles.ROUTES:
        try:
            route = titles.route_for(d.get("source_file") or "")
        except Exception:
            route = "other"
    ds = d.get("date_source")
    return {"mid": d.get("meeting_id") or None,
            "day": day if isinstance(day, str) else None,
            "recorded_at": ra,
            "date_source": ds if isinstance(ds, str) else None,
            "route": route}


def _read(base: str, jpath: Path):
    """(lines, title, summary, facets) for one meeting json, or None if it
    cannot be read right now. Lines are (index, start, who, text), blank lines skipped,
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
        return lines, title, _summary_text(d), facets(d, base)
    except Exception as e:
        _warn(f"bad:{base}", f"{base}: transcript not indexed ({type(e).__name__})")
        return [], dates.title_of(base), "", facets(d if isinstance(d, dict) else {}, base)


def _drop(conn, fid: int):
    lo = fid * _ROW
    conn.execute("DELETE FROM segs WHERE rowid >= ? AND rowid < ?", (lo, lo + _ROW))
    conn.execute("DELETE FROM segs_raw WHERE rowid >= ? AND rowid < ?", (lo, lo + _ROW))
    conn.execute("DELETE FROM docs WHERE rowid = ?", (fid,))
    conn.execute("DELETE FROM docs_raw WHERE rowid = ?", (fid,))


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
    lines, title, summary, fc = got
    cols = (fc["mid"], fc["day"], fc["recorded_at"], fc["date_source"], fc["route"])
    if row:
        fid = row[0]
        conn.execute("""UPDATE files SET mtime = ?, size = ?, mid = ?, day = ?,
                        recorded_at = ?, date_source = ?, route = ? WHERE id = ?""",
                     (mtime, size, *cols, fid))
    else:
        fid = conn.execute(
            """INSERT INTO files(base, mtime, size, mid, day, recorded_at,
                                 date_source, route) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (base, mtime, size, *cols)).lastrowid
    rows = [(fid * _ROW + i, text, start, who)
            for i, start, who, text in lines if i < _ROW]
    conn.executemany("INSERT INTO segs(rowid, text, start, who) VALUES (?, ?, ?, ?)",
                     rows)
    conn.executemany("INSERT INTO segs_raw(rowid, text) VALUES (?, ?)",
                     [(r[0], r[1]) for r in rows])
    conn.execute("INSERT INTO docs(rowid, title, summary) VALUES (?, ?, ?)",
                 (fid, title, summary))
    conn.execute("INSERT INTO docs_raw(rowid, title, summary) VALUES (?, ?, ?)",
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
    because the next query's sync repairs anything it misses. The one-second
    bound covers the in-process lock too: a full rebuild holds `_lock` for
    minutes, and a panel rename must not wait behind it."""
    try:
        if not _lock.acquire(timeout=1.0):
            _warn("touch", "index busy, not updated now, the next search will")
            return
        try:
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
        finally:
            _lock.release()
    except Exception as e:
        _warn("touch", f"index not updated now, the next search will ({e})")


# ------------------------------------------------------------- query text ---

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


# ----------------------------------------------------------------- filters ---
# Plan Phase 4E. A filter narrows which MEETINGS a search reads. It comes as
# a query parameter on /api/search or typed inline in the query ("budget
# after:2026-10-01 route:voice_memo"). Values:
#   after:YYYY-MM-DD   recorded on or after that day
#   before:YYYY-MM-DD  recorded before that day (the day itself is out)
#   route:a,b          the route that brought it in (titles.ROUTES)
#   dated:a,b          its date_source (provenance.DATE_SOURCES), or `none`
#                      for a meeting without the key (from before the amnesty)
#   from:word          the line's speaker label contains the word
#   in:<group key>     a computed group (stt/groups.py)
# The day is the date part of recorded_at when it is set, else the stored
# date. A meeting with no day at all is outside every date filter.

FILTER_KEYS = ("after", "before", "route", "date_source", "from", "in")
_INLINE_NAMES = {"after": "after", "before": "before", "route": "route",
                 "dated": "date_source", "date_source": "date_source",
                 "from": "from", "in": "in"}
_INLINE = re.compile(r'(?<!\S)(after|before|route|dated|date_source|from|in):'
                     r'("[^"]*"|\S+)', re.I)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FilterError(ValueError):
    """A filter value this module cannot read. The message says which."""


def split_query(q: str):
    """(text, {filter: raw value}) from a query with inline filters. The
    filter words leave the text, so 'budget after:2026-10-01' searches for
    'budget'. A later copy of one filter wins."""
    raw = {}

    def take(m):
        raw[_INLINE_NAMES[m.group(1).lower()]] = m.group(2).strip('"')
        return " "
    text = _INLINE.sub(take, str(q or ""))
    return re.sub(r"\s+", " ", text).strip(), raw


def _listval(v):
    if isinstance(v, (list, tuple, set)):
        items = [str(x) for x in v]
    else:
        items = str(v or "").split(",")
    return [x.strip().lower() for x in items if x.strip()]


def clean_filters(raw: dict) -> dict:
    """Check and normalize filter values. Unknown keys are ignored, and an
    empty value means no filter. Raises FilterError on a bad value."""
    from . import provenance, titles
    f = {}
    for k in ("after", "before"):
        v = str((raw or {}).get(k) or "").strip()
        if v:
            if not _DAY.match(v):
                raise FilterError(f"{k}: should look like 2026-10-01")
            try:
                from datetime import date as _d
                _d.fromisoformat(v)
            except ValueError:
                raise FilterError(f"{k}: {v} is not a real day") from None
            f[k] = v
    r = _listval((raw or {}).get("route"))
    if r:
        bad = [x for x in r if x not in titles.ROUTES]
        if bad:
            raise FilterError(f"route: '{bad[0]}' is not one of "
                              + ", ".join(titles.ROUTES))
        f["route"] = sorted(set(r))
    ds = _listval((raw or {}).get("date_source"))
    if ds:
        ok = set(provenance.DATE_SOURCES) | {"none"}
        bad = [x for x in ds if x not in ok]
        if bad:
            raise FilterError(f"dated: '{bad[0]}' is not one of "
                              + ", ".join(sorted(ok)))
        f["date_source"] = sorted(set(ds))
    who = str((raw or {}).get("from") or "").strip().lower()
    if who:
        f["from"] = who
    g = str((raw or {}).get("in") or "").strip()
    if g:
        f["in"] = g
    return f


def _group_bases(key: str) -> set:
    from . import groups
    return set(groups.members(key))


def meeting_passes(fc: dict, f: dict, bases=None) -> bool:
    """Does one meeting's facets pass the meeting-level filters? `bases` is
    the resolved `in:` set, or None for no group filter."""
    if bases is not None and fc.get("base") not in bases:
        return False
    day = fc.get("day")
    if "after" in f and not (day and day >= f["after"]):
        return False
    if "before" in f and not (day and day < f["before"]):
        return False
    if "route" in f and fc.get("route") not in f["route"]:
        return False
    if "date_source" in f and (fc.get("date_source") or "none") not in f["date_source"]:
        return False
    return True


def _where(f: dict, bases):
    """SQL conditions on files f (and segs s, for `from`) for the filters."""
    conds, args = [], []
    if bases is not None:
        conds.append("f.base IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(sorted(bases)))
    if "after" in f:
        conds.append("f.day >= ?")
        args.append(f["after"])
    if "before" in f:
        conds.append("f.day < ?")
        args.append(f["before"])
    if "route" in f:
        conds.append("f.route IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(f["route"]))
    if "date_source" in f:
        conds.append("coalesce(f.date_source, 'none') IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(f["date_source"]))
    if "from" in f:
        conds.append("instr(lower(coalesce(s.who, '')), ?) > 0")
        args.append(f["from"])
    return conds, args


# ---------------------------------------------------------------- querying ---

_HITS = """WITH hit(id) AS (
                SELECT rowid FROM segs WHERE segs MATCH ?
                UNION
                SELECT rowid FROM segs_raw WHERE segs_raw MATCH ?)"""


def _fts(q: str, limit: int, f=None, bases=None):
    stem, prefix, toks = _match_expr(q)
    if stem is None:
        return None
    f = f or {}
    conds, cargs = _where(f, bases)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _lock:
        conn = _open()
        try:
            _sync(conn)
            if conds:
                total = conn.execute(
                    f"""{_HITS} SELECT count(*) FROM hit h
                        JOIN segs s ON s.rowid = h.id
                        JOIN files f ON f.id = h.id / {_ROW}{where}""",
                    (stem, prefix, *cargs)).fetchone()[0]
            else:
                total = conn.execute(f"{_HITS} SELECT count(*) FROM hit",
                                     (stem, prefix)).fetchone()[0]
            rows = conn.execute(
                f"""{_HITS}
                    SELECT f.base, s.rowid % {_ROW}, s.start, s.who, s.text
                    FROM hit h JOIN segs s ON s.rowid = h.id
                    JOIN files f ON f.id = h.id / {_ROW}{where}
                    ORDER BY f.mtime DESC, f.base, s.rowid
                    LIMIT ?""", (stem, prefix, *cargs, limit)).fetchall() if total else []
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


def _prepare(q, filters):
    """(text, clean filters, group bases or None) for a query plus any
    explicit filters. Inline filters in the text win over explicit ones."""
    text, inline = split_query(q)
    raw = dict(filters or {})
    raw.update(inline)
    f = clean_filters(raw)
    bases = _group_bases(f["in"]) if "in" in f else None
    return text.strip().lower(), f, bases


def query(q: str, limit: int = 40, filters: dict | None = None) -> dict:
    """Search every live transcript line. FTS first, the scan when FTS finds
    nothing or fails. Returns {query, hits, total}, newest meeting first.
    With filters (inline in `q` or in `filters`), the answer also carries
    `filters`, the values used, and a bad value answers with `error`."""
    try:
        q, f, bases = _prepare(q, filters)
    except FilterError as e:
        return {"query": (q or "").strip().lower(), "hits": [], "total": 0,
                "error": str(e)}
    out = _query(q, limit, f, bases)
    if f:
        out["filters"] = f
    return out


def _query(q, limit, f, bases):
    if len(q) < 3:
        return {"query": q, "hits": [], "total": 0}
    try:
        r = _fts(q, limit, f, bases)
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable, scanning instead ({e})")
        _discard_if_corrupt(e)
        r = None
    if r is None or r["total"] == 0:
        return _scan(q, limit, f, bases)
    return r


def meetings(q: str, limit: int = 20, filters: dict | None = None) -> list:
    """Meetings whose title or AI summary matches, best match first:
    [{base, title}]. Not behind /api/search, whose line-level shape the page
    depends on. Empty when the index cannot answer or a filter is bad."""
    try:
        q, f, bases = _prepare(q, filters)
    except FilterError:
        return []
    f.pop("from", None)          # a speaker filter has no meaning for a title
    stem, prefix, _ = _match_expr(q)
    if len(q) < 3 or stem is None:
        return []
    conds, cargs = _where(f, bases)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    try:
        with _lock:
            conn = _open()
            try:
                _sync(conn)
                # the whole words match stemmed (`docs`), the last word as a
                # prefix of an unstemmed word (`docs_raw`): porter stems a
                # part-typed word, so 'attendan*' on `docs` finds nothing
                rows = conn.execute(
                    f"""SELECT f.base, d.title, min(h.r) AS r FROM (
                           SELECT rowid AS id, bm25(docs, 5.0, 1.0) AS r
                             FROM docs WHERE docs MATCH ?
                           UNION ALL
                           SELECT rowid, bm25(docs_raw, 5.0, 1.0)
                             FROM docs_raw WHERE docs_raw MATCH ?) h
                       JOIN docs d ON d.rowid = h.id
                       JOIN files f ON f.id = h.id{where}
                       GROUP BY h.id ORDER BY r, f.base LIMIT ?""",
                    (stem, prefix, *cargs, limit)).fetchall()
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable ({e})")
        _discard_if_corrupt(e)
        return []
    return [{"base": b, "title": t} for b, t, _r in rows]


def ranked_lines(terms, bases=None, limit: int = 60) -> list:
    """Lines that match ANY of `terms`, best first by bm25 (plan Phase 4F,
    the retrieval step of library Ask). Each term is a \\w-only word, matched
    stemmed. `bases` limits the meetings (None for all). Returns
    [{base, mid, day, title, index, start, who, text, rank}], or [] when the
    index cannot answer."""
    toks = [t for t in dict.fromkeys(str(x).lower() for x in terms or [])
            if re.fullmatch(r"\w+", t)]
    if not toks:
        return []
    expr = " OR ".join(f'"{t}"' for t in toks)
    conds, cargs = _where({}, bases)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    try:
        with _lock:
            conn = _open()
            try:
                _sync(conn)
                rows = conn.execute(
                    f"""SELECT f.base, f.mid, f.day, d.title, s.rowid % {_ROW},
                               s.start, s.who, s.text, bm25(segs) AS r
                        FROM segs s JOIN files f ON f.id = s.rowid / {_ROW}
                        LEFT JOIN docs d ON d.rowid = f.id
                        WHERE segs MATCH ?{where}
                        ORDER BY r LIMIT ?""", (expr, *cargs, limit)).fetchall()
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable ({e})")
        _discard_if_corrupt(e)
        return []
    keys = ("base", "mid", "day", "title", "index", "start", "who", "text", "rank")
    return [dict(zip(keys, r)) for r in rows]


def neighbors(base: str, index: int, before: int = 2, after: int = 2) -> list:
    """The indexed lines of one meeting from `before` lines ahead of `index`
    to `after` lines past it, in order: [{index, start, who, text}]. Blank
    lines are not indexed, so the window counts indexed lines only."""
    try:
        with _lock:
            conn = _open(create=False)
            if conn is None:
                return []
            try:
                row = conn.execute("SELECT id FROM files WHERE base = ?", (base,)).fetchone()
                if not row:
                    return []
                lo, mid = row[0] * _ROW, row[0] * _ROW + int(index)
                head = conn.execute(
                    "SELECT rowid, start, who, text FROM segs WHERE rowid >= ? AND rowid < ? "
                    "ORDER BY rowid DESC LIMIT ?", (lo, mid, before)).fetchall()
                tail = conn.execute(
                    "SELECT rowid, start, who, text FROM segs WHERE rowid >= ? AND rowid < ? "
                    "ORDER BY rowid LIMIT ?", (mid, lo + _ROW, after + 1)).fetchall()
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable ({e})")
        return []
    return [{"index": r[0] % _ROW, "start": r[1], "who": r[2], "text": r[3]}
            for r in list(reversed(head)) + tail]


def facet_rows() -> list:
    """[{base, mid, day, recorded_at, date_source, route}] for every live
    meeting, from the index (synced first). Empty when the index fails."""
    try:
        with _lock:
            conn = _open()
            try:
                _sync(conn)
                rows = conn.execute(
                    "SELECT base, mid, day, recorded_at, date_source, route FROM files"
                ).fetchall()
            finally:
                conn.close()
                _secure(db_path())
    except Exception as e:
        _warn(type(e).__name__, f"index unavailable ({e})")
        _discard_if_corrupt(e)
        return []
    keys = ("base", "mid", "day", "recorded_at", "date_source", "route")
    return [dict(zip(keys, r)) for r in rows]


# ------------------------------------------------------------ fallback scan ---
# The pre-index search, kept whole: segment texts cached per file mtime, a
# case-insensitive substring scan. It is the only path that matches mid-word.

_cache = {}  # path -> (mtime, [(seg_index, start, who, text_lower, text)], facets)


def _mtime(p):
    """Sort key that tolerates a meeting vanishing mid-request (concurrent
    rename/delete), mirroring _segments()'s own FileNotFoundError guard."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _segments(jpath):
    """(rows, facets) for one meeting json, cached on its mtime."""
    try:
        mtime = jpath.stat().st_mtime
    except FileNotFoundError:
        return [], {"base": jpath.stem}
    hit = _cache.get(str(jpath))
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    fc = {}
    try:
        d = json.loads(jpath.read_text())
        fc = facets(d, jpath.stem)
        # the roster's label, as the transcript shows it: a below-floor
        # voice's lines carry no display, and a hit read "SPEAKER_08"
        roster = output.roster_labels(d.get("speakers", []))
        rows = [(i, s["start"], output.line_label(s, roster),
                 s.get("text", "").lower(), s.get("text", ""))
                for i, s in enumerate(d.get("segments", []))
                if s.get("text", "").strip()]
    except Exception:
        rows = []
    fc["base"] = jpath.stem
    _cache[str(jpath)] = (mtime, rows, fc)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    return rows, fc


def _scan(q: str, limit: int = 40, f=None, bases=None) -> dict:
    """Case-insensitive substring search over all transcript segments.
    `q` arrives stripped and lowercased. Newest meeting first. The filters
    apply as they do on the index path."""
    f = f or {}
    hits, total = [], 0
    files = sorted((config.meeting_file(b, ".json") for b in config.meeting_bases()),
                   key=_mtime, reverse=True)
    who_has = f.get("from")
    for jpath in files:
        base = jpath.stem
        rows, fc = _segments(jpath)
        if (f or bases is not None) and not meeting_passes(fc, f, bases):
            continue
        for idx, start, who, low, text in rows:
            pos = low.find(q)
            if pos < 0:
                continue
            if who_has and who_has not in str(who or "").lower():
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

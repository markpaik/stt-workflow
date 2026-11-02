"""Computed groups of meetings (unified plan Phase 4E, design-plaud-ui.md
section 8 "Groups").

Groups form by themselves. Nothing is filed and no file moves: a group is a
view computed on read from what the meeting jsons already say.

How a group forms:

1. Title words. Each title loses its date stamp, twin suffix, and stop words
   ("the", "meeting", "call", ...). What is left is the meeting's word set.
   Words such as "weekly", "check", "sync", or "update" are WEAK: they count
   toward the overlap, but two titles that share only weak words never join.
   Meetings with the same word set start as one cluster. Clusters then merge
   by average linkage while their mean word overlap (Jaccard) is 0.6 or
   more. Average linkage, not single linkage, because the audit's fuzzy
   match chained one group to 21 spellings.
2. People and hour. A meeting whose title has no strong word (a recorder
   default, a marker name) joins the title cluster whose members share its
   exact set of named people (the owner left out) at the same hour of day,
   give or take one. Failing that, such meetings group with each other on
   the same people at the same hour.
3. A group needs two or more meetings. A meeting with no match has no group,
   and nothing asks about it.

Corrections: "Not part of this group" writes `group_overrides.json` in
PROJECT_DIR (mode 600), shaped {meeting_id: {"not_in": [group key, ...]}}.
A meeting without an id is keyed on its folder name. The next computation
leaves the meeting out of that group, and a group that falls under two
meetings leaves the list. Undo removes the key again. A group key is a hash
of the cluster's most common word set (or its people and hour), so it holds
while members come and go, and changes only when the dominant spelling does.

Use count: `group_usage.jsonl` in PROJECT_DIR gets one line per group open
or group question, {at, event}, with no title and no key, for the one-month
count the plan asks for (`python -m stt.groups --usage`).

`python -m stt.groups --quality` prints the share of meetings grouped, the
group count, and the largest group, beside the audit's 9 groups. It reads
only, and it prints counts, never a title.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import config, dates

THRESHOLD = 0.6       # mean word overlap (Jaccard) to merge two clusters
MIN_SIZE = 2          # a group needs two or more meetings
HOUR_SLACK = 1        # people-and-hour match: the same hour, give or take one
_AVERAGE_MAX = 800    # word sets above this merge by single linkage (speed)

# Words that say nothing about WHICH meeting this is. Dropped.
_STOP = {
    "a", "an", "and", "the", "of", "for", "to", "on", "in", "at", "with", "w",
    "re", "by", "from", "about", "my", "our", "vs", "x", "into", "or", "is",
    "meeting", "meetings", "mtg", "call", "calls", "recording", "recordings",
    "new", "copy", "memo", "voice", "audio", "untitled", "notes", "session",
}
# Words that recur across unrelated series. They count toward the overlap,
# but a pair that shares only these never joins.
_WEAK = {
    "weekly", "biweekly", "monthly", "daily", "check", "checkin", "sync",
    "update", "updates", "1", "one", "team", "touch", "base", "standup",
    "stand", "up", "review", "follow", "followup", "planning", "plan",
    "debrief", "prep", "discussion", "chat", "intro", "kickoff", "office",
    "hours", "huddle", "connect", "catch", "1on1",
}
_WORD = re.compile(r"[a-z0-9]+")
_DIGITS = re.compile(r"^\d+$")
_lock = threading.RLock()


# ------------------------------------------------------------------ words ---

def words(title: str) -> frozenset:
    """The meeting's word set: lowercase words of its title, without stop
    words or bare numbers (dates and times say nothing about the series)."""
    t = str(title or "").lower().replace("1-on-1", "1on1").replace("1:1", "1on1")
    out = set()
    for w in _WORD.findall(t):
        if w in _STOP or (_DIGITS.match(w) and w != "1"):
            continue
        out.add(w)
    out.discard("1")             # "1" only ever meant "1-on-1", handled above
    return frozenset(out)


def strong(ws) -> frozenset:
    return frozenset(w for w in ws if w not in _WEAK)


def jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _linkable(a, b) -> float:
    """The overlap of two word sets, or 0 when they share no strong word."""
    if not (strong(a) & strong(b)):
        return 0.0
    return jaccard(a, b)


# ------------------------------------------------------------------- rows ---

_row_cache = {}   # json path -> (mtime, row)


def _hour(ra):
    if not ra:
        return None
    try:
        return datetime.fromisoformat(str(ra)).hour
    except ValueError:
        return None


def row_from_json(base: str, d: dict) -> dict:
    """The few keys grouping reads, from one meeting json."""
    d = d if isinstance(d, dict) else {}
    named = []
    for s in d.get("speakers") or []:
        if isinstance(s, dict) and s.get("name"):
            nm = s.get("display") or s.get("name")
            if nm not in named:
                named.append(nm)
    ra = d.get("recorded_at") if isinstance(d.get("recorded_at"), str) else None
    return {"base": base,
            "meeting_id": d.get("meeting_id") or None,
            "title": d.get("title") or dates.title_of(base),
            "named": named,
            "recorded_at": ra,
            "date": d.get("date") or dates.meeting_date(base),
            "duration_sec": float(d.get("duration_sec") or 0.0)}


def load_rows() -> list:
    """One grouping row per live meeting, read from the jsons and cached on
    each file's mtime. Reads only."""
    rows = []
    live = set()
    for b in config.meeting_bases():
        j = config.meeting_file(b, ".json")
        live.add(str(j))
        try:
            mt = j.stat().st_mtime
        except OSError:
            continue
        hit = _row_cache.get(str(j))
        if hit and hit[0] == mt:
            rows.append(hit[1])
            continue
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        r = dict(row_from_json(b, d), _mt=mt)
        _row_cache[str(j)] = (mt, r)
        rows.append(r)
    for k in [k for k in _row_cache if k not in live]:
        _row_cache.pop(k, None)
    return rows


# -------------------------------------------------------------- overrides ---

def overrides_path() -> Path:
    return Path(config.PROJECT_DIR) / "group_overrides.json"


def load_overrides() -> dict:
    """{meeting key: set of group keys it is not part of}. A missing or bad
    file is no corrections. A string `not_in` is read as one key."""
    try:
        d = json.loads(overrides_path().read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        ni = v.get("not_in") if isinstance(v, dict) else None
        if isinstance(ni, str):
            ni = [ni]
        if isinstance(ni, list):
            keys = {str(x) for x in ni if x}
            if keys:
                out[str(k)] = keys
    return out


@contextlib.contextmanager
def _file_lock():
    p = Path(str(overrides_path()) + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write_600(p: Path, text: str):
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def meeting_key(row: dict) -> str:
    """What an override is keyed on: the meeting_id, else the folder name."""
    return row.get("meeting_id") or row["base"]


def set_override(mkey: str, group_key: str, exclude: bool = True) -> dict:
    """Record (or, with exclude=False, undo) "not part of this group" for
    one meeting. Returns {ok, not_in}."""
    mkey, group_key = str(mkey or "").strip(), str(group_key or "").strip()
    if not mkey or not group_key:
        return {"ok": False, "error": "a meeting and a group are both needed"}
    with _lock, _file_lock():
        p = overrides_path()
        try:
            d = json.loads(p.read_text())
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        cur = d.get(mkey) if isinstance(d.get(mkey), dict) else {}
        ni = cur.get("not_in")
        ni = [ni] if isinstance(ni, str) else list(ni or [])
        if exclude and group_key not in ni:
            ni.append(group_key)
        if not exclude:
            ni = [x for x in ni if x != group_key]
        if ni:
            cur["not_in"] = ni
            d[mkey] = cur
        else:
            d.pop(mkey, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_600(p, json.dumps(d, indent=2, ensure_ascii=False))
    _memo["sig"] = None
    return {"ok": True, "not_in": ni}


# ---------------------------------------------------------------- compute ---

def _key(kind: str, sig) -> str:
    raw = kind + "|" + "|".join(sorted(str(x) for x in sig))
    return "g" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _cluster_titles(sigs: list) -> list:
    """Average-linkage clustering of distinct word sets. `sigs` is a list of
    (word set, weight). Returns a list of lists of indexes into `sigs`."""
    n = len(sigs)
    clusters = {i: [i] for i in range(n)}
    weight = {i: sigs[i][1] for i in range(n)}
    if n > _AVERAGE_MAX:                # single linkage, union-find
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i in range(n):
            for j in range(i + 1, n):
                if _linkable(sigs[i][0], sigs[j][0]) >= THRESHOLD:
                    parent[find(i)] = find(j)
        out = {}
        for i in range(n):
            out.setdefault(find(i), []).append(i)
        return list(out.values())
    sim = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = _linkable(sigs[i][0], sigs[j][0])
            if s > 0:
                sim[(i, j)] = s

    def get(a, b):
        return sim.get((a, b) if a < b else (b, a), 0.0)
    while True:
        best, pair = 0.0, None
        for (a, b), s in sim.items():
            if s > best or (s == best and pair and (a, b) < pair):
                best, pair = s, (a, b)
        if pair is None or best < THRESHOLD:
            break
        a, b = pair
        wa, wb = weight[a], weight[b]
        others = [k for k in clusters if k not in (a, b)]
        new = {}
        for k in others:
            s = (wa * get(a, k) + wb * get(b, k)) / (wa + wb)
            if s > 0:
                new[k] = s
        for key in [p for p in sim if a in p or b in p]:
            sim.pop(key)
        clusters[a] = clusters[a] + clusters.pop(b)
        weight[a] = wa + wb
        weight.pop(b)
        for k, s in new.items():
            sim[(a, k) if a < k else (k, a)] = s
    return list(clusters.values())


def _people(row, me):
    return frozenset(p for p in (row.get("named") or []) if p and p != me)


def _near(h1, h2) -> bool:
    return h1 is not None and h2 is not None and abs(h1 - h2) <= HOUR_SLACK


def _name_and_why(kind, members, sig):
    titles = Counter(dates.title_of(m["base"]) or m.get("title") or "" for m in members)
    newest = max(members, key=lambda m: (m.get("date") or "", m["base"]))
    top = max(titles.values())
    name = (dates.title_of(newest["base"]) if titles[dates.title_of(newest["base"])] == top
            else sorted(t for t, c in titles.items() if c == top)[0])
    if kind == "title":
        why = "Titles share the words " + ", ".join(sorted(sig))
    else:
        people, hour = sig
        h = int(hour)
        clock = f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
        why = f"The same people, around {clock}"
    return name or "Untitled", why


def compute(rows: list, me=None, overrides=None) -> list:
    """The groups for these rows, largest first. Each group is
    {key, kind, name, why, count, hours, bases}, bases newest first.
    `overrides` is load_overrides() (read here when None)."""
    if overrides is None:
        overrides = load_overrides()
    me = me if me is not None else config.mic_speaker()
    by_base = {r["base"]: r for r in rows}
    ws = {r["base"]: words(r.get("title") or dates.title_of(r["base"])) for r in rows}
    titled = [b for b in ws if strong(ws[b])]
    untitled = [b for b in ws if not strong(ws[b])]

    # 1. title clusters over the distinct word sets
    sig_members = {}
    for b in titled:
        sig_members.setdefault(ws[b], []).append(b)
    sigs = list(sig_members)
    parts = _cluster_titles([(s, len(sig_members[s])) for s in sigs])
    out = []
    for idx in parts:
        members = [b for i in idx for b in sig_members[sigs[i]]]
        # the dominant spelling names the key: most members, then fewest words
        dom = max((sigs[i] for i in idx),
                  key=lambda s: (len(sig_members[s]), -len(s), sorted(s)))
        out.append({"kind": "title", "sig": dom, "members": members})

    # 2. people and hour for meetings with no strong title word
    leftovers = []
    for b in untitled:
        r = by_base[b]
        ppl, h = _people(r, me), _hour(r.get("recorded_at"))
        best, score = None, 0
        if ppl and h is not None:
            for g in out:
                n = sum(1 for m in g["members"]
                        if _people(by_base[m], me) == ppl
                        and _near(_hour(by_base[m].get("recorded_at")), h))
                if n > score:
                    best, score = g, n
        if best is not None:
            best["members"].append(b)
        elif ppl and h is not None:
            leftovers.append(b)
    pk = {}
    for b in leftovers:
        r = by_base[b]
        pk.setdefault((_people(r, me), _hour(r.get("recorded_at"))), []).append(b)
    for (ppl, h), members in pk.items():
        out.append({"kind": "people",
                    "sig": (",".join(sorted(ppl)), str(h)), "members": members})

    # 3. keys, corrections, and the two-meeting floor
    groups = []
    for g in out:
        key = (_key("t", g["sig"]) if g["kind"] == "title"
               else _key("p", g["sig"]))
        members = [b for b in g["members"]
                   if key not in overrides.get(meeting_key(by_base[b]), ())]
        if len(members) < MIN_SIZE:
            continue
        rs = sorted((by_base[b] for b in members),
                    key=lambda r: (r.get("recorded_at") or r.get("date") or "", r["base"]),
                    reverse=True)
        name, why = _name_and_why(g["kind"], rs, g["sig"])
        groups.append({"key": key, "kind": g["kind"], "name": name, "why": why,
                       "count": len(rs),
                       "hours": round(sum(r.get("duration_sec") or 0 for r in rs) / 3600, 1),
                       "bases": [r["base"] for r in rs]})
    groups.sort(key=lambda g: (-g["count"], g["name"].lower(), g["key"]))
    return groups


_memo = {"sig": None, "groups": []}


def current() -> list:
    """compute() over the live store, cached until a meeting json or the
    overrides file changes."""
    with _lock:
        rows = load_rows()
        try:
            omt = overrides_path().stat().st_mtime
        except OSError:
            omt = None
        sig = (str(config.meetings_dir()), str(overrides_path()),
               tuple(sorted((r["base"], r.get("_mt")) for r in rows)), omt,
               config.mic_speaker())
        if _memo["sig"] == sig:
            return _memo["groups"]
        g = compute(rows)
        _memo["sig"], _memo["groups"] = sig, g
        return g


def by_key(key: str):
    return next((g for g in current() if g["key"] == key), None)


def members(key: str) -> list:
    g = by_key(key)
    return list(g["bases"]) if g else []


def membership() -> dict:
    """{base: (group key, group name)} for every grouped meeting."""
    return {b: (g["key"], g["name"]) for g in current() for b in g["bases"]}


# ------------------------------------------------------------------- usage ---

USAGE_EVENTS = ("group_open", "group_ask")


def usage_path() -> Path:
    return Path(config.PROJECT_DIR) / "group_usage.jsonl"


def log_use(event: str) -> bool:
    """One line per group open or group question. No title, no key."""
    if event not in USAGE_EVENTS:
        return False
    p = usage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                       "event": event}) + "\n"
    fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(line)
    return True


def usage_counts(days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out = {e: 0 for e in USAGE_EVENTS}
    try:
        lines = usage_path().read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("event") in out and str(r.get("at", "")) >= since:
            out[r["event"]] += 1
    return out


# ----------------------------------------------------------------- quality ---

AUDIT = {"groups": 9, "spellings": 48, "largest_spellings": 21}


def quality(rows=None, overrides=None) -> dict:
    """Counts only: meetings, grouped share, groups, largest group, and the
    distinct title spellings inside groups, beside the audit's figures."""
    rows = load_rows() if rows is None else rows
    gs = compute(rows, overrides=overrides)
    grouped = sum(g["count"] for g in gs)
    spell = [len({dates.title_of(b) for b in g["bases"]}) for g in gs]
    return {"meetings": len(rows), "grouped": grouped,
            "grouped_share": round(grouped / len(rows), 3) if rows else 0.0,
            "groups": len(gs), "largest": max((g["count"] for g in gs), default=0),
            "spellings": sum(spell), "largest_spellings": max(spell, default=0),
            "by_kind": dict(Counter(g["kind"] for g in gs)),
            "audit": AUDIT}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Computed meeting groups (read only).")
    ap.add_argument("--quality", action="store_true",
                    help="counts beside the audit's 9 groups; no titles")
    ap.add_argument("--usage", action="store_true",
                    help="group opens and questions in the last 30 days")
    a = ap.parse_args(argv)
    if a.usage:
        print(json.dumps(usage_counts(), indent=2))
    else:
        print(json.dumps(quality(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

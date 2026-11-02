#!/usr/bin/env python
"""Daily counts for the review workload (unified plan, Phase 1 step 8).

Reads the meeting store and the unknown-voice registry, and writes nothing
but one row a day to PROJECT_DIR/review_metrics.jsonl. A row holds counts
only, never a title, a name, or a meeting id:

- meetings, by title_state, by reviewed, and by route
- Needs you: its rows, title rows apart from the other rows, and each kind
- arrivals in the last 14 days, by the stored processed_at
- silent recordings (no_speech)
- voices that ask to be named, and short voices hidden behind a count
- named talk: hours of talk and the share with a named speaker, for public
  downloads and for every other meeting

  ./run.sh py tools/review_metrics.py            # print today's row
  ./run.sh py tools/review_metrics.py --record   # append it, once a day

run_batch.py calls record_today() at the end of each run, so the log grows
by at most one row a day without a scheduler of its own.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, titles, unknowns  # noqa: E402


def metrics_path() -> Path:
    return Path(config.PROJECT_DIR) / "review_metrics.jsonl"


def _unknown_list() -> list:
    """The registry voices with their meetings, resolved read-only: a stored
    name that no longer exists falls back to its id's live base, found by
    reading each json's id (never by rebuilding the id index)."""
    reg = unknowns.load()
    ids = {}
    for b in config.meeting_bases():
        mid = config.id_of(b)
        if mid:
            ids[mid] = b
    live = set(config.meeting_bases())
    out = []
    for uid, meta in sorted(reg.get("speakers", {}).items()):
        if meta.get("dropped"):
            continue
        mts = []
        for base, mid in zip(meta.get("meetings", []) or [],
                             unknowns.aligned_meeting_ids(meta)):
            b = base if base in live else ids.get(mid)
            if b in live:
                mts.append(b)
        if mts:
            out.append({"uid": uid, "display": unknowns.display(uid),
                        "archived": bool(meta.get("archived")), "meetings": mts})
    return out


def _talk(d: dict) -> tuple:
    """(seconds of talk, seconds by a named speaker) over the lines with text."""
    named = {s.get("id") for s in d.get("speakers", []) if s.get("name")}
    total = named_s = 0.0
    for seg in d.get("segments", []):
        if not (seg.get("text") or "").strip():
            continue
        secs = max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
        total += secs
        if seg.get("speaker") in named:
            named_s += secs
    return total, named_s


def compute(now=None) -> dict:
    """Today's row. Reads only."""
    from gui import server as srv
    now = now or datetime.now()
    dst = config.meetings_dir()
    metas, c_state, c_rev, c_route = [], Counter(), Counter(), Counter()
    talk = {"public": [0.0, 0.0], "other": [0.0, 0.0]}
    arrivals = silent = 0
    for b in config.meeting_bases():
        j = config.meeting_file(b, ".json")
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        meta = srv._meeting_meta(j, dst)
        if meta is None:
            continue
        metas.append(meta)
        c_state[d.get("title_state") or "missing"] += 1
        r = d.get("reviewed")
        c_rev["missing" if r is None else ("true" if r else "false")] += 1
        route = meta.get("route") or "other"
        c_route[route] += 1
        if srv._arrived_within(meta, now):
            arrivals += 1
        if d.get("no_speech"):
            silent += 1
        t, n = _talk(d)
        side = talk["public" if route == "public_download" else "other"]
        side[0] += t
        side[1] += n
    ny = srv._needs_you(metas, set(), _unknown_list(), now=now)
    kinds = Counter(x["kind"] for x in ny)

    def _side(v):
        return {"talk_hours": round(v[0] / 3600, 2), "named_hours": round(v[1] / 3600, 2),
                "named_share": round(v[1] / v[0], 3) if v[0] else None}

    return {"date": now.date().isoformat(),
            "at": now.isoformat(timespec="seconds"),
            "meetings": len(metas),
            "title_state": dict(sorted(c_state.items())),
            "reviewed": dict(sorted(c_rev.items())),
            "route": dict(sorted(c_route.items())),
            "needs_you": {"rows": len(ny), "title_rows": kinds.get("needs_title", 0),
                          "other_rows": len(ny) - kinds.get("needs_title", 0),
                          "by_kind": dict(sorted(kinds.items()))},
            "arrivals_14d": arrivals,
            "no_speech": silent,
            "voices": {"asking": sum(len(m.get("ask_clusters") or {}) for m in metas),
                       "short_hidden": sum(int(m.get("short_voices") or 0) for m in metas)},
            "named_talk": {"public": _side(talk["public"]),
                           "non_public": _side(talk["other"])}}


def _last_date(p: Path):
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", "replace").strip().splitlines()
        return json.loads(tail[-1]).get("date") if tail else None
    except (OSError, ValueError, IndexError):
        return None


def record_today(now=None) -> bool:
    """Append today's row unless today already has one. True when written."""
    now = now or datetime.now()
    p = metrics_path()
    if _last_date(p) == now.date().isoformat():
        return False
    row = compute(now)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--record", action="store_true",
                    help="append today's row to review_metrics.jsonl (once a day)")
    a = ap.parse_args(argv)
    if a.record:
        wrote = record_today()
        print("recorded" if wrote else "today already has a row")
        return 0
    print(json.dumps(compute(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

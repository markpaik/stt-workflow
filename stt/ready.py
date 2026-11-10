"""The ready count: meetings that became readable since the owner last looked.

The menu bar shows this count so the owner learns that a meeting is ready
without opening the panel to check (unified plan, Phase 1 step 7, row 28).
"Ready" means processed and readable. It says nothing about human review.

The last look lives in PROJECT_DIR/panel_seen.json: the time of the look and
the ids of every meeting that existed then. A meeting counts as new when it
was not in that set and its stored processed_at is later than the look. The
id set is what makes the signal appear once per meeting: a Redo re-stamps
processed_at, but a meeting already seen stays seen.

This module posts no notification. The written no-banner rule stands.
"""
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from . import config

_HEAD = 65536
_PROC_RE = re.compile(rb'(?<!\\)"processed_at"\s*:\s*"([^"\\]{1,40})"')
_cache = {}


def seen_path() -> Path:
    return Path(config.PROJECT_DIR) / "panel_seen.json"


def _key_of(base: str, dest_dir=None) -> tuple:
    """(meeting key, stored processed_at or None), read from the json's head
    and cached by mtime: the menu bar asks every 2 seconds, and a transcript
    can run to megabytes."""
    j = config.meeting_file(base, ".json", dest_dir)
    try:
        st = j.stat()
    except OSError:
        return base, None
    ck = (str(j), st.st_mtime_ns)
    if ck in _cache:
        return _cache[ck]
    try:
        with open(j, "rb") as fh:
            head = fh.read(_HEAD)
    except OSError:
        return base, None
    m = _PROC_RE.search(head)
    proc = m.group(1).decode("utf-8", "replace") if m else None
    key = config.id_of(base, dest_dir) or base
    if len(_cache) > 2000:
        _cache.clear()
    _cache[ck] = (key, proc)
    return key, proc


def load_seen():
    """{"seen_at": iso, "ids": [...]}, or None before the first look."""
    try:
        d = json.loads(seen_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("seen_at"):
        return None
    return d


def mark_seen(dest_dir=None, now=None) -> dict:
    """Record a look: now, and every live meeting's key. Atomic, mode 600.
    Holds ids and a time only, never a title.

    With no library folder (a volume not mounted yet at login) the look saw
    nothing, so the last look stands and nothing is written. Writing ids []
    made the next Redo of an old meeting count as new, and cleared every
    meeting still waiting to be seen."""
    if not Path(dest_dir or config.meetings_dir()).is_dir():
        return load_seen() or {
            "seen_at": (now or datetime.now()).isoformat(timespec="seconds"), "ids": []}
    keys = sorted({_key_of(b, dest_dir)[0] for b in config.meeting_bases(dest_dir)})
    d = {"seen_at": (now or datetime.now()).isoformat(timespec="seconds"), "ids": keys}
    p = seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="panel_seen.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return d


def new_since_seen(dest_dir=None) -> list:
    """The bases of meetings that became ready since the last look, newest
    first. Empty before the first look: with no look on record there is no
    "since", and counting the whole library would mean nothing."""
    seen = load_seen()
    if seen is None:
        return []
    at = seen["seen_at"]
    ids = set(seen.get("ids") or [])
    out = []
    for b in config.meeting_bases(dest_dir):
        key, proc = _key_of(b, dest_dir)
        if key in ids or not proc or proc <= at:
            continue
        out.append((proc, b))
    return [b for _, b in sorted(out, reverse=True)]


def new_count(dest_dir=None) -> int:
    return len(new_since_seen(dest_dir))

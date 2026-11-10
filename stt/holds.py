"""Files parked in the queue: visible, but never picked up by an automatic run.

A queue is not a commitment. Some recordings are drafts, some are someone else's
copy, some you just are not ready to spend twenty minutes of transcription on.
Holding one keeps it exactly where it is and makes every AUTOMATIC sweep (folder
watch, nightly, login catch-up) skip it.

An EXPLICIT request always wins: naming the file in a run (--files / the panel's
"Run selected") processes it regardless, because that is you asking for it by
name. The hold clears itself once the file is processed, and an automatic sweep
clears it once the file is gone from every watched folder (release_missing), so
it can never leave a stale entry pinning a name forever.
"""
import fcntl
import json
import os
import threading
from pathlib import Path

from . import config

PATH = config.PROJECT_DIR / "holds.json"
_LOCK = config.PROJECT_DIR / "holds.lock"
_lock_state = threading.local()


def _apply(fn):
    try:
        cur = set(json.loads(PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        cur = set()
    out, ret = fn(cur)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(out), indent=2))
    os.replace(tmp, PATH)  # atomic: a truncated read must never silently unhold
    return ret


def _mutate(fn):
    # reentrancy guard mirrors jobs._mutate: flock is per-fd, so a nested call on
    # the holding thread would block on a lock this very process owns
    if getattr(_lock_state, "held", False):
        return _apply(fn)
    with open(_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        _lock_state.held = True
        try:
            return _apply(fn)
        finally:
            _lock_state.held = False


def items() -> set:
    try:
        return set(json.loads(PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return set()


def is_held(name: str) -> bool:
    return name in items()


def hold(name: str) -> bool:
    return _mutate(lambda cur: (cur | {name}, True))


def release(name: str) -> bool:
    return _mutate(lambda cur: (cur - {name}, name in cur))


def release_missing(dirs) -> list:
    """Release every hold whose file is in none of `dirs`, the watched
    folders. Returns the released names, sorted.

    A hold is a name, so a hold on a file binned outside the panel stayed,
    and the next recording under that name started held and every sweep
    skipped it. Nothing is released unless every folder answers: the first
    one (the primary source) must list, and any other must list or be
    missing. An unmounted drive or iCloud Drive turned off must never read
    as "every held file was deleted"."""
    cur = items()
    if not cur:
        return []
    dirs = [Path(d) for d in dirs]
    for i, d in enumerate(dirs):
        try:
            os.listdir(d)
        except FileNotFoundError:
            if i == 0:
                return []
        except OSError:
            return []
    if not dirs:
        return []

    def _gone(n):
        return not any(os.path.lexists(d / n) for d in dirs)
    if not any(_gone(n) for n in cur):
        return []

    def _f(now):
        gone = sorted(n for n in now if _gone(n))
        return now - set(gone), gone
    return _mutate(_f)


def toggle(name: str) -> bool:
    """Returns the NEW state: True = now held."""
    def _t(cur):
        if name in cur:
            return cur - {name}, False
        return cur | {name}, True
    return _mutate(_t)

"""Files parked in the queue: visible, but never picked up by an automatic run.

A queue is not a commitment. Some recordings are drafts, some are someone else's
copy, some you just are not ready to spend twenty minutes of transcription on.
Holding one keeps it exactly where it is and makes every AUTOMATIC sweep (folder
watch, nightly, login catch-up) skip it.

An EXPLICIT request always wins: naming the file in a run (--files / the panel's
"Run selected") processes it regardless, because that is you asking for it by
name. The hold clears itself once the file is processed, so it can never leave a
stale entry pinning a name forever.
"""
import fcntl
import json
import os
import threading

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


def toggle(name: str) -> bool:
    """Returns the NEW state: True = now held."""
    def _t(cur):
        if name in cur:
            return cur - {name}, False
        return cur | {name}, True
    return _mutate(_t)

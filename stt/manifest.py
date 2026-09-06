"""A tiny JSON manifest of processed files, for idempotent re-runs."""
import contextlib
import fcntl
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from . import config

_lock_state = threading.local()


def lock_path():
    # resolved per call, never bound at import: tests point config.MANIFEST_PATH
    # at a sandbox after this module is imported
    return config.MANIFEST_PATH.with_suffix(".lock")


@contextlib.contextmanager
def locked():
    """Hold the manifest lock for a whole read-modify-write.

    The batch process and the GUI server both load, mutate and save this file.
    Without one lock spanning the read AND the write, the later save silently
    discards the earlier one -- dropping a just-finished file's processed record
    or a retarget's path fix, which is how a renamed meeting gets silently
    re-transcribed. Re-entrant within one thread (flock is per-fd, so a nested
    acquire on the holding thread would block on a lock this process owns);
    another process still genuinely waits."""
    if getattr(_lock_state, "held", False):
        yield
        return
    p = lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "w")
    except OSError:
        # an unwritable lock path must never block processing outright -- the
        # unserialized behavior is exactly what shipped before this existed
        yield
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _lock_state.held = True
        try:
            yield
        finally:
            _lock_state.held = False
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


@contextlib.contextmanager
def update():
    """The read-modify-write cycle, under one lock: yields the manifest and
    saves it on a clean exit. Every mutation of the file on disk goes through
    this -- a caller that loads early, mutates late and saves is the race."""
    with locked():
        m = load()
        yield m
        save(m)


def load() -> dict:
    with locked():
        if config.MANIFEST_PATH.exists():
            try:
                m = json.loads(config.MANIFEST_PATH.read_text())
                m.setdefault("processed", {})  # a malformed file must not blank the queue
                return m
            except json.JSONDecodeError:
                pass
        return {"processed": {}}


def save(m: dict):
    # atomic: a kill mid-write must never leave a half-written manifest — a
    # truncated read would make every already-processed file look brand new
    # and get needlessly reprocessed
    with locked():
        tmp = config.MANIFEST_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(m, indent=2))
        os.replace(tmp, config.MANIFEST_PATH)


def is_processed(m: dict, key: str, mtime: float) -> bool:
    rec = m["processed"].get(key)
    if rec is None or abs(rec.get("mtime", 0) - mtime) >= 1.0:
        return False
    # self-healing: if the transcript outputs were deleted, the work no longer
    # exists — treat the file as new so it can be reprocessed
    core = [o for o in rec.get("outputs", []) if o.endswith((".txt", ".json"))]
    if core and not all(Path(o).exists() for o in core):
        return False
    return True


def retarget(old_dir, new_dir, old_base=None, new_base=None):
    """Follow a meeting folder that MOVED (rename, date re-stamp, archive) in the
    recorded output paths.

    Without this, is_processed() self-heals on the now-missing outputs and reports
    the source file as brand new — so if the original audio is still sitting in a
    watched folder (keep-original setups), the next run silently RE-TRANSCRIBES the
    meeting you just renamed or archived, resurrecting it as a duplicate.

    A rename moves the folder AND renames every file inside it (the
    <base>/<base>.* invariant), so the recorded FILENAMES have to follow too —
    retargeting only the directory would leave the paths pointing at names that
    no longer exist, which is the very failure this exists to prevent. Never
    raises: manifest hygiene must not block the move itself."""
    try:
        old_dir, new_dir = Path(old_dir), Path(new_dir)
        # one lock across the read AND the write: a concurrent mark() would
        # otherwise be discarded by this save, or this path fix by that one
        with locked():
            m = load()
            changed = False
            for rec in m["processed"].values():
                outs = rec.get("outputs") or []
                new = []
                for o in outs:
                    p = Path(o)
                    if p.parent != old_dir:
                        new.append(o)
                        continue
                    name = p.name
                    if old_base and new_base and name.startswith(old_base + "."):
                        name = new_base + name[len(old_base):]
                    new.append(str(new_dir / name))
                if new != outs:
                    rec["outputs"] = new
                    changed = True
            if changed:
                save(m)
        return changed
    except Exception:
        return False


def mark(m: dict, key: str, mtime: float, outputs: list, fp=None, size=None):
    rec = {
        "mtime": mtime,
        "outputs": [str(o) for o in outputs],
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    # the SOURCE file's content fingerprint, taken before the pipeline moved or
    # re-encoded it (stt.dupes.fingerprint). Optional and additive: records
    # written before this existed simply carry no fp, and every reader treats a
    # missing one as "no content signal, fall back to the name". It is what lets
    # the panel recognize an already-processed recording that comes back under a
    # different name -- and the only such signal for a VIDEO source, whose
    # stored meeting audio is a re-encode sharing no bytes with the original.
    if fp:
        rec["fp"] = fp
        if size:
            rec["size"] = size
    m["processed"][key] = rec
    return rec


def record(key: str, mtime: float, outputs: list, fp=None, size=None) -> dict:
    """mark() as a LOCKED read-modify-write against the file on disk, and the
    only way a running batch may record a finished file.

    The batch loads the manifest once at the start of a run and holds that
    snapshot for hours. Saving the snapshot back drops every record the GUI (or
    a second run) wrote in between. Returns the record, so the caller can keep
    its own snapshot current without saving it."""
    with update() as m:
        return mark(m, key, mtime, outputs, fp=fp, size=size)

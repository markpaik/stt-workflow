"""A tiny JSON manifest of processed files, for idempotent re-runs."""
import json
import os
from datetime import datetime
from pathlib import Path

from . import config


def load() -> dict:
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


def mark(m: dict, key: str, mtime: float, outputs: list):
    m["processed"][key] = {
        "mtime": mtime,
        "outputs": [str(o) for o in outputs],
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }

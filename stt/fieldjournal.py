"""Undo journals for one-time changes to the meeting store.

The pattern tools/amnesty.py set: each changed field is written to an
append-only journal (mode 600, inside the mode-700 config.migration_dir())
BEFORE the json it describes, and undo restores a field only where it still
holds the value the run wrote. An edit made after the run survives its undo.

Used by tools/backfill_keys.py (per-field undo) and tools/fix_dates.py
(per-meeting undo through apply_meeting_edits).
"""
import json
import os
import re
import tempfile
from pathlib import Path

from . import config

# what every run id this code makes looks like ("backfill-20261109T101500"):
# no slash and no leading dot, so an id can never walk out of the folder
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class NoSuchRun(ValueError):
    """An undo named a run with no journal: mistyped, or not a run id at all
    ('../../x'). The message is the one line a CLI prints."""

MISSING = {"$missing": True}


def is_missing(v) -> bool:
    return isinstance(v, dict) and v.get("$missing") is True


def get(d: dict, k: str):
    return d[k] if k in d else MISSING


def put(d: dict, k: str, v) -> None:
    if is_missing(v):
        d.pop(k, None)
    else:
        d[k] = v


def same(a, b) -> bool:
    return a == b or (is_missing(a) and is_missing(b))


def private_dir(p) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def atomic_json(path: Path, d: dict) -> None:
    """Replace `path` with `d` in one rename. `generated_at` is whatever `d`
    holds, so a caller that does not touch it leaves open pages alone."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Journal:
    """Append-only JSON lines, mode 600, fsynced per entry."""

    def __init__(self, path: Path):
        self.path = Path(path)
        private_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.path, 0o600)
        self.fh = os.fdopen(fd, "a")

    def write(self, entry: dict) -> None:
        self.fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def journal_path(run_id: str, journal_dir=None) -> Path:
    if not _RUN_ID.fullmatch(str(run_id or "")):
        raise NoSuchRun(f"no such run: {run_id!r} is not a run id")
    return Path(journal_dir or config.migration_dir()) / f"{run_id}.jsonl"


def entries(run_id: str, journal_dir=None) -> list:
    """The journal of one run. NoSuchRun when there is none.

    A line that is not a JSON object is skipped. A crash inside one write
    leaves a torn last line, and that entry's change was never made: the
    journal line is written before the json it describes. A torn line made
    every undo of the run fail with a traceback, and nothing was restored,
    even for the whole entries before it. tools/iso_rename.py and
    tools/retitle_all.py skip such a line the same way."""
    try:
        text = journal_path(run_id, journal_dir).read_text()
    except FileNotFoundError:
        raise NoSuchRun(f"no such run: {run_id}") from None
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def bases_by_id(store=None) -> dict:
    """{meeting_id: base} read straight from the jsons. Never rebuilds the
    id index, so a dry run writes nothing."""
    out = {}
    for b in config.meeting_bases(store):
        mid = config.id_of(b, store)
        if mid:
            out[mid] = b
    return out


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None

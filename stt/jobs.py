"""Queue of panel-submitted runs (redos, hand-picked files) that arrive while a
batch already holds the single-instance lock.

Without this, a spawned run_batch would hit the lock and exit — the click would
be silently lost. Instead the panel enqueues the request here; the finishing
batch chains straight into the next job, and the panel self-heals a broken
chain by kicking the head job whenever it sees the machine idle.

Crash-safe hand-off: a job is only REMOVED by the run_batch that has already
acquired the lock for it (--job <id>). A spawn that loses the lock race exits
without touching the queue, so the job simply waits for the next kick.
"""
import fcntl
import json
import os
import time

from . import config

PATH = config.PROJECT_DIR / "queued_jobs.json"
_LOCK = config.PROJECT_DIR / "queued_jobs.lock"

# ASCII record separator: joins/splits --files and --paths for the queued-job
# CLI round-trip (spawn_args() here -> run_batch.py's argparse). A plain comma
# silently corrupts the list if a real filename ever contains one; this
# character effectively never appears in a filename or gets typed by hand.
FIELD_SEP = "\x1e"


def join_list(items) -> str:
    return FIELD_SEP.join(items)


def split_list(s: str) -> list:
    """Parse a --files/--paths value. Prefers FIELD_SEP (what spawn_args()
    emits); falls back to a plain comma so a human typing the flag by hand
    (per this project's documented CLI usage) still works."""
    return s.split(FIELD_SEP) if FIELD_SEP in s else s.split(",")


def _mutate(fn):
    with open(_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = json.loads(PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            cur = []
        out, ret = fn(cur)
        # atomic: a kill mid-write (forced quit, the SIGKILL escalation in
        # stop_run itself) must never truncate this file — a truncated read
        # degrades silently to "queue empty", dropping every pending job
        tmp = PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, PATH)
        return ret


def add(job: dict) -> dict:
    """job: {files|paths, force, strict, verify, parallel, label}. Returns it
    with its queue id ("at") stamped — bumped if two adds land in the same
    millisecond, so cancelling one can never cancel its neighbor."""
    def _add(cur):
        at = round(time.time(), 3)
        taken = {j.get("at") for j in cur}
        while at in taken:
            at = round(at + 0.001, 3)
        return cur + [{**job, "at": at}], at
    return {**job, "at": _mutate(_add)}


def items() -> list:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def remove(at: float) -> bool:
    def _rm(cur):
        kept = [j for j in cur if j.get("at") != at]
        return kept, len(kept) != len(cur)
    return _mutate(_rm)


def clear() -> int:
    """Drop every queued run (Stop processing). Returns how many were dropped."""
    return _mutate(lambda cur: ([], len(cur)))


def spawn_args(job: dict) -> list:
    """The full command that runs this job (caffeinate keeps the Mac awake;
    run.sh resolves the venv). Mirrors what the panel builds for a direct run."""
    args = ["caffeinate", "-i", "-s", str(config.PROJECT_DIR / "run.sh"),
            "batch", "--ignore-pause", "--job", str(job["at"])]
    if job.get("files"):
        args += ["--files", join_list(job["files"])]
    if job.get("paths"):
        args += ["--paths", join_list(job["paths"])]
    if job.get("force"):
        args += ["--force"]
    if job.get("strict"):
        args += ["--strict"]
    if job.get("verify"):
        args += ["--verify"]
    if job.get("onetime"):
        args += ["--one-time-speakers"]
    if int(job.get("parallel", 1)) == 2:
        args += ["--parallel", "2"]
    return args

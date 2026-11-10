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
import sys
import threading
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
    items = list(items)
    s = FIELD_SEP.join(items)
    # one item has no FIELD_SEP, so split_list would split it on its commas:
    # 'Budget, Q3.m4a' alone became 'Budget' and ' Q3.m4a', which match no
    # file. A FIELD_SEP at the end marks it as one name.
    if len(items) == 1 and "," in s:
        s += FIELD_SEP
    return s


def split_list(s: str) -> list:
    """Parse a --files/--paths value. Prefers FIELD_SEP (what spawn_args()
    emits; one at the end marks a single name); falls back to a plain comma
    so a human typing the flag by hand (per this project's documented CLI
    usage) still works. An empty value is no names: join_list([]) gave
    '', and ''.split(',') is [''], one empty name."""
    if not s:
        return []
    if FIELD_SEP not in s:
        return s.split(",")
    parts = s.split(FIELD_SEP)
    return parts[:-1] if parts[-1] == "" else parts


_lock_state = threading.local()


def _load():
    """(jobs, ok): the queued jobs on disk, and False when the file is there
    but is not a JSON list (a torn write, outside damage, a hand edit). Only
    the job objects are kept: a '{}' file made add() raise TypeError, so the
    Run click failed instead of queueing."""
    try:
        raw = json.loads(PATH.read_text())
    except FileNotFoundError:
        return [], True
    except ValueError:  # bad JSON, or bytes that are not text
        return [], False
    if not isinstance(raw, list):
        return [], False
    return [j for j in raw if isinstance(j, dict)], True


def _keep_aside():
    """Move a damaged queue file to queued_jobs.json.bad before the next
    write replaces it, so what it held can still be read back by hand."""
    bad = PATH.with_suffix(".json.bad")
    try:
        os.replace(PATH, bad)
        print(f"warning: {PATH.name} was not a list of jobs; kept it as "
              f"{bad.name} and started an empty queue", file=sys.stderr, flush=True)
    except OSError:
        pass


def _apply(fn):
    cur, ok = _load()
    if not ok:
        _keep_aside()
    out, ret = fn(cur)
    # atomic: a kill mid-write (forced quit, the SIGKILL escalation in
    # stop_run itself) must never truncate this file — a truncated read
    # degrades silently to "queue empty", dropping every pending job
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, PATH)
    return ret


def _mutate(fn):
    # Reentrant per THREAD: run_batch's SIGTERM handler re-queues via add() while
    # the main thread may already hold this flock; flock treats the handler's new
    # fd as a different holder, so it would self-deadlock. When this thread holds
    # it, do the read-modify-write without re-locking (sole writer in that window).
    if getattr(_lock_state, "held", False):
        return _apply(fn)
    with open(_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        _lock_state.held = True
        try:
            return _apply(fn)
        finally:
            _lock_state.held = False


def add(job: dict) -> dict:
    """job: {files|paths, force, strict, verify, parallel, label}, and for a
    re-queued run its CLI-only options (no_diarize, keep_original,
    ignore_battery, only, speakers). Returns it
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
    return _load()[0]


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
    args = ["caffeinate", "-i", "-s", str(config.CODE_DIR / "run.sh"),
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
    # CLI-only options a re-queued run carried (run_batch.job_spec_from_args)
    for key, flag in (("no_diarize", "--no-diarize"), ("keep_original", "--keep-original"),
                      ("ignore_battery", "--ignore-battery")):
        if job.get(key):
            args += [flag]
    for key, flag in (("only", "--only"), ("speakers", "--speakers"),
                      ("source", "--source"), ("dest", "--dest")):
        if job.get(key):
            # one "--flag=value" token: a value that starts with "-" is
            # still read as the value, never as another option
            args += [f"{flag}={job[key]}"]
    return args

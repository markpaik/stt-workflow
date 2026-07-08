"""Pause/resume + stop controls shared by the batch, menu bar, and control panel.

Pause is a flag file: automatic triggers (nightly, WatchPaths, RunAtLoad) see it
and exit immediately; manual runs can override.

Stop targets PROCESS GROUPS, not single pids. A --parallel run spawns worker
processes whose command lines don't mention run_batch.py (multiprocessing spawn),
so a pid-by-name kill orphans them — each holding several GB of models. All
run_batch processes share their launcher's process group (caffeinate/launchd),
and status.json records that pgid, so we can find and signal every member even
after the parent dies. stop_run() verifies termination and escalates to SIGKILL.
"""
import os
import signal
import subprocess
import time
from datetime import datetime

from . import config, status

PAUSE_FLAG = config.PROJECT_DIR / "paused.flag"
STOP_FLAG = config.PROJECT_DIR / "stopping.flag"
STOP_WINDOW = 25.0  # seconds a "Stop" suppresses re-queue and self-heal kicks


def is_paused() -> bool:
    return PAUSE_FLAG.exists()


def pause():
    PAUSE_FLAG.write_text(datetime.now().isoformat(timespec="seconds"))


def resume():
    PAUSE_FLAG.unlink(missing_ok=True)


def mark_stopping():
    """Record that a Stop is intentional. A killed --job run re-queues itself
    from its SIGTERM handler so a crash/sleep never loses work; on a user Stop
    that same re-queue would make the panel's self-heal respawn the very run
    just stopped. This flag lets both sides tell the two cases apart."""
    try:
        STOP_FLAG.write_text(datetime.now().isoformat(timespec="seconds"))
    except OSError:
        pass


def clear_stopping():
    STOP_FLAG.unlink(missing_ok=True)


def stopping_recently(within: float = STOP_WINDOW) -> bool:
    """True if a Stop was requested in the last `within` seconds."""
    try:
        return (time.time() - STOP_FLAG.stat().st_mtime) < within
    except OSError:
        return False


def _pgrep(args):
    try:
        out = subprocess.run(["pgrep"] + args, capture_output=True, text=True)
        return [int(p) for p in out.stdout.split()]
    except Exception:
        return []


def _parent_pids():
    """run_batch.py parents (found by command line)."""
    return _pgrep(["-f", "run_batch.py"])


def _group_is_ours(pgid) -> bool:
    """Guard against pgid reuse: only claim a recorded group if a member's
    command line clearly belongs to a BATCH RUN — the run_batch parent or a
    multiprocessing spawn worker.

    Deliberately NOT "our venv python / PROJECT_DIR appears in the command
    line": that also matches the menu-bar app, the control panel, and any
    helper launched from this repo's venv, whose paths all contain
    PROJECT_DIR. When a finished run's pgid gets recycled onto one of those,
    the loose check reported the batch as still running forever, pinning the
    panel on "processing / Starting…" with nothing actually running."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-g", str(pgid)],
                             capture_output=True, text=True).stdout
    except Exception:
        return False
    return "run_batch.py" in out or "multiprocessing" in out


def batch_groups():
    """Process-group ids of every live batch run: groups of visible parents,
    plus the pgid recorded in status.json (catches orphaned workers whose
    parent is gone)."""
    groups = set()
    for pid in _parent_pids():
        try:
            groups.add(os.getpgid(pid))
        except (ProcessLookupError, PermissionError):
            pass
    pgid = status.read().get("pgid")
    if pgid and _pgrep(["-g", str(pgid)]) and _group_is_ours(pgid):
        groups.add(int(pgid))
    return groups


_snap = {"t": 0.0, "pids": [], "mem_mb": 0}


def snapshot(max_age: float = 1.5) -> dict:
    """One cached pass over the process table: {"pids": [...], "mem_mb": int}.
    The panel (2s) and menu bar (2s) both poll; without this cache they spawn
    ~8 pgrep/ps subprocesses every 2 seconds between them."""
    now = time.monotonic()
    if now - _snap["t"] < max_age:
        return {"pids": _snap["pids"], "mem_mb": _snap["mem_mb"]}
    pids = set(_parent_pids())
    for g in batch_groups():
        pids.update(_pgrep(["-g", str(g)]))
    mem = 0
    if pids:
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", ",".join(map(str, sorted(pids)))],
                                 capture_output=True, text=True)
            mem = round(sum(int(x) for x in out.stdout.split()) / 1024)
        except Exception:
            pass
    _snap.update(t=now, pids=sorted(pids), mem_mb=mem)
    return {"pids": _snap["pids"], "mem_mb": _snap["mem_mb"]}


def batch_pids():
    """Every live pid belonging to a batch run — parents AND workers.
    (Uncached — used by stop_run, which must see fresh state.)"""
    pids = set(_parent_pids())
    for g in batch_groups():
        pids.update(_pgrep(["-g", str(g)]))
    return sorted(pids)


def stop_run(timeout: float = 8.0) -> dict:
    """Stop every batch process group; verify; escalate to SIGKILL.
    Also clears panel-queued runs — "Stop processing" must not be followed by
    a queued job auto-starting seconds later.
    Returns {"stopped": bool, "forced": bool, "survivors": [pid...],
    "cleared_jobs": n}."""
    from . import jobs
    mark_stopping()  # before anything: a job's SIGTERM handler checks this to
    # skip re-queuing, and the panel's self-heal checks it to skip re-kicking
    try:
        cleared = jobs.clear()
    except Exception:
        # the kill switch must work even if the queue file is unwritable —
        # losing the ability to clear queued jobs is far better than losing
        # the ability to stop a runaway batch
        cleared = 0
    groups = batch_groups()
    if not groups:
        return {"stopped": False, "forced": False, "survivors": [],
                "cleared_jobs": cleared}
    for g in groups:
        try:
            os.killpg(g, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # monotonic: a wall-clock jump (NTP correction mid-stop) must neither
    # stretch nor cut short the grace window before SIGKILL escalation
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and batch_pids():
        time.sleep(0.3)
    forced = False
    if batch_pids():  # graceful didn't finish the job — force it
        forced = True
        for g in batch_groups():
            try:
                os.killpg(g, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)
    # sweep AGAIN after death: a panel-spawned --job batch re-queues its
    # claimed job from its SIGTERM handler (so a crash or shutdown never
    # loses it) — and that re-add lands in the queue cleared above, since
    # the handler only runs once our SIGTERM arrives. Without this second
    # clear, the panel's idle self-heal kick would restart the very run the
    # user just stopped, seconds later.
    try:
        jobs.clear()
    except Exception:
        pass
    status.end_run()
    return {"stopped": True, "forced": forced, "survivors": batch_pids(),
            "cleared_jobs": cleared}

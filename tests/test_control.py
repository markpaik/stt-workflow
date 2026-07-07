"""control: pause states, group discovery, and the stop path that used to
orphan multi-GB workers — proven against REAL processes."""
import os
import subprocess
import sys
import time

from stt import config, control, status


def test_pause_resume_states(sandbox):
    assert not control.is_paused()
    control.pause()
    assert control.is_paused()
    control.pause()  # idempotent
    assert control.is_paused()
    control.resume()
    assert not control.is_paused()
    control.resume()  # idempotent from resumed state
    assert not control.is_paused()


def test_stop_with_nothing_running(sandbox):
    res = control.stop_run(timeout=0.5)
    assert res == {"stopped": False, "forced": False, "survivors": [],
                   "cleared_jobs": 0}


def _spawn_fake_batch(sandbox):
    """A process group like a real run: leader + a worker child whose command
    line does NOT mention run_batch.py (the exact shape that used to leak).
    PROJECT_DIR in argv satisfies the ownership guard."""
    code = ("import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, str(config.PROJECT_DIR)],
                         start_new_session=True)
    time.sleep(1.0)
    return p


def test_group_stop_kills_orphanable_workers(sandbox):
    p = _spawn_fake_batch(sandbox)
    pgid = os.getpgid(p.pid)
    try:
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        assert pgid in control.batch_groups()
        assert len(control.batch_pids()) >= 2  # leader + worker

        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == []
        assert subprocess.run(["pgrep", "-g", str(pgid)],
                              capture_output=True, text=True).stdout == ""
        assert not status.read()["running"]
    finally:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            # macOS raises EPERM (not ESRCH) for an already-reaped group —
            # the same quirk stop_run's except clauses guard against
            pass


def test_stop_kills_even_when_job_queue_clear_fails(sandbox, monkeypatch):
    """A broken queued_jobs.json write (disk full, permissions) must never
    disable the kill switch — the process-group stop has to proceed regardless."""
    from stt import jobs

    def _boom():
        raise OSError("disk full")
    monkeypatch.setattr(jobs, "clear", _boom)

    p = _spawn_fake_batch(sandbox)
    pgid = os.getpgid(p.pid)
    try:
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)

        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == [] and res["cleared_jobs"] == 0
        assert subprocess.run(["pgrep", "-g", str(pgid)],
                              capture_output=True, text=True).stdout == ""
    finally:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def test_stale_pgid_of_foreign_group_ignored(sandbox):
    """A recycled pgid pointing at processes that are NOT ours must never be
    claimed (stop would kill innocent programs)."""
    p = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    pgid = os.getpgid(p.pid)
    try:
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        assert pgid not in control.batch_groups()
    finally:
        p.kill()


def test_snapshot_caches(sandbox, monkeypatch):
    calls = {"n": 0}
    real = control._parent_pids

    def counting():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(control, "_parent_pids", counting)
    control.snapshot()  # cold: snapshot + batch_groups each scan once
    cold = calls["n"]
    control.snapshot()
    control.snapshot()
    assert calls["n"] == cold  # subsequent calls served from cache within TTL

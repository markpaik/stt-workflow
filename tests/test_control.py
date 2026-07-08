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
    """A process group like a real run: a leader whose command line marks it as
    run_batch (the ownership signal, exactly like the real parent) plus a worker
    child whose OWN command line does not mention it (the orphanable shape). The
    group is claimed via the leader, and stop takes the whole group down."""
    code = ("import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, "run_batch.py"],
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


def test_stop_sweeps_the_job_a_dying_batch_requeues(sandbox):
    """A panel-spawned --job batch re-queues its claimed job from its SIGTERM
    handler (crash-safety). But stop_run() clears the queue BEFORE sending
    SIGTERM — so that re-add used to land in the freshly-emptied queue and the
    panel's idle self-heal restarted the very run the user just stopped.
    stop_run must sweep again after the group is verified dead."""
    from stt import jobs
    jobs.add({"label": "genuinely queued", "files": ["a.m4a"]})

    qfile = jobs.PATH
    # a real process group whose SIGTERM handler re-queues a job, exactly
    # like run_batch._terminate does (written directly: the subprocess can't
    # see this test's monkeypatched sandbox paths)
    code = (
        "import json,os,signal,sys,time\n"
        "def t(s,f):\n"
        "    json.dump([{'label':'resurrected','files':['a.m4a'],'at':1.0}],"
        "open(sys.argv[1],'w'))\n"
        "    os._exit(130)\n"
        "signal.signal(signal.SIGTERM,t)\n"
        "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(qfile),
                          "run_batch.py"], start_new_session=True)
    pgid = os.getpgid(p.pid)
    time.sleep(0.8)
    try:
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        assert pgid in control.batch_groups()

        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == []
        assert res["cleared_jobs"] == 1  # the genuinely queued run
        assert jobs.items() == []  # the handler's re-add was swept, not revived
    finally:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def test_stop_deadline_uses_monotonic_not_wallclock(sandbox, monkeypatch):
    """The SIGKILL-escalation grace window must be immune to wall-clock jumps
    (same class as icloud.materialize's fix). Proven by making time.time()
    raise: if stop_run still depends on it for the deadline, this fails
    loudly instead of silently passing."""
    def _boom():
        raise AssertionError("stop_run() must not call time.time() for its deadline")
    monkeypatch.setattr(control.time, "time", _boom)

    p = _spawn_fake_batch(sandbox)
    pgid = os.getpgid(p.pid)
    try:
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == []
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


def test_group_with_only_project_dir_in_cmdline_is_not_ours(sandbox):
    """The menu-bar app and control panel run from this repo's venv, so their
    command lines contain PROJECT_DIR. A finished run's pgid recycled onto one
    of them must NOT read as a live batch — otherwise the panel pins on
    'processing / Starting…' with nothing actually running. Only run_batch and
    multiprocessing-spawn members count as a batch."""
    # a real process whose ONLY 'ours' signal is PROJECT_DIR in its argv,
    # exactly like `python .../gui/menubar.py`
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)",
                          str(config.PROJECT_DIR / "gui" / "menubar.py")],
                         start_new_session=True)
    pgid = os.getpgid(p.pid)
    try:
        assert control._group_is_ours(pgid) is False
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        assert pgid not in control.batch_groups()  # not claimed as a run
    finally:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def test_stopping_flag_marks_intentional_stops(sandbox):
    """mark/clear/stopping_recently is the signal that a Stop was deliberate."""
    assert not control.stopping_recently()
    control.mark_stopping()
    assert control.stopping_recently()
    assert not control.stopping_recently(within=-1)  # window is respected
    control.clear_stopping()
    assert not control.stopping_recently()


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

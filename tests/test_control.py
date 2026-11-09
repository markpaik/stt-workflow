"""control: pause states, group discovery, and the stop path that used to
orphan multi-GB workers — proven against REAL processes."""
import os
import subprocess
import sys
import time

from stt import config, control, status

# the real finder, kept before conftest's sandbox fixture stubs it per test
_REAL_PARENT_PIDS = control._parent_pids


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


def test_recorder_group_is_never_claimed_as_a_batch(sandbox):
    """G3: the meeting recorder runs as `caffeinate ... native/stt-recorder` in
    its own session. The batch Stop must never target it — _group_is_ours claims
    only run_batch / multiprocessing members, so a recorder-shaped command line
    is not a batch and Stop cannot kill a live recording."""
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)",
                          str(config.PROJECT_DIR / "native" / "stt-recorder")],
                         start_new_session=True)
    pgid = os.getpgid(p.pid)
    try:
        assert control._group_is_ours(pgid) is False
        d = status.read()
        d.update(running=True, pgid=pgid)
        status._write(d)
        assert pgid not in control.batch_groups()  # Stop would never reach it
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


def test_a_blank_started_marker_is_not_a_running_relabel(sandbox):
    """W33: a blank `started` is written when the process-start check fails.
    Treating it exactly like a marker with NO timestamp field fell back to
    trusting any live process at that pid, so a failed check plus a hard kill
    plus pid reuse read as a live relabel."""
    import json

    p = control.relabel_marker_path()
    p.write_text(json.dumps({"pid": os.getpid(), "started": ""}))
    assert control.relabel_running() is False, \
        "an unknown identity must never override the recycled-pid guard"

    # a LEGACY marker, written before the field existed, still gets the benefit
    # of the doubt: absent is not the same as blank
    p.write_text(json.dumps({"pid": os.getpid()}))
    assert control.relabel_running() is True

    # and a marker whose start time actually matches is still live
    p.write_text(json.dumps({"pid": os.getpid(),
                             "started": control._proc_started(os.getpid())}))
    assert control.relabel_running() is True


# ---------- which batch is THIS home's: the batch.lock holder ----------

def _decoy(*argv):
    """A live process whose command line holds `argv` (it sleeps 30 s)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *argv])


def _lock_names(sandbox, pid, age=0.0):
    lk = sandbox / "batch.lock"
    lk.write_text(str(pid))
    if age:
        t = time.time() - age
        os.utime(lk, (t, t))


def test_a_command_line_that_mentions_run_batch_is_not_a_batch(sandbox, monkeypatch):
    """pgrep -f run_batch.py matched any process on the machine: a shell
    running a command that names the file, an editor, another home's batch.
    The panel then read "running" with no batch of its own, and Stop
    signalled those groups."""
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    p = _decoy("run_batch.py", "--files", "x.m4a")
    try:
        time.sleep(0.2)
        assert control._parent_pids() == []
        assert control.batch_groups() == set()
        assert control.snapshot(max_age=0)["pids"] == []
    finally:
        p.kill()
        p.wait()


def test_the_process_named_in_batch_lock_is_the_batch(sandbox, monkeypatch):
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    p = _decoy("run_batch.py")
    try:
        time.sleep(0.2)
        _lock_names(sandbox, p.pid)
        assert control._parent_pids() == [p.pid]
    finally:
        p.kill()
        p.wait()
    assert control._parent_pids() == []      # the holder is gone


def test_a_lock_pid_that_is_not_run_batch_is_ignored(sandbox, monkeypatch):
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    p = _decoy("something_else.py")
    try:
        time.sleep(0.2)
        _lock_names(sandbox, p.pid)
        assert control._parent_pids() == []
    finally:
        p.kill()
        p.wait()


def test_a_recycled_lock_pid_is_ignored(sandbox, monkeypatch):
    """A batch that died leaves its pid in batch.lock. A later process that
    gets the same pid started AFTER the lock was written: not the batch."""
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    p = _decoy("run_batch.py")
    try:
        time.sleep(0.2)
        _lock_names(sandbox, p.pid, age=600)
        assert control._parent_pids() == []
    finally:
        p.kill()
        p.wait()


def test_elapsed_time_parses_every_ps_form():
    assert control._etime_secs("05:07") == 307
    assert control._etime_secs("02:05:07") == 2 * 3600 + 307
    assert control._etime_secs("3-02:05:07") == 3 * 86400 + 2 * 3600 + 307


def test_stop_leaves_the_script_that_started_the_batch(sandbox, monkeypatch):
    """A script without job control that starts the batch in the background
    shares its process group with the batch. stop_run sent SIGTERM to that
    whole group, so Stop ended the script too. In a group the batch does
    not lead, only the batch's own processes are signalled."""
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    pidfile = sandbox / "batch.pid"
    # the script leads its own group; the fake batch runs in it as a
    # background child, as `python run_batch.py &` in a script does
    script = (f'{sys.executable} -c "import time;time.sleep(60)" run_batch.py & '
              f'echo $! > "{pidfile}"; wait; sleep 30')
    sh = subprocess.Popen(["/bin/bash", "-c", script], start_new_session=True)
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        batch = int(pidfile.read_text())
        time.sleep(0.3)
        assert os.getpgid(batch) == sh.pid != batch
        _lock_names(sandbox, batch)
        d = status.read()
        d.update(running=True, pgid=sh.pid)
        status._write(d)
        assert control.batch_pids() == [batch]       # the script is not the batch
        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == [] and not res["forced"]
        time.sleep(0.2)
        assert sh.poll() is None                     # the script lives on
        assert subprocess.run(["/bin/ps", "-p", str(batch)],
                              capture_output=True).returncode != 0
    finally:
        try:
            os.killpg(sh.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        sh.wait()


def test_stop_still_takes_a_whole_group_the_batch_leads(sandbox, monkeypatch):
    """The panel, launchd and a terminal start the batch as its group's
    leader (caffeinate execs its utility in its own pid). That group goes
    whole, workers included."""
    monkeypatch.setattr(control, "_parent_pids", _REAL_PARENT_PIDS)
    p = _spawn_fake_batch(sandbox)
    pgid = os.getpgid(p.pid)
    try:
        assert pgid == p.pid
        _lock_names(sandbox, p.pid)
        assert len(control.batch_pids()) >= 2       # the leader and its worker
        res = control.stop_run(timeout=5)
        assert res["stopped"] and res["survivors"] == []
        assert subprocess.run(["pgrep", "-g", str(pgid)],
                              capture_output=True, text=True).stdout == ""
    finally:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            pass

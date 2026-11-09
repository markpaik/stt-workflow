"""C5: run_batch's SIGTERM handler calls status.end_run() and jobs.add() while
the main thread may already hold those flocks. flock treats the handler's fresh
fd as a different holder, so an unguarded re-entry self-deadlocks until the 8s
SIGKILL escalation. The per-thread reentrancy guard must let the nested write
through. Each nested call runs in a worker thread joined with a timeout, so a
regression fails cleanly instead of hanging the whole suite."""
import threading


def test_status_lock_is_reentrant_within_a_thread(sandbox):
    from stt import status
    status.start_run(["a.m4a"])
    result = {}

    def outer_then_nested():
        with status._lock():          # the thread already holds the status lock
            status.end_run()          # ...and re-enters via end_run's own _lock
        result["ran"] = True

    t = threading.Thread(target=outer_then_nested)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "nested locked write deadlocked (reentrancy guard missing)"
    assert result.get("ran") and status.read().get("running") is False


def test_jobs_mutate_is_reentrant_within_a_thread(sandbox):
    from stt import jobs
    result = {}

    def body():
        def outer(cur):
            jobs.add({"files": ["y"], "paths": []})   # nested add re-enters _mutate
            return cur, None
        jobs._mutate(outer)
        result["ran"] = True

    t = threading.Thread(target=body)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "nested jobs mutate deadlocked (reentrancy guard missing)"
    assert result.get("ran")


_DRIVER = r'''
import os, subprocess, sys, time
from pathlib import Path
import run_batch

home = Path(os.environ["STT_HOME"])

def fake_process_one(src_str, dest_str, opts):
    kid = subprocess.Popen(["/bin/sleep", "60"])       # an ffmpeg stand-in
    (home / "kid.pid").write_text(str(kid.pid))
    (home / "ready").write_text("1")
    time.sleep(60)

run_batch.process_one = fake_process_one
run_batch.battery_ok = lambda: True
sys.argv = ["run_batch.py", "--no-diarize", "--ignore-battery", "--ignore-pause",
            "--source", str(home / "source"), "--dest", str(home / "meetings")]
run_batch.main()
'''


def test_a_stop_never_kills_the_shell_that_started_the_batch(tmp_path):
    """_terminate called os.killpg(os.getpgid(0)). A batch started as a
    background job of a script shares the script's process group, so
    `kill -TERM <batch>` ended the script too. The batch still takes its
    own children down."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    (home / "source").mkdir(parents=True)
    (home / "meetings").mkdir()
    (home / "source" / "a.m4a").write_bytes(b"\x00" * 64)
    (tmp_path / "driver.py").write_text(_DRIVER)
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update({"STT_HOME": str(home), "STT_VOICEMEMOS_DIR": str(tmp_path / "vm"),
                "STT_VOICEMEMOS_SYNC": "0", "PYTHONPATH": str(repo)})
    script = (f'"{sys.executable}" "{tmp_path / "driver.py"}" & PID=$!\n'
              f'for i in $(seq 1 300); do [ -f "{home / "ready"}" ] && break; sleep 0.1; done\n'
              'kill -TERM $PID\nwait $PID\necho "shell alive, batch rc=$?"\n')
    # its own session, so the batch shares the SCRIPT's group, never pytest's
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       env=env, cwd=str(repo), timeout=90, start_new_session=True)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "shell alive, batch rc=130" in r.stdout
    kid = int((home / "kid.pid").read_text())
    for _ in range(50):
        try:
            os.kill(kid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(kid, 9)
        raise AssertionError("the batch's child outlived the Stop")

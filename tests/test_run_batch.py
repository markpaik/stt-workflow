"""run_batch.py: the queued-job spec used to re-queue an interrupted --job run
(see _terminate() in main()) so a Stop mid-file can never lose a Redo."""
import types

import run_batch


def _args(**over):
    base = dict(files=None, paths=None, force=False, strict=False,
                verify=False, parallel=1)
    base.update(over)
    return types.SimpleNamespace(**base)


def test_intentional_stop_does_not_requeue_but_interrupt_does(sandbox):
    """A killed --job run re-queues itself so a crash or sleep never loses the
    work. But when the user hit Stop (control marks it), re-queuing would make
    the panel's self-heal respawn the very run they stopped ("Starting… ↔
    Stopped" flicker). The re-queue must honor that intent."""
    from stt import control, jobs
    job = {"label": "redo", "files": ["a.m4a"], "at": 1.0}

    control.mark_stopping()
    assert run_batch._requeue_if_unintended(job) is False
    assert jobs.items() == []            # intentional Stop: stays gone

    control.clear_stopping()
    assert run_batch._requeue_if_unintended(job) is True
    assert len(jobs.items()) == 1        # crash/sleep: preserved for the next kick


def test_job_spec_from_args_files_and_paths():
    spec = run_batch.job_spec_from_args(_args(files="A.m4a,B.m4a"), [])
    assert spec["files"] == ["A.m4a", "B.m4a"]
    assert spec["paths"] == []

    spec = run_batch.job_spec_from_args(_args(paths="/x/C.m4a"), [])
    assert spec["paths"] == ["/x/C.m4a"] and spec["files"] == []


def test_job_spec_from_args_carries_flags():
    spec = run_batch.job_spec_from_args(
        _args(force=True, strict=True, verify=True, parallel=2), [])
    assert spec["force"] and spec["strict"] and spec["verify"]
    assert spec["parallel"] == 2


def test_job_spec_from_args_label_summarizes_todo():
    import pathlib
    todo = [pathlib.Path("One.m4a"), pathlib.Path("Two.m4a"), pathlib.Path("Three.m4a")]
    spec = run_batch.job_spec_from_args(_args(), todo)
    assert spec["label"] == "One.m4a, Two.m4a +1 more"

    spec = run_batch.job_spec_from_args(_args(), todo[:1])
    assert spec["label"] == "One.m4a"

    # nothing left to summarize (e.g. everything already processed) still
    # yields a sane, non-empty label rather than ""
    spec = run_batch.job_spec_from_args(_args(), [])
    assert spec["label"] == "resumed run"


def test_job_spec_round_trips_through_the_real_queue(sandbox):
    """The spec this function builds must be exactly what jobs.add()/spawn_args()
    expect — round-trip it through the real queue module, not a mock."""
    from stt import jobs
    spec = run_batch.job_spec_from_args(_args(paths="/x/A.m4a", verify=True), [])
    added = jobs.add(spec)
    assert jobs.items() == [added]
    args = jobs.spawn_args(added)
    assert "--paths" in args and "/x/A.m4a" in args and "--verify" in args


def test_spawn_chained_job_logs_before_spawning(sandbox, monkeypatch):
    """A crashed chained job used to leave zero trace (stdout/stderr to
    DEVNULL). It must now be logged to the same spawned.log the GUI uses,
    written BEFORE the subprocess starts (so even an instant crash is
    recorded)."""
    from stt import config, jobs
    captured = {}

    def fake_popen(args, **kw):
        captured["args"] = args
        captured["stdout"] = kw.get("stdout")
        captured["stderr"] = kw.get("stderr")
        # by the time Popen is called, the log line must already be on disk
        captured["log_at_spawn_time"] = (config.PROJECT_DIR / "logs" / "spawned.log").read_text()
        class _P:
            pid = 4242
        return _P()

    monkeypatch.setattr(run_batch.subprocess, "Popen", fake_popen)

    job = jobs.add({"paths": ["/x/A.m4a"], "verify": True, "label": "A.m4a"})
    run_batch.spawn_chained_job(job)

    assert "--paths" in captured["args"] and "/x/A.m4a" in captured["args"]
    assert captured["stdout"] is not None and captured["stdout"] is captured["stderr"]
    assert captured["stdout"] is not run_batch.subprocess.DEVNULL
    assert "/x/A.m4a" in captured["log_at_spawn_time"]

    log_file = config.PROJECT_DIR / "logs" / "spawned.log"
    assert "/x/A.m4a" in log_file.read_text()


def test_spawn_chained_job_survives_a_real_immediate_crash(sandbox):
    """End-to-end with a REAL subprocess that exits immediately (simulating a
    bad venv / bad stt.env) — the failure must be visible in spawned.log,
    not silently swallowed by DEVNULL."""
    import time
    from stt import config, jobs

    job = jobs.add({"paths": ["/x/A.m4a"], "label": "A.m4a"})
    real_args = jobs.spawn_args(job)
    # swap in a command that fails instantly, keeping spawn_chained_job's own
    # logging plumbing exactly as production code exercises it
    broken_args = ["/bin/sh", "-c", "echo 'simulated crash: bad stt.env' >&2; exit 1"]
    import run_batch as rb
    orig_spawn_args = jobs.spawn_args
    jobs.spawn_args = lambda j: broken_args
    try:
        rb.spawn_chained_job(job)
    finally:
        jobs.spawn_args = orig_spawn_args

    for _ in range(50):
        text = (config.PROJECT_DIR / "logs" / "spawned.log").read_text()
        if "simulated crash" in text:
            break
        time.sleep(0.05)
    else:
        text = (config.PROJECT_DIR / "logs" / "spawned.log").read_text()
    assert "simulated crash: bad stt.env" in text


def test_auto_summarize_only_new_meetings(sandbox, monkeypatch):
    """End-of-run auto-summary: drafts for meetings without an ai_summary,
    skips ones that already have one, and never leaves a stale 'summarizing'
    entry in the active status display."""
    import json

    from conftest import mfile
    from stt import status, summarize

    mfile("Fresh Mtg", ".json").write_text(json.dumps(
        {"source_file": "Fresh Mtg.m4a", "segments": [], "speakers": [], "words": []}))
    mfile("Done Mtg", ".json").write_text(json.dumps(
        {"source_file": "Done Mtg.m4a", "ai_summary": "already have one",
         "segments": [], "speakers": [], "words": []}))

    calls = []
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", lambda b: calls.append(b) or {})

    n = run_batch.auto_summarize(["Fresh Mtg.m4a", "Done Mtg.m4a"])
    assert n == 1 and calls == ["Fresh Mtg"]
    assert status.read().get("active", {}) == {}  # summarizing stage cleared


def test_auto_summarize_skips_cleanly_without_llm(sandbox, monkeypatch):
    from stt import summarize
    monkeypatch.setattr(summarize, "available", lambda: False)
    called = []
    monkeypatch.setattr(summarize, "suggest_title", lambda b: called.append(b))
    assert run_batch.auto_summarize(["X.m4a"]) == 0
    assert called == []


def test_auto_summarize_one_failure_never_stops_the_rest(sandbox, monkeypatch):
    import json

    from conftest import mfile
    from stt import status, summarize

    for b in ("A Mtg", "B Mtg"):
        mfile(b, ".json").write_text(json.dumps(
            {"source_file": f"{b}.m4a", "segments": [], "speakers": [], "words": []}))
    calls = []

    def boom_then_ok(base):
        calls.append(base)
        if base == "A Mtg":
            raise RuntimeError("LLM runner failed")
        return {}
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", boom_then_ok)

    n = run_batch.auto_summarize(["A Mtg.m4a", "B Mtg.m4a"])
    assert n == 1 and calls == ["A Mtg", "B Mtg"]
    assert status.read().get("active", {}) == {}  # cleared even after the failure


def test_main_entrypoint_paths_flow_regression(tmp_path):
    """Run the REAL entrypoint the panel's Redo spawns (fresh interpreter, not
    an import), with --paths + --dry-run: parses the paths list and exits 0.
    Regression for a shadowing bug: local `from stt import jobs` bindings
    inside main() made module-level `jobs` unbound at the --paths parse, so
    every panel-queued Redo crashed on spawn and sat in the queue forever —
    while all unit tests (which never drive main()) stayed green."""
    import subprocess
    import sys as _sys
    from pathlib import Path as _P

    audio = tmp_path / "Redo Me 07012026.m4a"
    audio.write_bytes(b"\x00" * 64)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**__import__("os").environ,
           # STT_PROJECT_DIR redirects the single-instance lock/manifest into
           # tmp so the subprocess never touches the real repo's batch.lock —
           # the "already running" branch can therefore never fire even if a
           # real batch holds the repo lock, so this test can't flake into a skip.
           "STT_PROJECT_DIR": str(proj),
           "STT_ICLOUD_DIR": str(tmp_path / "src"),
           "STT_MEETINGS_DIR": str(tmp_path / "dst"),
           "PYTHONPATH": str(_P(run_batch.__file__).parent)}
    (tmp_path / "src").mkdir()
    r = subprocess.run(
        [_sys.executable, run_batch.__file__, "--dry-run",
         "--paths", str(audio), "--force"],
        capture_output=True, text=True, timeout=60, env=env)
    assert "UnboundLocalError" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stderr
    assert "already running" not in r.stdout, r.stdout
    assert "Redo Me 07012026.m4a" in r.stdout  # the path actually parsed
    # hermeticity: the lock lived in tmp (proof STT_PROJECT_DIR was honored),
    # never in the real repo — before the fix the subprocess ignored the env
    # var and this file would sit under the repo instead.
    assert (proj / "batch.lock").exists()


def test_main_has_no_shadowed_module_imports():
    """The lock-independent half of the regression: any `from stt import X`
    INSIDE main() makes X local to the whole function, unbinding the
    module-level name for every line above it. Assert none of the shared
    modules appear in main()'s locals."""
    shadowed = set(run_batch.main.__code__.co_varnames) & {
        "config", "control", "icloud", "jobs", "manifest", "rates", "status"}
    assert not shadowed, f"module names shadowed as locals in main(): {shadowed}"


def test_warns_when_registry_empty_but_meetings_have_names(sandbox, monkeypatch):
    import json

    from conftest import mfile
    from stt import config

    calls = []
    monkeypatch.setattr(run_batch.subprocess, "run",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(stdout=""))
    mfile("Mtg", ".json").write_text(json.dumps(
        {"speakers": [{"id": "Katie", "name": "Katie"}],
         "segments": [], "words": []}))
    assert run_batch.warn_if_registry_lost(config.MEETINGS_DIR) is True
    assert calls  # user-facing notification attempted

    # a healthy registry: no warning
    import numpy as np

    from stt import identify
    identify.enroll("Katie", np.random.default_rng(5).normal(size=256), source="Mtg")
    assert run_batch.warn_if_registry_lost(config.MEETINGS_DIR) is False


def test_extract_audio_is_atomic_on_interrupt(tmp_path, monkeypatch):
    """Finding #2: an interrupted extraction (Stop kills ffmpeg mid-write) must
    not leave a truncated dest .m4a — run_batch's existence guard would treat it
    as a finished extract and later delete the original against a corrupt
    archive. ffmpeg writes to a .part sibling, os.replace()d in only on success."""
    from pathlib import Path

    import pytest

    from stt import audio

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    dst = tmp_path / "clip.m4a"

    def killed_midwrite(cmd, **kw):
        # ffmpeg opens its output (the last arg) and writes partial bytes, then
        # the process group is torn down before it can finish.
        Path(cmd[-1]).write_bytes(b"truncated")
        raise KeyboardInterrupt("SIGTERM during extract")

    monkeypatch.setattr(audio.subprocess, "run", killed_midwrite)
    with pytest.raises(KeyboardInterrupt):
        audio.extract_audio(src, dst)

    # the final dest was never created — only a stale .part took the damage
    assert not dst.exists()


def test_rescan_never_loops_on_a_permanently_failing_file(sandbox, monkeypatch):
    """Finding #5: a file that fails every attempt (corrupt audio) is tried at
    most once per run. The end-of-run rescan excludes files that already failed,
    so run_batch can't spin forever holding the single-instance lock."""
    import signal
    import sys

    from stt import config, summarize

    (config.ICLOUD_DIR / "corrupt.m4a").write_bytes(b"\x00" * 64)

    calls = []

    def always_fails(src_str, dest_str, opts):
        calls.append(src_str)
        if len(calls) > 3:  # a looping rescan would re-attempt this forever
            raise SystemExit("rescan retried a permanently-failing file")
        raise RuntimeError("corrupt audio")

    monkeypatch.setattr(run_batch, "process_one", always_fails)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])

    old = signal.getsignal(signal.SIGTERM)
    try:
        rc = run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)

    assert len(calls) == 1  # attempted once, never retried by the rescan
    assert rc == 1  # the failure is reported


def test_rate_sample_tags_true_concurrency_not_stale_worker_count(sandbox, monkeypatch):
    """Finding #20: rate calibration must tag each sample with the concurrency
    it actually ran at. Under --parallel 2, a BrokenProcessPool solo-retry runs
    single-worker and must record n_active=1, not the batch's initial count."""
    import concurrent.futures as cf
    import signal
    import sys
    from concurrent.futures.process import BrokenProcessPool
    from pathlib import Path

    from stt import config, rates, summarize

    for name in ("A.m4a", "B.m4a"):
        (config.ICLOUD_DIR / name).write_bytes(b"\x00" * 64)

    # run the "parallel" branch in-process so the stubbed process_one is used
    # (a real ProcessPoolExecutor would re-import run_batch in a subprocess).
    class _Fut:
        def __init__(self, fn, a):
            self._fn, self._a = fn, a

        def result(self):
            return self._fn(*self._a)

    class _Pool:
        def __init__(self, max_workers=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *a):
            return _Fut(fn, a)

    monkeypatch.setattr(cf, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(cf, "as_completed", lambda futs: list(futs))

    calls = {}

    def stub(src_str, dest_str, opts):
        name = Path(src_str).name
        calls[name] = calls.get(name, 0) + 1
        if name == "A.m4a" and calls[name] == 1:
            raise BrokenProcessPool("worker died")  # forces the single-worker retry
        return {"ok": True, "key": name, "mtime": Path(src_str).stat().st_mtime,
                "outputs": [], "summary": "s", "who": "",
                "duration_sec": (100.0 if name == "A.m4a" else 200.0),
                "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", stub)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(summarize, "available", lambda: False)

    recorded = []
    monkeypatch.setattr(rates, "record",
                        lambda dur, ss, key, n_active=1: recorded.append((dur, n_active)))

    monkeypatch.setattr(sys, "argv",
                        ["run_batch.py", "--no-diarize", "--parallel", "2",
                         "--source", str(config.ICLOUD_DIR),
                         "--dest", str(config.MEETINGS_DIR)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)

    # B ran in the 2-worker pool; A's solo retry ran single-worker
    assert (200.0, 2) in recorded
    assert (100.0, 1) in recorded  # the fix: solo retry tagged n_active=1, not 2

"""run_batch.py: the queued-job spec used to re-queue an interrupted --job run
(see _terminate() in main()) so a Stop mid-file can never lose a Redo."""
import types

import run_batch


def _args(**over):
    base = dict(files=None, paths=None, force=False, strict=False,
                verify=False, parallel=1)
    base.update(over)
    return types.SimpleNamespace(**base)


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
    env = {**__import__("os").environ,
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
    if "already running" in r.stdout:
        import pytest
        pytest.skip("a real batch holds the single-instance lock right now")
    assert "Redo Me 07012026.m4a" in r.stdout  # the path actually parsed


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

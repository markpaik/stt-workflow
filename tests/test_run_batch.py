"""run_batch.py: the queued-job spec used to re-queue an interrupted --job run
(see _terminate() in main()) so a Stop mid-file can never lose a Redo."""
import os
import types

import run_batch

# one spoken line: the summary guard skips a transcript with 0 segments (unified
# plan, Phase 1 step 5), so a fixture that expects a summary must hold speech
SPOKEN = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "we will ship it"}]


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
        {"source_file": "Fresh Mtg.m4a", "segments": SPOKEN, "speakers": [], "words": []}))
    mfile("Done Mtg", ".json").write_text(json.dumps(
        {"source_file": "Done Mtg.m4a", "ai_summary": "already have one",
         "segments": SPOKEN, "speakers": [], "words": []}))

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
            {"source_file": f"{b}.m4a", "segments": SPOKEN, "speakers": [], "words": []}))
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
    repo_lock = _P(run_batch.__file__).parent / "batch.lock"

    def _stamp(p):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None
    before = _stamp(repo_lock)
    r = subprocess.run(
        [_sys.executable, run_batch.__file__, "--dry-run",
         "--paths", str(audio), "--force"],
        capture_output=True, text=True, timeout=60, env=env)
    assert "UnboundLocalError" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stderr
    assert "already running" not in r.stdout, r.stdout
    assert "Redo Me 07012026.m4a" in r.stdout  # the path actually parsed
    # a dry run takes no lock, in tmp or in the repo: it writes nothing, and
    # it previews even while a real batch holds the lock
    assert not (proj / "batch.lock").exists()
    assert _stamp(repo_lock) == before


def test_main_has_no_shadowed_module_imports():
    """The lock-independent half of the regression: any `from stt import X`
    INSIDE main() makes X local to the whole function, unbinding the
    module-level name for every line above it. Assert none of the shared
    modules appear in main()'s locals."""
    shadowed = set(run_batch.main.__code__.co_varnames) & {
        "config", "control", "icloud", "jobs", "manifest", "rates", "status"}
    assert not shadowed, f"module names shadowed as locals in main(): {shadowed}"


def test_sweep_shells_scrubs_unknown_refs_for_each_removed_folder(sandbox):
    """The shell sweep is a permanent folder removal, so it must go through the
    same purge path as the user's Delete: a run that dies after registering
    unknowns but before the json write leaves the shell's name in 'heard in'
    lists — sweeping without the scrub left speakers claiming meetings that no
    longer exist (U005/U007/U009: 'heard in 2 meetings', zero playable clips)."""
    import json

    from conftest import mfile
    from stt import config, unknowns

    mfile("Real 05012026", ".json").write_text(json.dumps(
        {"source_file": "Real 05012026.m4a",
         "segments": SPOKEN, "speakers": [], "words": []}))
    (config.MEETINGS_DIR / "Shell A").mkdir()                       # empty shell
    (config.MEETINGS_DIR / "Shell B").mkdir()                       # stray-txt shell
    (config.MEETINGS_DIR / "Shell B" / "Shell B.txt").write_text("x")
    (config.MEETINGS_DIR / "Audio Kept").mkdir()                    # holds audio: spared
    (config.MEETINGS_DIR / "Audio Kept" / "Audio Kept.m4a").write_bytes(b"a")
    unknowns.save({"speakers": {
        "U005": {"file": "U005.npy", "meetings": ["Shell A", "Real 05012026"]},
        "U007": {"file": "U007.npy", "meetings": ["Shell A", "Shell B"]},
        "U009": {"file": "U009.npy", "meetings": ["Audio Kept"]}}})

    assert run_batch._sweep_shells() == 2

    sp = unknowns.load()["speakers"]
    assert sp["U005"]["meetings"] == ["Real 05012026"]   # dead ref gone, live one kept
    assert sp["U007"]["meetings"] == []                  # both refs died with their shells
    assert sp["U009"]["meetings"] == ["Audio Kept"]      # spared folder, ref kept
    # refs only — the entries and their sample files are never reaped here
    assert set(sp) == {"U005", "U007", "U009"}


def test_warns_when_registry_empty_but_meetings_have_names(sandbox, monkeypatch):
    import json

    from conftest import mfile
    from stt import config

    calls = []
    monkeypatch.setattr(run_batch.subprocess, "run",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(stdout=""))
    mfile("Mtg", ".json").write_text(json.dumps(
        {"speakers": [{"id": "Jules", "name": "Jules"}],
         "segments": SPOKEN, "words": []}))
    assert run_batch.warn_if_registry_lost(config.MEETINGS_DIR) is True
    assert calls  # user-facing notification attempted

    # a healthy registry: no warning
    import numpy as np

    from stt import identify
    identify.enroll("Jules", np.random.default_rng(5).normal(size=256), source="Mtg")
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
                        lambda dur, ss, key, n_active=1, **k: recorded.append((dur, n_active)))

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


def test_summarize_one_drafts_only_when_missing(sandbox, monkeypatch):
    """The per-file pass: drafts for a meeting without an ai_summary, no-ops
    for one that already has it, and never leaves a stale 'summarizing' entry
    in the active status display."""
    import json

    from conftest import mfile
    from stt import status, summarize

    mfile("Fresh Mtg", ".json").write_text(json.dumps(
        {"source_file": "Fresh Mtg.m4a", "segments": SPOKEN, "speakers": [], "words": []}))
    mfile("Done Mtg", ".json").write_text(json.dumps(
        {"source_file": "Done Mtg.m4a", "ai_summary": "have one",
         "segments": SPOKEN, "speakers": [], "words": []}))
    calls = []
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", lambda b: calls.append(b) or {})

    seen = []
    real_set = status.set_stage
    monkeypatch.setattr(status, "set_stage", lambda *a, **k: seen.append(k) or real_set(*a, **k))
    assert run_batch.summarize_one("Fresh Mtg.m4a") is True
    assert run_batch.summarize_one("Done Mtg.m4a") is False
    assert calls == ["Fresh Mtg"]
    assert status.read().get("active", {}) == {}
    # the stage carries an estimate, and its time is kept for the next one
    assert seen == [{"est_sec": status.SUMMARY_EST_DEFAULT}]
    assert len(status.read()["summary_secs"]) == 1


def test_summaries_land_per_file_not_at_end_of_run(sandbox, monkeypatch):
    """Drive the REAL main() (pipeline stubbed): each meeting's description
    must be drafted the moment that file finishes, between files — not in one
    batch at end-of-run. With a long backlog the end is hours away, and a Stop
    pressed before then used to silently lose every summary of the run."""
    import json
    import sys as _sys
    from pathlib import Path as _P

    from conftest import mfile
    from stt import config, summarize
    # diarization is stubbed, so no real token is needed (and the shell may have none)
    monkeypatch.setattr(config, "resolve_hf_token", lambda: "synthetic")

    src = sandbox / "source"
    for b in ("A Mtg", "B Mtg"):
        (src / f"{b}.m4a").write_bytes(b"\x00" * 64)

    events = []

    def fake_process_one(src_str, dest_str, opts):
        base = _P(src_str).stem
        events.append(("process", base))
        j = mfile(base, ".json")
        j.write_text(json.dumps({"source_file": f"{base}.m4a",
                                 "segments": SPOKEN, "speakers": [], "words": []}))
        mfile(base, ".txt").write_text("[00:00] A: hi")
        return {"ok": True, "key": f"{base}.m4a",
                "mtime": (src / f"{base}.m4a").stat().st_mtime,
                "outputs": [str(j)], "summary": "1 speaker(s), 1.0 min",
                "who": "", "duration_sec": 60.0, "stage_secs": {}}

    def fake_suggest(base):
        events.append(("summarize", base))
        j = config.meeting_file(base, ".json")   # persist like the real one,
        d = json.loads(j.read_text())            # so the end-of-run sweep skips
        d["ai_summary"] = "drafted"
        j.write_text(json.dumps(d))
        return {}

    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", fake_suggest)
    monkeypatch.setattr(_sys, "argv",
                        ["run_batch.py", "--files", "A Mtg.m4a,B Mtg.m4a",
                         "--force", "--ignore-battery", "--ignore-pause"])
    run_batch.main()

    assert events == [("process", "A Mtg"), ("summarize", "A Mtg"),
                      ("process", "B Mtg"), ("summarize", "B Mtg")], events
    for b in ("A Mtg", "B Mtg"):
        d = json.loads(config.meeting_file(b, ".json").read_text())
        assert d["ai_summary"] == "drafted"


def _probe_streams(path):
    import subprocess

    from stt.audio import FFPROBE
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return [tuple(ln.split(",")[:2]) for ln in out.stdout.strip().splitlines()]


def test_extract_audio_really_extracts_from_an_mp4(tmp_path):
    """REAL ffmpeg, no stubs: the atomicity fix wrote to a '.part' name, which
    ffmpeg cannot infer a container from — extraction failed on every actual
    video while the stubbed atomicity test stayed green. The m4a muxer must be
    named explicitly (-f ipod) for the .part path to work at all."""
    import subprocess

    from stt import audio
    from stt.audio import FFMPEG

    src = tmp_path / "clip.mp4"   # a genuine video: color frames + AAC audio
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", "1", "-c:v", "libx264", "-c:a", "aac", "-shortest",
                    str(src)], check=True, capture_output=True)
    dst = tmp_path / "clip.m4a"

    audio.extract_audio(src, dst)

    assert dst.exists() and dst.stat().st_size > 500
    assert not dst.with_suffix(dst.suffix + ".part").exists()
    streams = _probe_streams(dst)
    assert ("audio", "aac") in [(t, c) for c, t in streams] or \
           ("aac", "audio") in streams          # stream-copied AAC, video dropped
    assert all(t != "video" for c, t in streams)


def test_extract_audio_reencodes_non_aac_tracks(tmp_path):
    """The fallback branch, also against real ffmpeg: PCM audio can't be
    stream-copied into an .m4a, so the re-encode path must produce AAC."""
    import subprocess

    from stt import audio
    from stt.audio import FFMPEG

    src = tmp_path / "clip.mov"   # PCM-in-mov: valid video, m4a-incompatible audio
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", "1", "-c:v", "libx264", "-c:a", "pcm_s16le", "-shortest",
                    str(src)], check=True, capture_output=True)
    dst = tmp_path / "clip.m4a"

    audio.extract_audio(src, dst)

    streams = _probe_streams(dst)
    assert ("aac", "audio") in streams
    assert all(t != "video" for c, t in streams)


def test_iter_sources_sweeps_recordings_folder(sandbox):
    """The batch must ingest the recorder's staging folder as a second source,
    skip in-progress dot-prefixed captures, and process a name present in both
    folders only once."""
    from stt import config
    src = config.source_dir()
    rec = config.recordings_dir()
    src.mkdir(parents=True, exist_ok=True)
    rec.mkdir(parents=True, exist_ok=True)
    (src / "From iCloud 05212026.m4a").write_bytes(b"a")
    (rec / "Team Call 07092026.m4a").write_bytes(b"b")
    (rec / ".rec-inprogress.caf").write_bytes(b"c")   # live capture, must be skipped
    (rec / ".Team Call 07092026.m4a.part").write_bytes(b"d")  # finalize temp, skipped
    names = [p.name for p in run_batch.iter_sources(src)]
    assert "From iCloud 05212026.m4a" in names
    assert "Team Call 07092026.m4a" in names
    assert not any(n.startswith(".") for n in names)
    assert len(names) == len(set(names))  # no dupes across folders


def test_iter_sources_flags_a_cross_folder_name_collision(sandbox, capsys):
    """C7: two DIFFERENT files that merely share a basename across the source and
    recordings folders collide on the same meeting folder (base = stem) and
    manifest key. iter_sources yields one AND surfaces the clash on stderr rather
    than silently dropping the second."""
    from stt import config
    src = config.source_dir()
    rec = config.recordings_dir()
    src.mkdir(parents=True, exist_ok=True)
    rec.mkdir(parents=True, exist_ok=True)
    (src / "Weekly Sync 07092026.m4a").write_bytes(b"one")
    (rec / "Weekly Sync 07092026.m4a").write_bytes(b"a different recording")
    names = [p.name for p in run_batch.iter_sources(src)]
    assert names.count("Weekly Sync 07092026.m4a") == 1
    assert "more than one watched folder" in capsys.readouterr().err


def test_process_one_copies_audio_atomically(sandbox, monkeypatch):
    """C8: the source audio is copied into the meetings store via .part +
    os.replace. A crash mid-copy must not leave a truncated file that the
    success guard (dest_audio.exists()) would count as done and then delete the
    original against."""
    from pathlib import Path

    import pytest

    from stt import config, icloud, pipeline

    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    audio_src = src / "Sync 05012026.m4a"
    audio_src.write_bytes(b"REAL AUDIO BYTES")

    def fake_process_file(s, dest_dir=None, **kw):
        base = Path(s).stem
        config.meeting_dir(base, dest_dir).mkdir(parents=True, exist_ok=True)
        j = config.meeting_file(base, ".json", dest_dir)
        j.write_text("{}")
        t = config.meeting_file(base, ".txt", dest_dir)
        t.write_text("x")
        return {"base": base, "txt": t, "json": j, "emb": None,
                "duration_sec": 1.0, "n_speakers": 1, "identified": []}
    monkeypatch.setattr(pipeline, "process_file", fake_process_file)
    monkeypatch.setattr(icloud, "materialize", lambda p: True)
    # the audio-copy folder comes from the RESOLVED base process_file returned

    real_replace = run_batch.os.replace

    def crash_on_publish(a, b, *r, **k):
        if str(a).endswith(".part"):          # the audio publish, not status writes
            raise OSError("crash during audio copy")
        return real_replace(a, b, *r, **k)
    monkeypatch.setattr(run_batch.os, "replace", crash_on_publish)

    opts = {"do_diarize": False, "strict": False, "verify": False,
            "track_unknowns": True, "allowed": None, "do_move": True}
    with pytest.raises(OSError):
        run_batch.process_one(str(audio_src), str(config.MEETINGS_DIR), opts)
    assert not config.meeting_file("Sync 05012026", ".m4a").exists()  # nothing published
    assert audio_src.exists()                                          # original preserved


def test_selection_never_skips_a_file_the_panel_flags_as_a_duplicate(sandbox, capsys, monkeypatch):
    """stt.dupes is a REVIEW signal for the panel only: run_batch's own file
    selection (held / manifest.is_processed / --only / --files) never consults
    it, so a file that looks like a byte-identical copy of a past meeting is
    still queued for a real run unless a human deletes or holds it themselves.
    Pinned here with --dry-run against the exact scenario stt.dupes.
    source_duplicates() would flag: same bytes, new filename, no manifest
    record under this name."""
    import sys as _sys

    from conftest import mfile
    from stt import config

    import json as _json
    audio = b"RIFF" + b"\x07" * 9000
    mfile("Board Prep 05012026", ".json").write_text(_json.dumps(
        {"source_file": "board prep.m4a", "duration_sec": 600.0,
         "speakers": [], "segments": SPOKEN, "words": []}))
    mfile("Board Prep 05012026", ".m4a").write_bytes(audio)

    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    copy = src / "board prep 2.m4a"          # byte-identical, new name
    copy.write_bytes(audio)

    monkeypatch.setattr(_sys, "argv", ["run_batch.py", "--dry-run", "--no-diarize"])
    assert run_batch.main() == 0
    out = capsys.readouterr().out
    assert "board prep 2.m4a" in out
    assert "0 to process" not in out, "the flagged copy must still be selected"


def test_a_hold_placed_mid_run_stops_the_next_file(sandbox, monkeypatch):
    """W3: the todo list read the parks ONCE, before the run started. A hold
    placed on a later file in the same run did not stop that file from
    processing, and on success the run also released the hold it had never
    honored -- breaking the documented contract that an automatic sweep always
    skips a held file."""
    import signal
    import sys
    from pathlib import Path

    from stt import config, holds, summarize

    (config.ICLOUD_DIR / "one.m4a").write_bytes(b"\x00" * 64)
    (config.ICLOUD_DIR / "two.m4a").write_bytes(b"\x00" * 64)

    seen = []

    def fake_process_one(src_str, dest_str, opts):
        src = Path(src_str)
        seen.append(src.name)
        if src.name == "one.m4a":
            holds.hold("two.m4a")        # the user parks the next file mid-run
            holds.hold("one.m4a")        # and parks the one being processed
        return {"ok": True, "key": src.name, "mtime": src.stat().st_mtime,
                "outputs": [], "summary": "1 speaker(s)", "who": "",
                "base": src.stem, "duration_sec": 1.0, "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])

    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)

    assert seen == ["one.m4a"], "a park placed mid-run must stop the next file"
    assert holds.items() == {"one.m4a", "two.m4a"}, \
        "the run must never release a hold it did not honor"


def test_naming_a_held_file_overrides_the_park_and_clears_it(sandbox, monkeypatch):
    """W3, the other side of the contract: naming a held file explicitly is the
    user asking for it, which outranks the park -- and once the file is
    processed, clearing that park cannot pin a stale name."""
    import signal
    import sys
    from pathlib import Path

    from stt import config, holds, summarize

    (config.ICLOUD_DIR / "one.m4a").write_bytes(b"\x00" * 64)
    holds.hold("one.m4a")
    seen = []

    def fake_process_one(src_str, dest_str, opts):
        src = Path(src_str)
        seen.append(src.name)
        return {"ok": True, "key": src.name, "mtime": src.stat().st_mtime,
                "outputs": [], "summary": "1 speaker(s)", "who": "",
                "base": src.stem, "duration_sec": 1.0, "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--files", "one.m4a",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])

    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)

    assert seen == ["one.m4a"]
    assert holds.items() == set()


# ---------- the order the queue runs in: shortest first, re-picked per file ----------

def _lengths(monkeypatch, secs):
    """Stub the shared length helper by file name (seconds of audio)."""
    from stt import queue_order
    monkeypatch.setattr(queue_order, "duration",
                        lambda p: secs.get(__import__("os").path.basename(str(p))))


def _drive(monkeypatch, argv, on_file=None):
    """Run the REAL main() with the pipeline stubbed. Returns (rc, seen,
    pending_at_start): the files in the order they started, and status.json's
    pending list at the moment each one started. on_file(name) runs inside
    each file, for a test to change the world mid-run."""
    import signal
    import sys
    from pathlib import Path

    from stt import config, status, summarize

    seen, pend = [], []

    def fake_process_one(src_str, dest_str, opts):
        src = Path(src_str)
        seen.append(src.name)
        pend.append(list(status.read().get("pending", [])))
        if on_file:
            on_file(src.name)
        return {"ok": True, "key": src.name, "mtime": src.stat().st_mtime,
                "outputs": [], "summary": "1 speaker(s)", "who": "",
                "base": src.stem, "duration_sec": 1.0, "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)] + argv)
    old = signal.getsignal(signal.SIGTERM)
    try:
        rc = run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)
    return rc, seen, pend


def _drop(*names):
    from stt import config
    for n in names:
        (config.ICLOUD_DIR / n).write_bytes(b"\x00" * 64)


def test_the_shortest_meeting_runs_first_and_pending_says_so(sandbox, monkeypatch):
    """Name order put three multi-hour board meetings ahead of a 20-minute
    check-in. Every run takes the shortest file first (ties by name), and
    status.json's pending lists the rest in the order they will run."""
    _lengths(monkeypatch, {"A Board.m4a": 3 * 3600, "B Board.m4a": 4 * 3600,
                           "C Budget.m4a": 5400, "D Check-in.m4a": 1200,
                           "E Check-in.m4a": 1200})
    _drop("A Board.m4a", "B Board.m4a", "C Budget.m4a", "D Check-in.m4a",
          "E Check-in.m4a")
    rc, seen, pend = _drive(monkeypatch, [])
    assert rc == 0
    assert seen == ["D Check-in.m4a", "E Check-in.m4a", "C Budget.m4a",
                    "A Board.m4a", "B Board.m4a"]
    assert pend[0] == ["E Check-in.m4a", "C Budget.m4a", "A Board.m4a", "B Board.m4a"]
    assert pend[2] == ["A Board.m4a", "B Board.m4a"]
    assert pend[4] == []


def test_a_dry_run_lists_the_files_in_the_order_they_will_run(sandbox, monkeypatch, capsys):
    import sys

    from stt import config
    _lengths(monkeypatch, {"a long.m4a": 7200, "b short.m4a": 600})
    _drop("a long.m4a", "b short.m4a")
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--dry-run", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    assert run_batch.main() == 0
    out = capsys.readouterr().out
    assert out.index("[would process] b short.m4a") < out.index("[would process] a long.m4a")


def test_a_short_recording_that_lands_mid_run_jumps_the_queue(sandbox, monkeypatch):
    """The list is not fixed when the run starts: after every file the run
    re-reads the folders and takes the shortest file left, so a quick
    recording that arrives during a check-in runs before the queued board
    meetings, not hours later."""
    _lengths(monkeypatch, {"Board 1.m4a": 3 * 3600, "Board 2.m4a": 3 * 3600,
                           "Check-in.m4a": 1200, "Quick note.m4a": 300})
    _drop("Board 1.m4a", "Board 2.m4a", "Check-in.m4a")

    def arrive(name):
        if name == "Check-in.m4a":
            _drop("Quick note.m4a")
    rc, seen, pend = _drive(monkeypatch, [], on_file=arrive)
    assert rc == 0
    assert seen == ["Check-in.m4a", "Quick note.m4a", "Board 1.m4a", "Board 2.m4a"]
    assert pend[1] == ["Board 1.m4a", "Board 2.m4a"]


def test_a_jobs_own_files_run_shortest_first_and_take_in_nothing_else(
        sandbox, monkeypatch):
    """A queued job (--files) runs exactly its own list, shortest first; a
    recording that lands meanwhile waits for the next run."""
    _lengths(monkeypatch, {"Long.m4a": 7200, "Short.m4a": 900, "Other.m4a": 60})
    _drop("Long.m4a", "Short.m4a")

    def arrive(name):
        _drop("Other.m4a")
    rc, seen, pend = _drive(monkeypatch, ["--files", "Long.m4a,Short.m4a"],
                            on_file=arrive)
    assert rc == 0 and seen == ["Short.m4a", "Long.m4a"]
    assert pend[0] == ["Long.m4a"]


def test_each_parallel_worker_takes_the_shortest_file_left(sandbox, monkeypatch):
    """--parallel 2: a free worker takes the shortest file left at that
    moment, and a recording that lands mid-run goes ahead of longer ones
    still waiting."""
    import concurrent.futures as cf
    import signal
    import sys
    from pathlib import Path

    from stt import config, summarize

    _lengths(monkeypatch, {"A.m4a": 4000, "B.m4a": 3000, "C.m4a": 2000,
                           "D.m4a": 1000, "New.m4a": 10})
    _drop("A.m4a", "B.m4a", "C.m4a", "D.m4a")
    submitted, ran = [], []

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
            submitted.append(Path(a[0]).name)
            return _Fut(fn, a)

    monkeypatch.setattr(cf, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(cf, "as_completed", lambda futs: list(futs))

    def stub(src_str, dest_str, opts):
        name = Path(src_str).name
        ran.append(name)
        if name == "D.m4a":
            _drop("New.m4a")
        return {"ok": True, "key": name, "mtime": Path(src_str).stat().st_mtime,
                "outputs": [], "summary": "s", "who": "", "duration_sec": 1.0,
                "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", stub)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize", "--parallel", "2",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    # D and C fill the two workers; D finishes first and New lands, so the
    # freed worker takes New, not B; then B, then A
    assert submitted == ["D.m4a", "C.m4a", "New.m4a", "B.m4a", "A.m4a"]
    assert sorted(ran) == sorted(submitted)


# ---------- finishing the current file, then stopping ----------

def test_a_stop_after_flag_ends_the_run_after_the_current_file(sandbox, monkeypatch):
    """A running batch reads no pause flag between files, and Stop kills the
    file in flight. stop_after_file.flag is the gentle stop: the file in
    flight finishes, nothing new starts, the run ends cleanly, the flag is
    consumed, and no queued job is chained after it."""
    from stt import control, jobs, status
    _lengths(monkeypatch, {"one.m4a": 60, "two.m4a": 120, "three.m4a": 180})
    _drop("one.m4a", "two.m4a", "three.m4a")
    jobs.add({"files": ["later.m4a"], "label": "later"})
    chained = []
    monkeypatch.setattr(run_batch, "spawn_chained_job", lambda j: chained.append(j))

    def ask(name):
        control.request_finish()
    rc, seen, _ = _drive(monkeypatch, [], on_file=ask)
    assert rc == 0 and seen == ["one.m4a"]
    assert not control.finish_requested(), "the honored flag must be consumed"
    assert status.read()["running"] is False
    assert chained == [], "stopping means no chained job either"
    assert len(jobs.items()) == 1, "the queued job stays queued"


def test_a_stale_stop_after_flag_never_cuts_a_new_run_short(sandbox, monkeypatch):
    from stt import control
    _lengths(monkeypatch, {"one.m4a": 60, "two.m4a": 120})
    _drop("one.m4a", "two.m4a")
    control.request_finish()          # left over from a run that had ended
    rc, seen, _ = _drive(monkeypatch, [])
    assert rc == 0 and seen == ["one.m4a", "two.m4a"]
    assert not control.finish_requested()


def test_under_parallel_the_stop_flag_lets_the_files_in_flight_finish(
        sandbox, monkeypatch):
    """--parallel 2 with the flag set while two files run: both finish and
    are recorded, and no third file starts."""
    import concurrent.futures as cf
    import signal
    import sys
    from pathlib import Path

    from stt import config, control, manifest, summarize

    _lengths(monkeypatch, {"a.m4a": 60, "b.m4a": 120, "c.m4a": 180})
    _drop("a.m4a", "b.m4a", "c.m4a")
    started = []

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
            started.append(Path(a[0]).name)
            return _Fut(fn, a)

    monkeypatch.setattr(cf, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(cf, "as_completed", lambda futs: list(futs))

    def stub(src_str, dest_str, opts):
        name = Path(src_str).name
        control.request_finish()
        return {"ok": True, "key": name, "mtime": Path(src_str).stat().st_mtime,
                "outputs": [], "summary": "s", "who": "", "duration_sec": 1.0,
                "stage_secs": {}}

    monkeypatch.setattr(run_batch, "process_one", stub)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize", "--parallel", "2",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert started == ["a.m4a", "b.m4a"]
    m = manifest.load()
    assert set(m["processed"]) == {"a.m4a", "b.m4a"}
    assert not control.finish_requested()


def test_a_plain_run_syncs_voice_memos_again_mid_run(sandbox, monkeypatch):
    """A memo that finishes during a long run joins that run: the re-sweep
    before each pick calls the Voice Memos sync again (throttled)."""
    import sys as _sys
    from pathlib import Path as _P

    from stt import config, summarize, voicememos

    src = sandbox / "source"
    (src / "A Mtg.m4a").write_bytes(b"\x00" * 64)
    syncs, processed = [], []

    def fake_sync(dry_run=False, **k):
        syncs.append(dry_run)
        if len(syncs) == 2:     # the memo finished while A Mtg ran
            d = voicememos.staging_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / "Synced Memo 11032026.m4a").write_bytes(b"\x00" * 64)
        return {"status": "ok", "staged": [], "counts": {}}

    def fake_process_one(src_str, dest_str, opts):
        p = _P(src_str)
        processed.append(p.name)
        return {"ok": False, "key": p.name}

    monkeypatch.setattr(run_batch, "VM_RESYNC_SECONDS", 0.0)
    monkeypatch.setattr(voicememos, "sync", fake_sync)
    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(config, "resolve_hf_token", lambda: "synthetic")
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(_sys, "argv", ["run_batch.py", "--source", str(src),
                                       "--ignore-battery", "--ignore-pause"])
    run_batch.main()
    assert len(syncs) >= 2
    assert processed == ["A Mtg.m4a", "Synced Memo 11032026.m4a"]


def test_a_renamed_meeting_whose_original_stays_is_not_transcribed_again(
        sandbox, monkeypatch):
    """The automatic title renames a meeting folder right after the file
    finishes. manifest.retarget fixes the manifest on disk. The run's own
    snapshot still named the old folder, so with the original kept in the
    watched folder the mid-run rescan saw an unprocessed file and
    transcribed it again into a new folder, then again, in a loop."""
    import json
    import shutil
    import signal
    import sys
    from pathlib import Path

    from conftest import mfile
    from stt import config, manifest, summarize

    _drop("Kept Mtg.m4a")
    seen = []

    def fake_process_one(src_str, dest_str, opts):
        src = Path(src_str)
        seen.append(src.name)
        if len(seen) > 3:
            raise RuntimeError("re-transcribed in a loop")
        j = mfile(src.stem, ".json")
        j.write_text(json.dumps({"source_file": src.name, "segments": [],
                                 "speakers": [], "words": []}))
        mfile(src.stem, ".txt").write_text("[00:00] A: hi")
        return {"ok": True, "key": src.name, "mtime": src.stat().st_mtime,
                "outputs": [str(j), str(j.with_suffix(".txt"))],
                "summary": "1 speaker(s)", "who": "", "base": src.stem,
                "duration_sec": 1.0, "stage_secs": {}}

    def fake_summarize_one(key, base=None, track=None):
        # the inline automatic title: move the folder and its files, then
        # retarget the manifest on disk, as summarize.apply_auto_title does
        old = config.meeting_file(base, ".json").parent
        new = old.parent / "Titled Mtg"
        shutil.move(str(old), str(new))
        for f in list(new.iterdir()):
            f.rename(new / ("Titled Mtg" + f.name[len(base):]))
        manifest.retarget(old, new, base, "Titled Mtg")
        return True

    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", fake_summarize_one)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize", "--keep-original",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        rc = run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)
    assert seen == ["Kept Mtg.m4a"], seen
    assert rc == 0
    assert (config.ICLOUD_DIR / "Kept Mtg.m4a").exists()


def test_the_end_of_run_sweep_follows_a_meeting_the_title_renamed(sandbox, monkeypatch):
    """The inline pass titles and renames the folder. The end-of-run sweep
    used to get the folder name from before that rename and log a false
    FileNotFoundError. It now gets the folder's final name."""
    import json

    from conftest import mfile
    from stt import summarize

    mfile("Raw Mtg", ".json").write_text(json.dumps(
        {"source_file": "Raw Mtg.m4a", "segments": SPOKEN, "speakers": [], "words": []}))

    def fake_suggest(base):
        j = mfile(base, ".json")
        d = json.loads(j.read_text())
        d["ai_summary"] = "drafted"
        j.write_text(json.dumps(d))
        return {}

    def fake_apply(base):
        if base != "Raw Mtg":
            return {"ok": True, "base": base, "skipped": "no draft title"}
        old = mfile(base, ".json")
        new = mfile("Titled Mtg", ".json")
        new.write_text(old.read_text())
        old.unlink()
        old.parent.rmdir()
        return {"ok": True, "base": "Titled Mtg"}

    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", fake_suggest)
    monkeypatch.setattr(summarize, "apply_auto_title", fake_apply)
    track = {"base": "Raw Mtg"}
    assert run_batch.summarize_one("Raw Mtg.m4a", "Raw Mtg", track=track) is True
    assert track == {"base": "Titled Mtg"}
    seen = []
    real = run_batch.summarize_one
    monkeypatch.setattr(run_batch, "summarize_one",
                        lambda k, b=None, **kw: seen.append(b) or real(k, b, **kw))
    assert run_batch.auto_summarize([("Raw Mtg.m4a", track["base"])]) == 0
    assert seen == ["Titled Mtg"]


def test_an_empty_run_skips_the_doctor_unless_its_report_is_a_day_old(sandbox, monkeypatch):
    """The watch trigger starts an empty run every few minutes. Each one ran
    the full doctor check. The check now runs after a run that processed or
    failed a file, or when the last report is a day old or missing."""
    import os
    import time

    from stt import config, doctor
    calls = []
    monkeypatch.setattr(doctor, "run_check_at_batch_end",
                        lambda store=None: calls.append(store) or {"findings": 0})
    rep = config.PROJECT_DIR / doctor.REPORT_PATH_NAME
    _drive(monkeypatch, [])
    assert len(calls) == 1            # no report yet: the check runs
    rep.write_text("{}")
    _drive(monkeypatch, [])
    assert len(calls) == 1            # an empty run with a fresh report: skipped
    _drop("New Mtg.m4a")
    _drive(monkeypatch, [])
    assert len(calls) == 2            # a file was processed: the check runs
    old = time.time() - 25 * 3600
    os.utime(rep, (old, old))
    _drive(monkeypatch, [])
    assert len(calls) == 3            # a day-old report: the check runs


def test_a_sandboxed_batch_reads_the_sandbox_stt_env_not_the_repo_one(tmp_path, monkeypatch):
    """Under STT_HOME the settings file is $STT_HOME/stt.env, as config says.
    The repo stt.env holds the REAL library and iCloud paths, and loading it
    first made a sandboxed batch read, clean and sweep the real library."""
    import run_batch
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    (repo / "stt.env").write_text("STT_MEETINGS_DIR=/real/meetings\n"
                                  "STT_ICLOUD_DIR=/real/icloud\nSTT_ZZ_REPO=1\n")
    (home / "stt.env").write_text("STT_ZZ_HOME=1\n")
    for k in ("STT_MEETINGS_DIR", "STT_ICLOUD_DIR", "STT_ZZ_REPO", "STT_ZZ_HOME",
              "STT_PROJECT_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STT_HOME", str(home))
    run_batch._load_env_file(repo)
    for k in ("STT_MEETINGS_DIR", "STT_ICLOUD_DIR", "STT_ZZ_REPO"):
        assert k not in os.environ, k
    assert os.environ.get("STT_ZZ_HOME") == "1"

    # with no STT_HOME the repo file is the settings file, as before
    monkeypatch.delenv("STT_HOME")
    monkeypatch.delenv("STT_ZZ_HOME")
    run_batch._load_env_file(repo)
    assert os.environ.get("STT_MEETINGS_DIR") == "/real/meetings"


def test_iter_audio_skips_empty_files(sandbox):
    """A 0-byte file (a failed export, a copy still being created) is not a
    recording. The batch never picks it, and the panel never lists it."""
    src = sandbox / "source"
    (src / "zero.m4a").write_bytes(b"")
    (src / "real.m4a").write_bytes(b"x" * 64)
    assert [p.name for p in run_batch.iter_audio(src)] == ["real.m4a"]
    from stt import config
    assert not config.is_source_audio(src / "zero.m4a")
    assert config.is_source_audio(src / "real.m4a")


def test_process_one_names_an_empty_file(sandbox, monkeypatch):
    """A 0-byte file named by --paths fails at once with a reason that
    says so, never after a 15-minute wait on iCloud."""
    import pytest
    from stt import config
    f = sandbox / "zero.m4a"
    f.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        run_batch.process_one(str(f), str(config.MEETINGS_DIR),
                              {"do_diarize": False, "strict": False,
                               "allowed": None, "do_move": False})


def _run_main(monkeypatch, *extra):
    """run_batch.main() in the sandbox with the model steps stubbed off."""
    import signal
    import sys

    from stt import config, summarize
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR), *extra])
    old = signal.getsignal(signal.SIGTERM)
    try:
        return run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)


def test_a_corrupt_source_fails_with_ffmpegs_reason_and_no_traceback(sandbox, monkeypatch, capsys):
    """The failure line and the history row carry one short reason, never
    the ffmpeg command line with absolute paths, and no Python traceback."""
    from stt import config, status
    (config.ICLOUD_DIR / "Corrupt Clip.wav").write_bytes(b"garbage")
    assert _run_main(monkeypatch) == 1
    err = capsys.readouterr().err
    want = ("ffmpeg could not decode Corrupt Clip.wav: "
            "Invalid data found when processing input")
    assert f"FAILED: Corrupt Clip.wav: {want}" in err
    assert "Traceback" not in err and "exit status" not in err
    assert str(config.ICLOUD_DIR) not in err
    row = status.read()["recent"][0]
    assert row["name"] == "Corrupt Clip.wav" and row["summary"] == want


def _history(name):
    import json as _json

    from stt import status
    try:
        rows = [_json.loads(x) for x in status.HISTORY_LOG.read_text().splitlines()]
    except OSError:
        return []
    return [r for r in rows if r.get("name") == name]


def test_a_corrupt_source_is_set_aside_after_it_fails(sandbox, monkeypatch, capsys):
    """A file ffmpeg cannot read failed, and added a history line, on every
    automatic run. It is now set aside after its first failure. Retry (the
    file named by --files) still runs it, and a changed file runs again."""
    import os as _os

    from stt import config
    bad = config.ICLOUD_DIR / "Corrupt Clip.wav"
    bad.write_bytes(b"garbage")
    assert _run_main(monkeypatch) == 1
    assert _run_main(monkeypatch) == 0
    assert _run_main(monkeypatch) == 0
    assert len(_history(bad.name)) == 1
    assert "set aside" in capsys.readouterr().out
    assert _run_main(monkeypatch, "--files", bad.name) == 1   # Retry runs it
    assert len(_history(bad.name)) == 2
    bad.write_bytes(b"garbage, re-exported")
    _os.utime(bad, (bad.stat().st_mtime + 60,) * 2)
    assert _run_main(monkeypatch) == 1                       # changed: runs again
    assert len(_history(bad.name)) == 3


def test_a_file_that_keeps_failing_is_set_aside_after_three_runs(sandbox, monkeypatch):
    from stt import config, manifest
    (config.ICLOUD_DIR / "flaky.m4a").write_bytes(b"\x00" * 64)
    seen = []

    def fake_process_one(src_str, dest_str, opts):
        seen.append(src_str)
        raise RuntimeError("the model crashed")
    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    for _ in range(manifest.FAIL_PARK_AFTER + 2):
        _run_main(monkeypatch)
    assert len(seen) == manifest.FAIL_PARK_AFTER


def test_a_full_disk_never_sets_a_file_aside(sandbox, monkeypatch):
    """A failure that is this Mac's (the converted audio could not be
    written: a full disk) parked every file a run touched, and the files
    stayed parked after space was freed."""
    import errno as _errno

    from stt import audio, config, manifest
    (config.ICLOUD_DIR / "Board.m4a").write_bytes(b"\x00" * 64)
    seen = []

    def fake_process_one(src_str, dest_str, opts):
        # a new exception each time: a kept one holds the run's frames, and
        # with them the lock file of a finished run
        seen.append(src_str)
        if len(seen) % 2:
            raise OSError(_errno.ENOSPC, "No space left on device")
        raise audio.WriteFailed("could not write the converted audio for "
                                "Board.m4a: No space left on device")
    monkeypatch.setattr(run_batch, "process_one", fake_process_one)
    for _ in range(manifest.FAIL_PARK_AFTER + 2):
        _run_main(monkeypatch)
    assert len(seen) == manifest.FAIL_PARK_AFTER + 2
    assert "Board.m4a" not in (manifest.load().get("failed") or {})


def test_a_second_batch_never_blanks_the_lock_holders_pid(sandbox):
    """acquire_lock opened batch.lock with "w", so every run that LOST the
    lock race (the folder watch fires often) truncated the pid the running
    batch wrote. The panel reads that pid to find this home's batch."""
    from stt import config
    first = run_batch.acquire_lock()
    try:
        assert first is not None
        assert (config.PROJECT_DIR / "batch.lock").read_text() == str(os.getpid())
        assert run_batch.acquire_lock() is None
        assert (config.PROJECT_DIR / "batch.lock").read_text() == str(os.getpid())
    finally:
        first.close()
    again = run_batch.acquire_lock()       # a fresh holder writes only its pid
    try:
        assert (config.PROJECT_DIR / "batch.lock").read_text() == str(os.getpid())
    finally:
        again.close()


def test_a_requeued_job_keeps_every_cli_flag(sandbox):
    """A SIGTERM mid-run re-queues the claimed job from job_spec_from_args.
    The spec dropped --no-diarize, --keep-original, --only, --speakers and
    --ignore-battery, so the replay deleted originals a --keep-original run
    kept, and a --only run swept the whole folder."""
    from stt import jobs
    args = _args(files="U1 Mid.wav,U2 Long.wav", no_diarize=True, keep_original=True,
                 only="Mid", speakers="A Person,B Person", ignore_battery=True)
    spec = run_batch.job_spec_from_args(args, [])
    argv = jobs.spawn_args({**spec, "at": 1.0})
    assert argv[3:5] == [str(run_batch.config.CODE_DIR / "run.sh"), "batch"]
    ns = run_batch.build_parser().parse_args(argv[5:])
    assert ns.no_diarize and ns.keep_original and ns.ignore_battery
    assert ns.only == "Mid" and ns.speakers == "A Person,B Person"
    assert jobs.split_list(ns.files) == ["U1 Mid.wav", "U2 Long.wav"]
    # a value that starts with "-" stays a value
    argv = jobs.spawn_args({**spec, "only": "-x", "at": 1.0})
    assert run_batch.build_parser().parse_args(argv[5:]).only == "-x"


def test_a_plain_job_adds_no_cli_flags(sandbox):
    from stt import jobs
    spec = run_batch.job_spec_from_args(_args(files="A.m4a"), [])
    ns = run_batch.build_parser().parse_args(jobs.spawn_args({**spec, "at": 1.0})[5:])
    assert not (ns.no_diarize or ns.keep_original or ns.ignore_battery)
    assert ns.only is None and ns.speakers is None


def test_a_long_named_source_processes_and_moves(sandbox, monkeypatch):
    """A 251-character stem: the meeting lands in a capped folder, the
    audio copy fits, and the delete step (with its sidecar names past 255
    bytes) removes the original without raising."""
    from test_pipeline import _fake_asr

    from stt import audio, config, icloud, pipeline
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(audio, "to_wav16k", lambda s, d: d.write_bytes(b"x"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 10.0)
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(icloud, "materialize", lambda p: True)
    src = config.ICLOUD_DIR / ("L" * 251 + ".m4a")
    src.write_bytes(b"\x01" * 256)
    res = run_batch.process_one(str(src), str(config.MEETINGS_DIR),
                                {"do_diarize": False, "strict": False,
                                 "allowed": None, "do_move": True})
    assert res["ok"] and len(res["base"].encode()) <= config.MAX_BASE_BYTES
    assert config.meeting_file(res["base"], ".m4a").read_bytes() == b"\x01" * 256
    assert not src.exists()


def test_office_lock_files_are_not_recordings(sandbox):
    """'~$name.m4a' is an Office owner/lock file, not audio. iter_audio
    skipped only dot files, so the dry run listed it to process."""
    from stt import config
    src = sandbox / "source"
    (src / "~$lock.m4a").write_bytes(b"x" * 162)
    (src / "~$Board Prep.m4a").write_bytes(b"x" * 162)
    (src / "Meeting ~$ notes.m4a").write_bytes(b"x" * 64)   # only a leading ~$
    (src / "~Tilde Start.m4a").write_bytes(b"x" * 64)       # a plain name with ~
    assert [p.name for p in run_batch.iter_audio(src)] == [
        "Meeting ~$ notes.m4a", "~Tilde Start.m4a"]
    assert not config.is_source_audio(src / "~$lock.m4a")


def _summary_spy(monkeypatch):
    from stt import summarize
    calls = []
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "has_speech", lambda base: True)
    monkeypatch.setattr(summarize, "suggest_title", lambda base: calls.append(("describe", base)))
    monkeypatch.setattr(summarize, "apply_auto_title",
                        lambda base: calls.append(("title", base)) or {"ok": False})
    monkeypatch.setattr(summarize, "stale_bases", lambda: [])
    return calls


def _fake_done(src_str, dest_str, opts):
    from pathlib import Path
    src = Path(src_str)
    return {"ok": True, "key": src.name, "mtime": src.stat().st_mtime, "outputs": [],
            "summary": "1 speaker(s)", "who": "", "base": "2026-01-05 Mtg",
            "duration_sec": 1.0, "stage_secs": {}}


def test_a_run_into_another_folder_never_describes_the_librarys_meeting(sandbox, monkeypatch, capsys):
    """summarize_one resolved the just-finished meeting through the library
    (config.meetings_dir()), not --dest. A run with --dest elsewhere read,
    and could rewrite, the library's same-named meeting."""
    import json
    import signal
    import sys

    from conftest import mfile
    from stt import config
    mfile("2026-01-05 Mtg", ".json").write_text(json.dumps(
        {"source_file": "other.m4a", "segments": SPOKEN, "speakers": []}))
    (config.ICLOUD_DIR / "Mtg.m4a").write_bytes(b"\x00" * 64)
    other = sandbox / "other_dest"
    calls = _summary_spy(monkeypatch)
    monkeypatch.setattr(run_batch, "process_one", _fake_done)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(other)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert calls == []
    assert "not the meetings library" in capsys.readouterr().out
    # nor the idle refresh of out-of-date library descriptions
    stale = []
    monkeypatch.setattr(run_batch, "regenerate_stale", lambda *a, **k: stale.append(1) or 0)
    old = signal.getsignal(signal.SIGTERM)
    try:
        run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)
    assert stale == []


def test_a_run_into_the_library_still_describes_its_meeting(sandbox, monkeypatch):
    import json
    import signal
    import sys

    from conftest import mfile
    from stt import config
    mfile("2026-01-05 Mtg", ".json").write_text(json.dumps(
        {"source_file": "Mtg.m4a", "segments": SPOKEN, "speakers": []}))
    (config.ICLOUD_DIR / "Mtg.m4a").write_bytes(b"\x00" * 64)
    calls = _summary_spy(monkeypatch)
    monkeypatch.setattr(run_batch, "process_one", _fake_done)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert run_batch.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert ("describe", "2026-01-05 Mtg") in calls


def test_a_files_name_that_matches_nothing_is_reported(sandbox, monkeypatch, capsys):
    """--paths printed 'skip (not a file)' for a bad path, but --files
    printed nothing for a name no source file has: a Redo of a file that
    left the source folder looked like a success."""
    import sys

    from stt import config
    _lengths(monkeypatch, {"Here.m4a": 600})
    _drop("Here.m4a")
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--dry-run", "--no-diarize",
                                      "--files", "Nope.wav,Here.m4a",
                                      "--source", str(config.ICLOUD_DIR),
                                      "--dest", str(config.MEETINGS_DIR)])
    assert run_batch.main() == 0
    cap = capsys.readouterr()
    assert "   not found in source: Nope.wav" in cap.err
    assert "Here.m4a" not in cap.err
    assert "[would process] Here.m4a" in cap.out
    assert "1 to process, 0 already done, 1 not found." in cap.out


def test_a_dry_run_previews_while_another_batch_holds_the_lock(sandbox, monkeypatch, capsys):
    """The lock was taken before the dry-run branch, so a preview, which
    writes nothing, could not run during a batch."""
    import fcntl
    import sys

    from stt import config
    _lengths(monkeypatch, {"Here.m4a": 600})
    _drop("Here.m4a")
    held = open(config.PROJECT_DIR / "batch.lock", "a+")
    held.write("4242")
    held.flush()
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(sys, "argv", ["run_batch.py", "--dry-run", "--no-diarize",
                                          "--source", str(config.ICLOUD_DIR),
                                          "--dest", str(config.MEETINGS_DIR)])
        assert run_batch.main() == 0
        out = capsys.readouterr().out
        assert "[would process] Here.m4a" in out and "already running" not in out
        # the running batch's pid is untouched
        assert (config.PROJECT_DIR / "batch.lock").read_text() == "4242"
        # a real run still waits its turn
        monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize",
                                          "--source", str(config.ICLOUD_DIR),
                                          "--dest", str(config.MEETINGS_DIR)])
        assert run_batch.main() == 0
        assert "Another batch is already running" in capsys.readouterr().out
    finally:
        held.close()

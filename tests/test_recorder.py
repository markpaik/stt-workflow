"""recorder: naming, transcode/finalize, orphan recovery. No native binary or
TCC needed — finalize runs real ffmpeg on a fabricated stereo CAF."""
import subprocess
from datetime import datetime

import pytest

from stt import audio, config, recorder, status


def _stereo_caf(path, seconds=1.0):
    """A real 2-channel CAF: 440 Hz left, 880 Hz right."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [audio.FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
         "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True)
    return path


FIXED = datetime(2026, 7, 9, 14, 30, 0)


def test_final_name_defaults_and_date_suffix(sandbox):
    assert recorder.final_name("", now=FIXED) == "Recording 07092026 1430"
    # a name without an 8-digit run gets a parseable date appended
    assert recorder.final_name("Budget Sync", now=FIXED) == "Budget Sync 07092026"
    # a name that already carries a date is left alone
    assert recorder.final_name("LT Meeting 05212026", now=FIXED) == "LT Meeting 05212026"


def test_final_name_sanitizes_and_uniquifies(sandbox):
    assert "/" not in recorder.final_name("a/b:c", now=FIXED)
    (config.recordings_dir()).mkdir(parents=True, exist_ok=True)
    (config.recordings_dir() / "Budget Sync 07092026.m4a").write_bytes(b"x")
    assert recorder.final_name("Budget Sync", now=FIXED) == "Budget Sync 07092026 (2)"


def test_finalize_transcodes_stereo_and_clears_state(sandbox):
    caf = _stereo_caf(config.recordings_dir() / ".rec-abcd1234.caf")
    status.set_recording({"pid": 999999, "caf": str(caf)})
    r = recorder.finalize(caf, "Team Call")
    assert r["ok"], r
    dst = config.recordings_dir() / f"{r['name']}.m4a"
    assert dst.exists() and not caf.exists()
    # stereo is preserved (Phase 2 needs the channel split)
    ch = subprocess.run(
        [audio.FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True).stdout.strip()
    assert ch == "2"
    assert status.recording() is None
    # no dotfiles left behind for the watcher to trip on
    assert not list(config.recordings_dir().glob(".*.part"))


def test_finalize_rejects_empty_capture(sandbox):
    caf = config.recordings_dir() / ".rec-empty.caf"
    caf.parent.mkdir(parents=True, exist_ok=True)
    caf.write_bytes(b"\0" * 100)  # header-only, no audio
    status.set_recording({"pid": 1, "caf": str(caf)})
    r = recorder.finalize(caf, "x")
    assert not r["ok"] and "captured" in r["error"]
    assert status.recording() is None and not caf.exists()


def test_start_refused_without_binary(sandbox, monkeypatch):
    monkeypatch.setattr(recorder, "BINARY", config.PROJECT_DIR / "native" / "nope")
    r = recorder.start()
    assert not r["ok"] and "build-recorder" in r["error"]


def test_start_refused_when_disk_nearly_full(sandbox, monkeypatch):
    """G4: recording a long call needs headroom — start() refuses under the free
    space floor rather than filling the disk and corrupting the capture."""
    monkeypatch.setattr(recorder, "available", lambda: True)
    monkeypatch.setattr(recorder, "_free_bytes", lambda p: recorder.MIN_FREE_BYTES - 1)
    r = recorder.start()
    assert not r["ok"] and "free" in r["error"]


def test_recover_orphans_finalizes_dead_capture(sandbox):
    caf = _stereo_caf(config.recordings_dir() / ".rec-orphan1.caf")
    status.set_recording({"pid": 999999, "caf": str(caf)})  # dead pid
    names = recorder.recover_orphans()
    assert len(names) == 1 and names[0].startswith("Recording ")
    assert status.recording() is None and not caf.exists()
    assert (config.recordings_dir() / f"{names[0]}.m4a").exists()


def test_recover_orphans_sweeps_stray_caf(sandbox):
    # no status entry at all, but a CAF is sitting in staging
    caf = _stereo_caf(config.recordings_dir() / ".rec-stray9.caf")
    names = recorder.recover_orphans()
    assert len(names) == 1
    assert not caf.exists()


def test_finalize_writes_layout_sidecar_when_mic_speaker_set(sandbox, monkeypatch):
    monkeypatch.setattr(config, "MIC_SPEAKER", "Mark Paik")
    caf = _stereo_caf(config.recordings_dir() / ".rec-side01.caf")
    status.set_recording({"pid": 999999, "caf": str(caf)})
    r = recorder.finalize(caf, "Team Call")
    assert r["ok"]
    import json
    side = config.recordings_dir() / f"{r['name']}.opts.json"
    assert side.exists()
    opts = json.loads(side.read_text())
    assert opts == {"channel_layout": "mic_left_system_right", "mic_speaker": "Mark Paik"}


def test_finalize_no_sidecar_without_mic_speaker(sandbox, monkeypatch):
    monkeypatch.setattr(config, "MIC_SPEAKER", None)
    caf = _stereo_caf(config.recordings_dir() / ".rec-side02.caf")
    status.set_recording({"pid": 999999, "caf": str(caf)})
    r = recorder.finalize(caf, "Team Call")
    assert r["ok"]
    assert not list(config.recordings_dir().glob("*.opts.json"))


def test_halt_ends_the_live_recording_before_the_naming_dialog(sandbox, monkeypatch):
    """The reported bug: Stop killed the capture, but the menu bar's naming dialog
    is MODAL and finalize() (which clears the state) only runs after it is
    answered. Until then the raw status entry still said 'recording', so the panel
    kept showing a recording that had already stopped. halt() must end the LIVE
    state immediately, while keeping the entry so recover_orphans still finds the
    CAF if we die before finalize."""
    caf = config.recordings_dir() / ".rec-halt01.caf"
    caf.parent.mkdir(parents=True, exist_ok=True)
    caf.write_bytes(b"\0" * 100)
    status.set_recording({"pid": 4242, "caf": str(caf)})
    # pretend that pid is our live recorder, and that it exits on the SIGINT
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: str(pid) == "4242")
    monkeypatch.setattr(recorder, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(recorder.os, "killpg", lambda *a: None)
    monkeypatch.setattr(recorder.os, "getpgid", lambda pid: 4242)

    out = recorder.halt()
    assert out == caf
    # capture is over: NOTHING may report a live recording from here on
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: False)
    assert recorder.live_recording() is None
    # but the entry survives, so a crash before finalize is still recoverable
    assert status.recording()["caf"] == str(caf)
    assert status.recording()["pid"] is None


def test_live_recording_is_the_one_source_of_truth(sandbox, monkeypatch):
    """A status entry alone never means 'recording' — it outlives the capture
    until finalize, and a recycled pid can fake liveness."""
    assert recorder.live_recording() is None                      # nothing set
    status.set_recording({"pid": 999999, "caf": "/x/.rec-a.caf"})  # dead pid
    assert recorder.live_recording() is None
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: True)
    assert recorder.live_recording()["caf"] == "/x/.rec-a.caf"    # genuinely live


def test_panel_never_shows_a_recording_that_has_stopped(sandbox, monkeypatch):
    """The exact regression: the panel read the raw status key with no liveness
    check while the menu bar filtered on a dead pid, so a stopped recording stayed
    on the page forever. Both now answer from recorder.live_recording()."""
    from gui import server as srv
    status.set_recording({"pid": 999999, "caf": "/x/.rec-a.caf"})  # stopped/dead
    assert srv.gather_state()["recording"] is None
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: True)
    assert srv.gather_state()["recording"] is not None             # a live one shows


def test_pause_and_resume_signal_the_recorder_and_track_state(sandbox, monkeypatch):
    """Pause stops the helper WRITING frames (SIGUSR1) without ending the capture;
    resume (SIGUSR2) starts it again. The signals must reach the recorder itself —
    it is spawned directly for exactly this reason, since SIGUSR1 to a caffeinate
    wrapper would kill it."""
    import signal as _sig
    sent = []
    status.set_recording({"pid": 4242, "caf": "/x/.rec-a.caf",
                          "started_monotonic": 0.0, "paused": False, "paused_total": 0.0})
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: str(pid) == "4242")
    monkeypatch.setattr(recorder.os, "kill", lambda pid, s: sent.append((pid, s)))

    assert recorder.pause() == {"ok": True, "paused": True}
    assert sent == [(4242, _sig.SIGUSR1)]
    assert status.recording()["paused"] is True
    assert recorder.pause()["ok"]                    # idempotent: no second signal
    assert len(sent) == 1

    assert recorder.resume() == {"ok": True, "paused": False}
    assert sent[-1] == (4242, _sig.SIGUSR2)
    assert status.recording()["paused"] is False
    assert "paused_at" not in status.recording()     # the span was banked and closed


def test_recording_outcomes_land_as_status_notes_not_notifications(sandbox, monkeypatch):
    """Feedback lives IN the menu and panel via a persisted note — notification
    banners either failed to display from this unbundled app or arrived as
    unwanted osascript alerts. Empty capture -> ⚠ note naming permissions;
    success -> ✓ note; the next start supersedes whatever is there."""
    caf = config.recordings_dir() / ".rec-empty.caf"
    caf.parent.mkdir(parents=True, exist_ok=True)
    caf.write_bytes(b"\0" * 100)
    status.set_recording({"pid": 1, "caf": str(caf)})
    assert not recorder.finalize(caf, "x")["ok"]
    note = status.recorder_note()
    assert note and note["ok"] is False and "permission" in note["text"].lower()

    caf2 = _stereo_caf(config.recordings_dir() / ".rec-good.caf")
    status.set_recording({"pid": 1, "caf": str(caf2)})
    r = recorder.finalize(caf2, "Team Call")
    assert r["ok"]
    note = status.recorder_note()
    assert note["ok"] is True and r["name"] in note["text"]

    # a batch starting mid-display must not wipe it (start_run rebuilds status)
    status.start_run(["a.m4a"])
    assert status.recorder_note()["ok"] is True
    status.clear_recorder_note()
    assert status.recorder_note() is None


def test_capture_stalled_flags_a_header_only_caf(sandbox, monkeypatch, tmp_path):
    """A TCC denial delivers no frames and raises no error — the only tell is a
    CAF that never grows. The detector must fire ~10s in (so the menu bar warns
    DURING the meeting), never during the first seconds, never while paused, and
    never once real audio is flowing."""
    caf = tmp_path / ".rec-x.caf"
    caf.write_bytes(b"\0" * 4096)                       # header-only
    clock = {"t": 100.0}
    monkeypatch.setattr(recorder.time, "monotonic", lambda: clock["t"])
    rec = {"caf": str(caf), "started_monotonic": 100.0,
           "paused": False, "paused_total": 0.0}

    clock["t"] = 103.0
    assert not recorder.capture_stalled(rec)            # too early to judge
    clock["t"] = 112.0
    assert recorder.capture_stalled(rec)                # 12s in, still no audio
    assert not recorder.capture_stalled({**rec, "paused": True,
                                         "paused_at": 100.0})  # paused ≠ stalled
    caf.write_bytes(b"\0" * 50000)                      # audio is flowing
    assert not recorder.capture_stalled(rec)
    assert not recorder.capture_stalled(None)
    assert not recorder.capture_stalled({**rec, "caf": str(tmp_path / "gone.caf")})


def test_pause_refused_when_not_recording(sandbox):
    assert not recorder.pause()["ok"]
    assert not recorder.resume()["ok"]
    status.set_recording({"pid": 999999, "caf": "/x/.rec-a.caf"})  # dead pid
    assert not recorder.pause()["ok"]


def test_elapsed_seconds_excludes_paused_spans(sandbox, monkeypatch):
    """The readout counts RECORDED audio, not wall-clock since Start — otherwise
    a 10-minute coffee break would inflate a 2-minute recording to 12."""
    clock = {"t": 100.0}
    monkeypatch.setattr(recorder.time, "monotonic", lambda: clock["t"])
    rec = {"started_monotonic": 100.0, "paused": False, "paused_total": 0.0}
    clock["t"] = 130.0
    assert recorder.elapsed_seconds(rec) == 30            # 30s recorded

    # paused at t=130; the clock keeps running but the readout must not
    rec = {**rec, "paused": True, "paused_at": 130.0}
    clock["t"] = 190.0
    assert recorder.elapsed_seconds(rec) == 30            # still 30s, frozen

    # resumed after a 60s pause, then 10s more recorded
    rec = {"started_monotonic": 100.0, "paused": False, "paused_total": 60.0}
    clock["t"] = 200.0
    assert recorder.elapsed_seconds(rec) == 40            # 100s wall - 60s paused
    assert recorder.elapsed_seconds(None) == 0


def test_recorder_running_requires_recorder_identity(sandbox, monkeypatch):
    """C3: a recycled PID that is alive but is NOT our recorder must not read as
    a live recording — otherwise start() refuses forever and a real orphan is
    never recovered. Identity comes from the process command line."""
    import os as _os
    # this pytest process is alive but its command line is not stt-recorder
    assert not recorder._recorder_running(_os.getpid())
    assert not recorder._recorder_running(999999)          # dead pid
    assert not recorder._recorder_running(None)
    # alive AND the command line carries the recorder binary -> recognized
    monkeypatch.setattr(recorder, "_proc_cmdline",
                        lambda pid: "/usr/bin/caffeinate -i /x/native/stt-recorder /y.caf")
    assert recorder._recorder_running(_os.getpid())


def test_recover_orphans_keeps_a_live_recording(sandbox, monkeypatch):
    """C4: the menu bar can restart while a detached recorder keeps running.
    Sweeping an OLD stray CAF from an earlier crash must finalize the stray but
    leave the LIVE recording's status intact (finalize clears only the state
    that names the CAF it finalized)."""
    live = {"pid": 4242, "caf": str(config.recordings_dir() / ".rec-live.caf")}
    config.recordings_dir().mkdir(parents=True, exist_ok=True)
    status.set_recording(live)
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: str(pid) == "4242")
    stray = _stereo_caf(config.recordings_dir() / ".rec-stray.caf")  # not the live one
    names = recorder.recover_orphans()
    assert len(names) == 1 and not stray.exists()   # the stray was recovered
    assert status.recording() == live               # the live recording untouched


def test_finalize_drops_caf_before_publishing_so_a_crash_cannot_duplicate(sandbox, monkeypatch):
    """C2: if the machine dies between the transcode and publishing the m4a, no
    CAF may survive for recover_orphans to re-finalize into a DUPLICATE meeting.
    Simulate a crash exactly at the publish step and assert the CAF is gone."""
    caf = _stereo_caf(config.recordings_dir() / ".rec-crash.caf")
    status.set_recording({"pid": 999999, "caf": str(caf)})
    real_replace = recorder.os.replace

    def crash_on_publish(src, dst, *a, **k):
        if str(src).endswith(".part"):        # the m4a publish, not status writes
            raise OSError("simulated crash before publish")
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(recorder.os, "replace", crash_on_publish)

    with pytest.raises(OSError):
        recorder.finalize(caf, "Team Call")
    assert not caf.exists()                    # dropped BEFORE the failed publish
    # a later recovery finds no stray CAF, so it cannot mint a duplicate
    assert recorder.recover_orphans() == []
    assert not list(config.recordings_dir().glob("*.m4a"))  # nothing published


def test_start_kicks_the_tap_writer_gate(sandbox, monkeypatch):
    # macOS holds a tap-containing device until some app writes audio; start()
    # must fire the silent afplay kick so quiet-Mac captures ever begin
    from stt import recorder
    calls = []

    class FakeRun:
        returncode = 0
        stdout = "123\n"
        stderr = ""

    monkeypatch.setattr(recorder, "available", lambda: True)
    monkeypatch.setattr(recorder, "stale", lambda: False)
    monkeypatch.setattr(recorder.subprocess, "run",
                        lambda *a, **k: FakeRun())
    monkeypatch.setattr(recorder.subprocess, "Popen",
                        lambda cmd, **k: calls.append(cmd) or type(
                            "P", (), {"pid": 1})())
    r = recorder.start()
    assert r["ok"], r
    kicks = [c for c in calls if c[0] == "/usr/bin/afplay"]
    assert kicks, "start() never played the silent writer-gate kick"
    assert kicks[0][1].endswith("silence.wav")

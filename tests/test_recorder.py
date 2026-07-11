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

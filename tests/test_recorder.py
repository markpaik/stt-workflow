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

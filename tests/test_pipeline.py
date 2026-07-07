"""pipeline.process_file: scratch-cleanup guarantee on a failed conversion.
(The ASR/diarization stages themselves are exercised by tuning/qa, not here —
this only covers the fast, ML-free failure path.)"""
import pytest

from stt import audio, config, pipeline


def test_process_file_cleans_scratch_wav_on_conversion_failure(sandbox, monkeypatch):
    """A failed/partial ffmpeg conversion (disk full, corrupt source, killed
    mid-write) must still hit the scratch cleanup — leaking the WAV would
    accumulate disk usage across a run with several bad files, worsening the
    very problem that caused the failure."""
    def _boom(src, dst):
        dst.write_bytes(b"partial garbage")  # ffmpeg wrote something, then died
        raise RuntimeError("ffmpeg crashed mid-conversion")
    monkeypatch.setattr(audio, "to_wav16k", _boom)
    monkeypatch.setattr(audio, "duration_sec", lambda p: 10.0)

    src = config.PROJECT_DIR / "bad.m4a"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)

    leaked = list(config.WORK_DIR.glob("*.wav"))
    assert leaked == [], f"scratch wav leaked: {leaked}"

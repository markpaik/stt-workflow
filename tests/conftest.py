"""Shared fixture: run every test against a throwaway sandbox so no test can
touch real voiceprints, manifests, status, or transcripts."""
import pytest

from stt import config, control, jobs, manifest, rates, status


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    d = tmp_path
    (d / "meetings").mkdir()
    (d / "source").mkdir()
    (d / "voiceprints").mkdir()
    monkeypatch.setattr(config, "PROJECT_DIR", d)
    monkeypatch.setattr(config, "VOICEPRINTS_DIR", d / "voiceprints")
    monkeypatch.setattr(config, "MANIFEST_PATH", d / "manifest.json")
    monkeypatch.setattr(config, "CALIBRATION_LOG", d / "calibration.jsonl")
    monkeypatch.setattr(config, "WORK_DIR", d / "work")
    monkeypatch.setattr(config, "MEETINGS_DIR", d / "meetings")
    monkeypatch.setattr(config, "ICLOUD_DIR", d / "source")
    monkeypatch.setattr(status, "STATUS_PATH", d / "status.json")
    monkeypatch.setattr(control, "PAUSE_FLAG", d / "paused.flag")
    monkeypatch.setattr(rates, "RATES_LOG", d / "rates.jsonl")
    monkeypatch.setattr(jobs, "PATH", d / "queued_jobs.json")
    monkeypatch.setattr(jobs, "_LOCK", d / "queued_jobs.lock")
    monkeypatch.setattr(rates, "_cache", {"sig": None, "learned": None})
    monkeypatch.setattr(control, "_snap", {"t": 0.0, "pids": [], "mem_mb": 0})
    # HERMETIC process discovery: pgrep sees the REAL machine — without this,
    # stop_run() inside a test finds (and kills!) an actual running batch.
    # Tests exercising group logic use the sandboxed status.json pgid path.
    monkeypatch.setattr(control, "_parent_pids", lambda: [])
    return d

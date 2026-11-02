"""Shared fixture: run every test against a throwaway sandbox so no test can
touch real voiceprints, manifests, status, or transcripts."""
import pytest

from stt import config, control, holds, jobs, manifest, rates, status


def mfile(base, suffix):
    """A meeting artifact's path in the per-meeting-folder layout, creating
    the folder — what test fixtures use to build meetings on disk."""
    p = config.meeting_file(base, suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    d = tmp_path
    (d / "meetings").mkdir()
    (d / "source").mkdir()
    (d / "voiceprints").mkdir()
    monkeypatch.setattr(config, "PROJECT_DIR", d)
    monkeypatch.setattr(config, "VOICEPRINTS_DIR", d / "voiceprints")
    monkeypatch.setattr(config, "MANIFEST_PATH", d / "manifest.json")
    # the meeting-id index binds its path at import like MANIFEST_PATH, so
    # without this every test that renames, archives or backfills a sandbox
    # meeting would write the id map of REAL meeting names into the checkout
    monkeypatch.setattr(config, "MEETINGS_INDEX_PATH", d / "meetings_index.json")
    # the search index binds its path at import too, and it holds transcript
    # text: a sandbox search must index the sandbox, never the checkout
    monkeypatch.setattr(config, "SEARCH_DB", d / "search.db")
    monkeypatch.setattr(config, "CALIBRATION_LOG", d / "calibration.jsonl")
    monkeypatch.setattr(config, "WORK_DIR", d / "work")
    # housekeeping rotates config.LOG_DIR at batch start: a test that runs
    # run_batch.main must never truncate the checkout's own logs
    monkeypatch.setattr(config, "LOG_DIR", d / "logs")
    monkeypatch.setattr(config, "MEETINGS_DIR", d / "meetings")
    monkeypatch.setattr(config, "ICLOUD_DIR", d / "source")
    monkeypatch.setattr(config, "RECORDINGS_DIR", d / "recordings")
    monkeypatch.setattr(config, "MIC_SPEAKER", None)  # don't inherit the real user's setting
    # the Voice Memos sync runs at every batch start: a sandbox run must read
    # a synthetic library, never the real one, even with STT_HOME unset
    monkeypatch.setenv("STT_VOICEMEMOS_DIR", str(d / "vm_container"))
    monkeypatch.delenv("STT_VOICEMEMOS_STAGING", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_SYNC", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_SINCE", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_PLACES", raising=False)
    monkeypatch.setattr(status, "STATUS_PATH", d / "status.json")
    monkeypatch.setattr(status, "HISTORY_LOG", d / "results.jsonl")
    monkeypatch.setattr(control, "PAUSE_FLAG", d / "paused.flag")
    monkeypatch.setattr(control, "STOP_FLAG", d / "stopping.flag")
    monkeypatch.setattr(rates, "RATES_LOG", d / "rates.jsonl")
    monkeypatch.setattr(jobs, "PATH", d / "queued_jobs.json")
    monkeypatch.setattr(jobs, "_LOCK", d / "queued_jobs.lock")
    # queue holds bind their paths at import, so a test that parks a file was
    # writing the REAL holds.json — leaving a phantom hold on the developer's
    # own queue, and leaking that state into the next test run (a file "held"
    # by nobody is skipped by every automatic run, silently, forever)
    monkeypatch.setattr(holds, "PATH", d / "holds.json")
    monkeypatch.setattr(holds, "_LOCK", d / "holds.lock")
    monkeypatch.setattr(rates, "_cache", {"sig": None, "learned": None})
    monkeypatch.setattr(control, "_snap", {"t": 0.0, "pids": [], "mem_mb": 0})
    # HERMETIC process discovery: pgrep sees the REAL machine — without this,
    # stop_run() inside a test finds (and kills!) an actual running batch.
    # Tests exercising group logic use the sandboxed status.json pgid path.
    monkeypatch.setattr(control, "_parent_pids", lambda: [])
    return d


# The board meeting that showed "Voices 22" on the strip and "Voices 21" on its
# To finish row: 22 unnamed diarizer clusters, two of which carry one global
# unknown id (the diarizer split one person) and so both display "Speaker 68".
# Naming U068 names both clusters, so it is ONE voice: 21 voices in 22
# clusters. Four clusters are transcript-local "Voice N" labels with no uid.
# Shared by the gather_state test (tests/test_timeline_feed.py) and the strip
# test (tests/test_new_shell.py), so both count the same meeting.
SPLIT_VOICE_BASE = "Board Meeting 09232026"
SPLIT_VOICE_UID = "U068"


def split_voice_meeting(base=SPLIT_VOICE_BASE):
    """Write the meeting (json, txt, audio) and its unknown registry into the
    sandbox. Returns (clusters, voices): 22 and 21."""
    import json

    from stt import unknowns
    uids = ["U068", "U068"] + [f"U{n:03d}" for n in range(70, 86)]
    speakers = [{"id": f"SPEAKER_{i:02d}", "name": None, "global_id": u,
                 "display": unknowns.display(u)} for i, u in enumerate(uids)]
    speakers += [{"id": f"SPEAKER_{i:02d}", "name": None, "global_id": None,
                  "display": f"Voice {i - 17}"} for i in range(18, 22)]
    segments = [{"start": i * 10.0, "end": i * 10.0 + 4.0, "speaker": s["id"],
                 "display": s["display"], "text": f"line from {s['id']}"}
                for i, s in enumerate(speakers)]
    d = {"source_file": f"{base}.m4a", "duration_sec": 7200.0,
         "date": "2026-09-23", "speakers": speakers, "segments": segments,
         "words": []}
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    reg = {"speakers": {u: {"file": f"{u}.npy", "meetings": [base]}
                        for u in dict.fromkeys(uids)}}
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps(reg))
    return len(speakers), len({s["display"] for s in speakers})


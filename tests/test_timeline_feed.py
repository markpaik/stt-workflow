"""The One Timeline server feed: one row per meeting/file that changes state in
place, plus the ranked attention tray. Fixtures fabricate the same status /
holds / history / registry files the pipeline writes at runtime (mirroring
test_run_batch.py, test_naming_inbox.py, test_review.py), then assert the join
gather_state performs over them.

The feed is STRICTLY ADDITIVE: every pre-existing /api/state key keeps its shape
(test_additive_keys), so the current panel keeps working while the redesign lands.
"""
import json
import time

from gui import server as srv
from stt import archive, config, control, holds, recorder, status, unknowns
from conftest import mfile

# the top-level /api/state keys that existed BEFORE the timeline/tray were added
BASELINE_KEYS = {
    'active', 'archived_count', 'asr_choices', 'battery', 'cloud_keys',
    'enrolled', 'llm_available', 'llm_backend', 'llm_backends', 'max_samples',
    'meetings', 'mem_mb', 'mic_speaker', 'model', 'overall_eta_sec', 'paths',
    'paused', 'pending', 'punctuate', 'queue', 'queued_jobs', 'rates', 'recent',
    'recorder_note', 'recorder_ready', 'recording', 'relabel_pending', 'running',
    'schedule', 'unknowns'}


# ---------- fixtures that write the runtime files ----------

def _meeting(base, *, date="2026-05-01", segments=None, **extra):
    """A processed meeting on disk (json + txt + audio), like test_naming_inbox."""
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": date,
         "speakers": [{"id": "SPEAKER_00", "display": "Alex Rivera"}],
         "segments": segments or [], "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    return d


def _source(name, held=False, body=b"\x00" * 4096):
    """A watched source file waiting in the queue (optionally held)."""
    p = config.source_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    if held:
        holds.hold(name)
    return p


def _history(*entries):
    """Write the permanent results log directly, newest LAST (file order), and
    bust the server's mtime cache so the fresh file is re-read this poll."""
    status.HISTORY_LOG.write_text("".join(json.dumps(e) + "\n" for e in entries))
    srv._results_cache["key"] = None


def _recording(monkeypatch, tmp_path, *, stalled=False, paused=False):
    """A live capture the way recorder.start() records it — a real (or header-
    only) CAF plus the status entry — with the pid check stubbed live."""
    caf = tmp_path / ".rec-abc12345.caf"
    caf.write_bytes(b"\x00" * (100 if stalled else 20000))  # <8192 == header-only
    status.set_recording({
        "pid": 4242, "caf": str(caf), "started_at": "2026-07-11T18:14:07",
        "started_monotonic": time.monotonic() - 30,  # 30s in: past STALL_AFTER_SECS
        "paused": paused, "paused_total": 0.0})
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: True)


def _running(monkeypatch, pids=(4242,)):
    """Make gather_state believe a batch is in flight (drives the active feed)."""
    monkeypatch.setattr(control, "snapshot",
                        lambda max_age=1.5: {"pids": list(pids), "mem_mb": 100})


def _row(st, *, id=None, source_file=None):
    for r in st["timeline"]:
        if (id is not None and r["id"] == id) or \
           (source_file is not None and r.get("source_file") == source_file):
            return r
    return None


# ---------- the seven states ----------

def test_recording_state(sandbox, monkeypatch, tmp_path):
    _recording(monkeypatch, tmp_path)
    r = next(x for x in srv.gather_state()["timeline"] if x["state"] == "recording")
    assert r["id"].startswith("rec:")
    assert r["elapsed_secs"] >= 8 and r["paused"] is False and r["stalled"] is False


def test_waiting_state(sandbox):
    _source("Team Standup 07112026 0900.wav")
    r = _row(srv.gather_state(), id="src:Team Standup 07112026 0900.wav")
    assert r["state"] == "waiting"
    assert r["title"] == "Team Standup 07112026 0900.wav"      # raw filename
    assert r["date"] == "2026-07-11" and "size_mb" in r        # date parsed from name
    assert r["source_file"] == "Team Standup 07112026 0900.wav"


def test_held_state(sandbox):
    _source("Draft Memo 07112026.wav", held=True)
    r = _row(srv.gather_state(), id="src:Draft Memo 07112026.wav")
    assert r["state"] == "held" and r["held"] is True and "size_mb" in r


def test_processing_state(sandbox, monkeypatch):
    _source("LT Weekly Meeting.m4a")
    status.set_stage("LT Weekly Meeting.m4a", "transcribing",
                     duration=600.0, progress=0.5, base="LT Weekly Meeting 06042026")
    _running(monkeypatch)
    r = _row(srv.gather_state(), source_file="LT Weekly Meeting.m4a")
    assert r["state"] == "processing"
    assert r["id"] == "LT Weekly Meeting 06042026"     # the resolved, announced base
    assert r["stage"] == "transcribing" and r["pct"] is not None


def test_needs_name_state(sandbox):
    _meeting("Recording 07102026 0915", date="2026-07-10", reviewed=False,
             ai_title="Budget Planning Cadence")
    r = _row(srv.gather_state(), id="Recording 07102026 0915")
    assert r["state"] == "needs_name"
    assert r["suggested_title"] == "Budget Planning Cadence"
    assert r["suggested_date"] == "2026-07-10" and r["has_audio"] is True


def test_ready_state(sandbox):
    _meeting("Weekly Check-in 05012026", date="2026-05-01", category="work",
             ai_summary="The team agreed to pilot monthly budgets. More detail here.",
             segments=[{"start": 0.0, "end": 3.0, "text": "a b c d e",
                        "flags": ["id_mismatch"]},                    # substantial
                       {"start": 3.0, "end": 3.4, "text": "ok",
                        "flags": ["overlap"]}])                       # minor crumb
    r = _row(srv.gather_state(), id="Weekly Check-in 05012026")
    assert r["state"] == "ready"
    assert r["title"] == "Weekly Check-in"                            # stamp stripped
    assert r["category"] == "work" and r["has_summary"] is True
    assert r["review_substantial"] == 1 and r["review_minor"] == 1
    # two sentences carried through — the preview clamps to ~two lines, not one
    assert r["summary"] == \
        "The team agreed to pilot monthly budgets. More detail here."


def test_failed_state_from_history(sandbox):
    """A file that failed and left the watched folder still shows as failed —
    the failure is not lost the moment the source is gone."""
    _history({"name": "Truncated.mp4", "ok": False,
              "summary": "ffmpeg: moov atom not found", "at": "2026-07-09T02:41:55"})
    r = _row(srv.gather_state(), id="src:Truncated.mp4")
    assert r["state"] == "failed" and "moov atom" in r["error"]
    assert "retry" in r["retry_note"].lower()


def test_failed_state_for_a_still_queued_source(sandbox):
    """The original stays in the watched folder and re-runs — a queued source
    whose last result was a failure reads as failed, not waiting."""
    _source("Truncated 07112026.mp4")
    _history({"name": "Truncated 07112026.mp4", "ok": False,
              "summary": "decode error", "at": "2026-07-09T02:41:55"})
    r = _row(srv.gather_state(), id="src:Truncated 07112026.mp4")
    assert r["state"] == "failed"
    assert "watched folder" in r["retry_note"]


def test_a_recovered_source_is_not_failed(sandbox):
    """Most-recent result wins: a source that failed once then succeeded must
    not linger as failed (mirrors status.history's newest-wins merge)."""
    _history({"name": "Flaky.m4a", "ok": False, "summary": "boom", "at": "2026-07-01T10:00:00"},
             {"name": "Flaky.m4a", "ok": True, "summary": "1 speaker", "at": "2026-07-02T10:00:00"})
    assert _row(srv.gather_state(), id="src:Flaky.m4a") is None


# ---------- the summary preview (two sentences, hard cap) ----------

def test_preview_two_sentences_and_cap():
    """The ready-row summary preview carries ~two lines: the first two sentences
    when they fit, else a hard cap that ends with an ellipsis."""
    p = srv._preview
    # empty in, empty out
    assert p("") == "" and p(None) == ""
    # two sentences that fit come through whole
    assert p("First point made. Second point too.") == \
        "First point made. Second point too."
    # a third sentence is dropped — exactly two, no trailing space
    assert p("One here. Two here. Three here.") == "One here. Two here."
    # one long unbroken sentence is hard-capped to ~320 chars with an ellipsis
    out = p("word " * 100)                     # 499 chars, no sentence break
    assert len(out) == 320 and out.endswith("…")
    # whitespace (incl. newlines) collapses to single spaces first
    assert p("A\n\n b.   C d.") == "A b. C d."


# ---------- the identity handoff ----------

def test_source_to_base_handoff_flips_id_and_keeps_source_file(sandbox, monkeypatch):
    """A source that gains an announced base mid-run flips its id from
    src:<file> to the resolved base, keeps source_file so the client morphs the
    row in place, and is never duplicated as a lingering waiting row."""
    src = "LT Weekly Meeting.m4a"
    _source(src)

    # before the run: a plain waiting row, identified by src:<file>
    before = _row(srv.gather_state(), source_file=src)
    assert before["state"] == "waiting" and before["id"] == f"src:{src}"

    # the batch announces the resolved (date-stamped) base via set_stage
    status.set_stage(src, "transcribing", duration=600.0,
                     base="LT Weekly Meeting 06042026")
    _running(monkeypatch)
    after = _row(srv.gather_state(), source_file=src)
    assert after["state"] == "processing"
    assert after["id"] == "LT Weekly Meeting 06042026"    # id flipped
    assert after["prev_id"] == f"src:{src}"               # the flip is spelled out
    # and exactly one row owns this source (no leftover waiting duplicate)
    assert sum(1 for r in srv.gather_state()["timeline"]
               if r.get("source_file") == src) == 1


def test_reprocessed_meeting_shows_processing_not_twice(sandbox, monkeypatch):
    """A Redo: the meeting already exists AND is in flight. It appears once, as
    its processing row, not also as a stale ready row."""
    _meeting("Board Prep 07022026", date="2026-07-02")
    status.set_stage("Board Prep 07022026.m4a", "diarizing", duration=600.0,
                     base="Board Prep 07022026")
    _running(monkeypatch)
    rows = [r for r in srv.gather_state()["timeline"] if r["id"] == "Board Prep 07022026"]
    assert len(rows) == 1 and rows[0]["state"] == "processing"


# ---------- archived exclusion ----------

def test_archived_meeting_is_excluded(sandbox):
    _meeting("Old Sync 03042025", date="2025-03-04")
    assert _row(srv.gather_state(), id="Old Sync 03042025") is not None
    archive.archive_meeting("Old Sync 03042025")
    assert _row(srv.gather_state(), id="Old Sync 03042025") is None
    assert srv.gather_state()["timeline"] == []


# ---------- the tray ----------

def test_tray_empty_when_nothing_needs_attention(sandbox):
    _meeting("Clean Meeting 05012026")   # ready, no flags, no unknowns, no failures
    assert srv.gather_state()["tray"] == []


def test_tray_unknown_voice_excludes_hidden(sandbox):
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U007": {"file": "U007.npy", "meetings": ["Vendor Demo 01152026"]},
        "U012": {"file": "U012.npy", "meetings": ["Board Prep 09102025"], "archived": True},
        "U013": {"file": "U013.npy", "meetings": ["X"], "dropped": "2026-01-01T00:00:00"}}}))
    tray = srv.gather_state()["tray"]
    voices = [t for t in tray if t["kind"] == "unknown_voice"]
    assert [t["target"] for t in voices] == ["U007"]     # archived + dropped excluded
    assert voices[0]["count"] == 1


def test_tray_review_only_counts_substantial(sandbox):
    _meeting("Flagged 04082026", date="2026-04-08",
             segments=[{"start": 0.0, "end": 3.0, "text": "a b c d e", "flags": ["x"]},
                       {"start": 3.0, "end": 5.0, "text": "f g h i j", "flags": ["y"]},
                       {"start": 5.0, "end": 5.3, "text": "ok", "flags": ["z"]}])  # minor
    tray = srv.gather_state()["tray"]
    rev = next(t for t in tray if t["kind"] == "review")
    assert rev["count"] == 2 and rev["target"] == "Flagged 04082026"


def test_tray_ranks_stall_failed_review_unknown(sandbox, monkeypatch, tmp_path):
    """All four kinds at once must come back in strict rank order."""
    _recording(monkeypatch, tmp_path, stalled=True)                   # recorder_stall
    _source("Broken.mp4")
    _history({"name": "Broken.mp4", "ok": False, "summary": "decode error",
              "at": "2026-07-09T02:41:55"})                           # failed
    _meeting("Flagged 04082026", date="2026-04-08",                   # review
             segments=[{"start": 0.0, "end": 3.0, "text": "a b c d e", "flags": ["x"]}])
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U007": {"file": "U007.npy", "meetings": ["m"]}}}))           # unknown_voice
    kinds = [t["kind"] for t in srv.gather_state()["tray"]]
    assert kinds == ["recorder_stall", "failed", "review", "unknown_voice"]


def test_tray_failed_carries_error_and_target(sandbox):
    _history({"name": "Broken.mp4", "ok": False,
              "summary": "moov atom not found", "at": "2026-07-09T02:41:55"})
    t = next(x for x in srv.gather_state()["tray"] if x["kind"] == "failed")
    assert t["detail"] == "moov atom not found" and t["target"] == "src:Broken.mp4"


# ---------- ordering + additivity ----------

def test_timeline_newest_first_with_active_pinned(sandbox, monkeypatch, tmp_path):
    """recording + processing pin to the top; the rest sort newest-first (by
    `when`: processed_at for meetings, file mtime for queued sources)."""
    import datetime
    import os
    _meeting("Older 01012025", date="2025-01-01", processed_at="2025-01-01T10:00:00")
    _meeting("Newer 06012026", date="2026-06-01", processed_at="2026-06-01T10:00:00")
    src = _source("Queued 07112026.wav")                 # newest -> above both meetings
    ts = datetime.datetime(2026, 7, 11, 9, 0, 0).timestamp()
    os.utime(src, (ts, ts))
    status.set_stage("Proc.m4a", "transcribing", duration=600.0, base="Proc 05012026")
    _recording(monkeypatch, tmp_path)
    _running(monkeypatch)
    states = [r["state"] for r in srv.gather_state()["timeline"]]
    assert states[0] == "recording" and states[1] == "processing"
    # among the rest, the newest (today's queued source) leads the older meetings
    rest = [r for r in srv.gather_state()["timeline"]
            if r["state"] not in ("recording", "processing")]
    assert [r["id"] for r in rest] == \
        ["src:Queued 07112026.wav", "Newer 06012026", "Older 01012025"]


def test_additive_keys(sandbox):
    _meeting("Some Meeting 05012026")
    st = srv.gather_state()
    # every pre-existing top-level key still present, unchanged type
    assert BASELINE_KEYS <= set(st)
    assert set(st) == BASELINE_KEYS | {"timeline", "tray"}
    assert isinstance(st["running"], bool)
    assert isinstance(st["meetings"], list) and isinstance(st["queue"], list)
    assert isinstance(st["active"], dict) and isinstance(st["unknowns"], list)
    # the two new keys are the promised lists
    assert isinstance(st["timeline"], list) and isinstance(st["tray"], list)

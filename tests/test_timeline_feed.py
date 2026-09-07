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
from stt import (archive, config, control, holds, manifest, recorder, status,
                 summarize, unknowns)
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


def test_needs_name_row_carries_the_review_card(sandbox):
    """The naming row is a review card: the original filename, how long, who was
    heard, what it was about, and which of those voices are still unnamed. That
    is the evidence a human names a meeting from, and the unnamed map is what
    lets the row open the naming panel for a voice the registry never tracked."""
    _meeting("Recording 07102026 0915", date="2026-07-10", reviewed=False,
             ai_title="Budget Planning Cadence",
             ai_summary="Cabinet walked the FY27 gap. Two options survive.",
             speakers=[{"id": "SPEAKER_00", "display": "Alex Rivera",
                        "name": "Alex Rivera"},
                       {"id": "SPEAKER_01", "display": "Speaker 2"},
                       # an unnamed cluster whose display already IS its raw id
                       # (a "Voice N" fallback label with no clean int suffix):
                       # not a real naming target, must not enter the map
                       {"id": "Voice 3", "display": "Voice 3"}])
    r = _row(srv.gather_state(), id="Recording 07102026 0915")
    assert r["state"] == "needs_name"
    # the filename the user saved: the panel offers its stem as the name
    assert r["source_file"] == "Recording 07102026 0915.m4a"
    assert r["minutes"] == 10
    assert r["speakers"] == ["Alex Rivera", "Speaker 2", "Voice 3"]
    assert r["summary"] == "Cabinet walked the FY27 gap. Two options survive."
    # only the unnamed voice whose id differs from its display maps, and it
    # maps to its cluster id; the named speaker and the id==display entry
    # are both excluded
    assert r["unnamed_clusters"] == {"Speaker 2": "SPEAKER_01"}


# ---------- "not a real speaker", per meeting ----------
#
# Some clusters are grumbling, a cough, music, a hallway echo. Naming them is
# wrong, but their lines are real audio, so the answer is "leave it as unknown
# and stop asking": the attribution is untouched and only the "?" prompts go.

DVOICES = [{"id": "SPEAKER_00", "display": "Alex Rivera", "name": "Alex Rivera"},
           {"id": "SPEAKER_01", "display": "Speaker 2"},
           {"id": "SPEAKER_02", "display": "Speaker 3"}]


def test_dismissed_voice_drops_out_of_the_naming_prompts(sandbox):
    _meeting("Grumble 07102026", speakers=[dict(s) for s in DVOICES],
             segments=[{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_02",
                        "text": "mmhm"}])
    j = config.meeting_file("Grumble 07102026", ".json")
    assert summarize.dismiss_voice("Grumble 07102026", "SPEAKER_02") == {
        "ok": True, "dismissed": ["SPEAKER_02"]}
    d = json.loads(j.read_text())
    # TOP-LEVEL, not a field on the speaker entry: relabel rebuilds "speakers"
    # wholesale, so anything parked in there would not survive the next one
    assert d["dismissed_voices"] == ["SPEAKER_02"]
    # the transcript is untouched: the segment still belongs to that cluster,
    # and the speaker still appears with its label
    assert d["segments"][0]["speaker"] == "SPEAKER_02"
    assert [s["display"] for s in d["speakers"]] == \
        ["Alex Rivera", "Speaker 2", "Speaker 3"]

    meta = srv._meeting_meta(j, config.meetings_dir())
    assert meta["unnamed_clusters"] == {"Speaker 2": "SPEAKER_01"}
    assert meta["dismissed_voices"] == ["SPEAKER_02"]
    assert meta["speakers"] == ["Alex Rivera", "Speaker 2", "Speaker 3"]


def test_dismissal_survives_a_relabel_shaped_rewrite(sandbox):
    """relabel.py and review._rewrite both rebuild speakers/segments/words and
    pass every OTHER top-level key through. That passthrough is the whole reason
    the list lives at the top level, so drive it here."""
    from stt import output
    _meeting("Relabeled 07102026", speakers=[dict(s) for s in DVOICES])
    summarize.dismiss_voice("Relabeled 07102026", "SPEAKER_02")
    j = config.meeting_file("Relabeled 07102026", ".json")
    data = json.loads(j.read_text())
    output.write_json(j, {k: v for k, v in data.items()
                          if k not in ("speakers", "segments", "words")},
                      data["speakers"], data["segments"], data["words"])
    assert json.loads(j.read_text())["dismissed_voices"] == ["SPEAKER_02"]


def test_dismissal_can_be_undone(sandbox):
    _meeting("Undo 07102026", speakers=[dict(s) for s in DVOICES])
    summarize.dismiss_voice("Undo 07102026", "SPEAKER_02")
    summarize.dismiss_voice("Undo 07102026", "SPEAKER_01")
    assert summarize.dismiss_voice("Undo 07102026", "SPEAKER_01")["dismissed"] == \
        ["SPEAKER_01", "SPEAKER_02"]      # idempotent, sorted
    assert summarize.restore_voice("Undo 07102026", "SPEAKER_01")["dismissed"] == \
        ["SPEAKER_02"]
    assert summarize.restore_voice("Undo 07102026", "SPEAKER_02")["dismissed"] == []
    # emptied means the key goes, not an empty list left lying around
    assert "dismissed_voices" not in json.loads(
        config.meeting_file("Undo 07102026", ".json").read_text())
    srv._meet_cache.clear()               # meta is mtime-cached; force a re-read
    meta = srv._meeting_meta(config.meeting_file("Undo 07102026", ".json"),
                             config.meetings_dir())
    assert meta["unnamed_clusters"] == {"Speaker 2": "SPEAKER_01",
                                        "Speaker 3": "SPEAKER_02"}


def test_dismissal_refuses_a_made_up_id_or_meeting(sandbox):
    # never store an id that matches nothing: the list would slowly fill with
    # junk from a stale client and silence nothing
    _meeting("Junk 07102026", speakers=[dict(s) for s in DVOICES])
    r = summarize.dismiss_voice("Junk 07102026", "SPEAKER_99")
    assert not r["ok"] and "SPEAKER_99" in r["error"]
    assert not summarize.dismiss_voice("Junk 07102026", "")["ok"]
    assert "dismissed_voices" not in json.loads(
        config.meeting_file("Junk 07102026", ".json").read_text())
    assert not summarize.dismiss_voice("No Such Meeting", "SPEAKER_00")["ok"]
    assert not summarize.restore_voice("No Such Meeting", "SPEAKER_00")["ok"]


def test_a_redo_deliberately_forgets_dismissals(sandbox, monkeypatch, tmp_path):
    """Unlike the date and the category, this is NOT carried across a Redo.
    process_file re-clusters, so 'SPEAKER_02' afterwards is a different voice
    than 'SPEAKER_02' before: carrying the list would silence the wrong one."""
    import subprocess

    from stt import pipeline
    from stt.audio import FFMPEG
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)

    src = tmp_path / "Redo Voice 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-ac", "1", "-c:a", "aac",
                    str(src)], check=True, capture_output=True)
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                          do_verify=False)
    base = "Redo Voice 05012026"
    # a diarize-free run has no clusters, so plant one to dismiss (the Redo is
    # what is under test, not the mutator)
    j = config.meeting_file(base, ".json")
    d = json.loads(j.read_text())
    d["speakers"] = [{"id": "SPEAKER_02", "display": "Speaker 3"}]
    j.write_text(json.dumps(d))
    assert summarize.dismiss_voice(base, "SPEAKER_02")["ok"]
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                          do_verify=False)   # the Redo
    assert "dismissed_voices" not in json.loads(
        config.meeting_file(base, ".json").read_text())


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


def test_redropped_source_keeps_its_waiting_row_beside_the_meeting(sandbox):
    """A file already processed into a meeting, dropped back into the watched
    folder: the mtime is new, so the manifest reads it as brand new and the
    batch WILL transcribe it a second time. Both rows show -- the meeting, and
    the waiting row carrying the duplicate chip -- because the meeting owning
    that source name would otherwise hide work that is really about to run."""
    _meeting("Weekly Sync 07012026", date="2026-07-01")   # source_file: <base>.m4a
    _source("Weekly Sync 07012026.m4a")
    st = srv.gather_state()
    assert _row(st, id="Weekly Sync 07012026") is not None       # the meeting
    r = _row(st, id="src:Weekly Sync 07012026.m4a")              # and the re-drop
    assert r is not None and r["state"] == "waiting"
    # the chip says which meeting it matched, and on which (weaker) signal
    assert r["dup_of"] == "Weekly Sync 07012026" and r["dup_reason"] == "name"


def test_processed_source_still_in_the_folder_has_no_waiting_row(sandbox):
    """The inverse guard: the SAME file, but the manifest records it processed
    at this exact mtime. Nothing will re-run it, so it is only its meeting --
    a lingering source never doubles the feed."""
    base = "Weekly Sync 07012026"
    _meeting(base, date="2026-07-01")
    src = _source(f"{base}.m4a")
    m = manifest.load()
    manifest.mark(m, src.name, src.stat().st_mtime,
                  [str(mfile(base, ".json")), str(mfile(base, ".txt"))])
    manifest.save(m)
    st = srv.gather_state()
    assert _row(st, id=base) is not None
    assert _row(st, id=f"src:{base}.m4a") is None


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
    for b in ("Vendor Demo 01152026", "Board Prep 09102025", "X"):
        _meeting(b)   # refs must resolve, or the zero-ref policy hides the voice
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U007": {"file": "U007.npy", "meetings": ["Vendor Demo 01152026"]},
        "U012": {"file": "U012.npy", "meetings": ["Board Prep 09102025"], "archived": True},
        "U013": {"file": "U013.npy", "meetings": ["X"], "dropped": "2026-01-01T00:00:00"}}}))
    tray = srv.gather_state()["tray"]
    voices = [t for t in tray if t["kind"] == "unknown_voice"]
    assert [t["target"] for t in voices] == ["U007"]     # archived + dropped excluded
    assert voices[0]["count"] == 1


def test_tray_and_panel_exclude_zero_ref_unknowns(sandbox):
    """forget_meeting_refs deliberately keeps an unknown at zero refs (the
    embedding still identifies the voice later), and a ref can also die
    without a scrub (pre-fix deletes). Neither may nag: a voice with no
    surviving meeting has nothing to play and nothing to review, so it is
    hidden from the Speakers panel AND the tray — not deleted."""
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U011": {"file": "U011.npy", "meetings": []},              # scrubbed empty
        "U012": {"file": "U012.npy", "meetings": ["Gone Mtg"]}}}))  # dead ref
    st = srv.gather_state()
    assert st["unknowns"] == []
    assert [t for t in st["tray"] if t["kind"] == "unknown_voice"] == []
    # hidden, NOT reaped: the registry entries survive for future matching
    assert set(unknowns.load()["speakers"]) == {"U011", "U012"}


def test_unknown_count_matches_resolvable_meetings_only(sandbox):
    """'heard in N meetings' may only count meetings that still exist — live
    or archived — so the count and what the naming dialog can actually offer
    can never disagree again (U005/U007/U009: 'heard in 2 meetings', zero
    playable clips)."""
    _meeting("Live Mtg 05012026")
    _meeting("Shelved Mtg 05012026")
    archive.archive_meeting("Shelved Mtg 05012026")
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U005": {"file": "U005.npy", "meetings":
                 ["Live Mtg 05012026", "Deleted Mtg", "Shelved Mtg 05012026"]}}}))
    st = srv.gather_state()
    u = next(x for x in st["unknowns"] if x["uid"] == "U005")
    assert u["meetings"] == ["Live Mtg 05012026", "Shelved Mtg 05012026"]
    nag = next(t for t in st["tray"] if t["kind"] == "unknown_voice")
    assert nag["count"] == len(u["meetings"]) == 2
    # the invariant itself: every meeting the client sees resolves on disk
    resolvable = set(config.meeting_bases()) | set(config.archived_bases())
    for x in st["unknowns"]:
        assert set(x["meetings"]) <= resolvable


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
    _meeting("m")   # the unknown's ref must resolve to a real meeting
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
    # relabel_running joined them later (the honest "a relabel is happening
    # right now" signal next to the queued-behind-another relabel_pending)
    assert set(st) == BASELINE_KEYS | {"timeline", "tray", "relabel_running"}
    assert isinstance(st["relabel_running"], bool)
    assert isinstance(st["running"], bool)
    assert isinstance(st["meetings"], list) and isinstance(st["queue"], list)
    assert isinstance(st["active"], dict) and isinstance(st["unknowns"], list)
    # the two new keys are the promised lists
    assert isinstance(st["timeline"], list) and isinstance(st["tray"], list)


def test_state_reports_a_relabel_that_is_actually_running(sandbox):
    """Naming a voice spawns a relabel that rewrites every transcript, and the
    panel has to say so. relabel_pending alone could not: it is only written
    when a SECOND relabel finds the lock held, so the ordinary case (nothing
    else running) showed no progress at all and the names appeared minutes
    later out of nowhere."""
    _meeting("Some Meeting 05012026")
    assert srv.gather_state()["relabel_running"] is False
    with control.relabel_marker():
        assert srv.gather_state()["relabel_running"] is True
    assert srv.gather_state()["relabel_running"] is False


def test_dupe_scan_never_kicks_while_a_batch_is_running(sandbox, monkeypatch):
    """The duplicate-transcript scan reads the whole library, so it is only
    ever kicked off an IDLE poll (see _kick_dupe_scan's caller in
    gather_state). Two meetings so it would have something to compare, and a
    call counter on _kick_dupe_scan itself rather than the real thread
    machinery, which is exercised elsewhere."""
    _meeting("A Mtg 05012026")
    _meeting("B Mtg 05012026")
    calls = []
    monkeypatch.setattr(srv, "_kick_dupe_scan", lambda: calls.append(1))

    _running(monkeypatch)                 # a batch is in flight
    srv.gather_state()
    assert calls == [], "the scan must not be kicked while a batch is running"

    monkeypatch.setattr(control, "snapshot",
                        lambda max_age=1.5: {"pids": [], "mem_mb": 0})
    srv.gather_state()
    assert calls == [1], "an idle poll is exactly when the scan should be kicked"


def test_the_pure_queue_helper_never_kicks_anything(sandbox, monkeypatch):
    """/api/queue_delete_dupes derives its file list through _queue_and_dupes
    precisely because gather_state kicks the next batch and the dupe scan as
    side effects: a delete request must never be the thing that starts a run.
    gather_state keeps those side effects, one layer up."""
    calls = []
    monkeypatch.setattr(srv, "_kick_jobs", lambda: calls.append("jobs"))
    monkeypatch.setattr(srv, "_kick_dupe_scan", lambda: calls.append("scan"))
    monkeypatch.setattr(srv, "_spawn", lambda *a, **k: calls.append("spawn"))
    _meeting("Some Meeting 05012026")
    srv._queue_and_dupes()
    assert calls == [], "the pure helper spawned or kicked something"
    srv.gather_state()
    assert "jobs" in calls and "scan" in calls, \
        "the side effects belong to gather_state, and must still happen there"


def test_two_concurrent_kicks_run_one_scan(sandbox, monkeypatch):
    """The busy flag's check-then-set spans real file I/O; unguarded, two 2s
    polls from two tabs could both pass it and run two full-library scans."""
    import threading as _t
    _meeting("A Mtg 05012026")
    _meeting("B Mtg 05012026")
    started, release = [], _t.Event()

    def slow_refresh():
        started.append(1)
        release.wait(2)
    monkeypatch.setattr(srv, "_dupe_refresh", slow_refresh)
    barrier = _t.Barrier(2)

    def kick():
        barrier.wait()
        srv._kick_dupe_scan()
    ts = [_t.Thread(target=kick) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for _ in range(100):                    # let the one worker thread start
        if started:
            break
        _t.Event().wait(0.02)
    release.set()
    for _ in range(100):                    # and drain it
        with srv._dupe_lock:
            if not srv._dupe_worker["busy"]:
                break
        _t.Event().wait(0.02)
    assert started == [1], "exactly one scan may run at a time"


def test_rescan_respects_the_single_flight_guard(sandbox, monkeypatch):
    """/api/dupe_scan used to bypass the busy flag entirely and run a third
    concurrent scan. It now answers False while one is running (the drawer
    keeps polling) and runs under the same guard otherwise."""
    with srv._dupe_lock:
        srv._dupe_worker["busy"] = True
    try:
        assert srv._force_dupe_scan() is False
    finally:
        with srv._dupe_lock:
            srv._dupe_worker["busy"] = False
    calls = []
    monkeypatch.setattr(srv, "_dupe_refresh", lambda: calls.append(1))
    assert srv._force_dupe_scan() is True
    assert calls == [1]


def test_a_gone_source_failure_is_honest_and_dismissible(sandbox, monkeypatch):
    """A failure whose source file no longer exists rendered under a hardcoded
    "original stays in the watched folder" line, with an X that called the
    file-delete endpoint, which refused because there was no file: a permanent
    ghost row. The row now says the original is gone (gone: true), and
    dismissing it filters every PAST failure for that name while a NEWER
    failure still surfaces."""
    monkeypatch.setattr(srv, "_results_rows", lambda: [
        {"name": "ghost.m4a", "at": "2026-08-20T10:00:00", "ok": False,
         "summary": "[Errno 2] No such file or directory"}])
    st = srv.gather_state()
    row = [r for r in st["timeline"] if r.get("id") == "src:ghost.m4a"][0]
    assert row["state"] == "failed" and row.get("gone") is True
    assert "the original is gone" in row["retry_note"]

    assert srv.dismiss_failure("ghost.m4a") is True
    st = srv.gather_state()
    assert not [r for r in st["timeline"] if r.get("id") == "src:ghost.m4a"], \
        "a dismissed failure must leave the timeline"

    # a NEWER failure for the same name resurfaces: dismissal silences the
    # past, never the file
    monkeypatch.setattr(srv, "_results_rows", lambda: [
        {"name": "ghost.m4a", "at": "2126-01-01T00:00:00", "ok": False,
         "summary": "failed again"}])
    st = srv.gather_state()
    assert [r for r in st["timeline"] if r.get("id") == "src:ghost.m4a"]

    # junk names are refused, nothing stored
    assert srv.dismiss_failure("../../etc/passwd") is False
    assert srv.dismiss_failure("") is False


# ---------- fix pass 2 (2026-09-06) ----------

def test_the_tray_failed_line_carries_the_gone_flag(sandbox, monkeypatch):
    """R5: the gone-source fix landed on the timeline row but not on the tray
    entry built from it, so the tray still offered Retry for a file that is no
    longer on disk -- a POST to /api/run for nothing."""
    monkeypatch.setattr(srv, "_results_rows", lambda: [
        {"name": "ghost.m4a", "at": "2026-08-20T10:00:00", "ok": False,
         "summary": "boom"}])
    st = srv.gather_state()
    entry = [t for t in st["tray"] if t["kind"] == "failed"][0]
    assert entry["gone"] is True, "the tray entry dropped the row's gone flag"

    # a failure whose source IS still in the folder keeps gone false, so the
    # tray goes on offering the retry that actually works
    _source("still here.m4a")
    monkeypatch.setattr(srv, "_results_rows", lambda: [
        {"name": "still here.m4a", "at": "2026-08-20T10:00:00", "ok": False,
         "summary": "boom"}])
    st = srv.gather_state()
    entry = [t for t in st["tray"] if t["kind"] == "failed"][0]
    assert entry["gone"] is False


def test_a_duplicate_of_an_archived_meeting_says_so_and_resolves_its_title(
        sandbox, monkeypatch):
    """R7: dup_of can name an ARCHIVED meeting (the duplicate gate spans live
    plus archived by design), but the title map is built from LIVE metas only.
    The row fell back to the raw stamped base and linked it into the meeting
    view, which renders live meetings only: a dead link under a permanent
    "Loading meeting" spinner."""
    from stt import archive, dupes
    _meeting("Board Prep 05012026")
    assert archive.archive_meeting("Board Prep 05012026")["ok"]
    _source("board prep.m4a")
    monkeypatch.setattr(dupes, "source_duplicates",
                        lambda *a, **k: {"board prep.m4a": {
                            "base": "Board Prep 05012026", "reason": "name"}})

    queue, _, _ = srv._queue_and_dupes()
    row = [f for f in queue if f["name"] == "board prep.m4a"][0]
    assert row["dup_of"] == "Board Prep 05012026"
    assert row["dup_archived"] is True
    assert row["dup_title"] == "Board Prep", \
        "the title must resolve from the archived meeting, not the raw base"

    st = srv.gather_state()
    tl = [r for r in st["timeline"] if r["id"] == "src:board prep.m4a"][0]
    assert tl["dup_archived"] is True and tl["dup_title"] == "Board Prep"


def test_a_live_duplicate_still_links_and_is_not_marked_archived(
        sandbox, monkeypatch):
    """R7 must not disarm the ordinary case: a LIVE match keeps its link."""
    from stt import dupes
    _meeting("Board Prep 05012026")
    _source("board prep.m4a")
    monkeypatch.setattr(dupes, "source_duplicates",
                        lambda *a, **k: {"board prep.m4a": {
                            "base": "Board Prep 05012026", "reason": "name"}})
    queue, _, _ = srv._queue_and_dupes()
    row = [f for f in queue if f["name"] == "board prep.m4a"][0]
    assert row["dup_title"] == "Board Prep" and not row.get("dup_archived")
    st = srv.gather_state()
    tl = [r for r in st["timeline"] if r["id"] == "src:board prep.m4a"][0]
    assert tl["dup_archived"] is False


def test_a_short_file_never_estimates_zero_minutes(sandbox, monkeypatch):
    """W26: a one-second file still carries the pipeline's fixed per-file
    overhead, but round() turned that into 0 -- and every downstream check is a
    plain truthiness test, so 0 read as NO estimate. A running batch with only
    short files pending then reported no ETA anywhere on screen."""
    _source("blip.m4a")
    monkeypatch.setattr(srv, "_est_duration", lambda p: 1.0)
    queue, _, _ = srv._queue_and_dupes()
    row = [f for f in queue if f["name"] == "blip.m4a"][0]
    assert row["est_min"] >= 1, "a real estimate must never round down to zero"

    # and it reaches the row instead of vanishing into a falsy 0
    st = srv.gather_state()
    tl = [r for r in st["timeline"] if r["id"] == "src:blip.m4a"][0]
    assert tl["est_minutes"] >= 1


def test_the_waiting_row_carries_the_stage_breakdown(sandbox, monkeypatch):
    """W27: est_detail (the transcribe/speakers split) was computed on every
    poll for every queued file and then thrown away -- the timeline row the
    shell renders never carried it. Either render it or stop computing it; it
    renders."""
    _source("long talk.m4a")
    monkeypatch.setattr(srv, "_est_duration", lambda p: 3600.0)
    queue, _, _ = srv._queue_and_dupes()
    row = [f for f in queue if f["name"] == "long talk.m4a"][0]
    assert row["est_detail"], "the split is still computed"

    st = srv.gather_state()
    tl = [r for r in st["timeline"] if r["id"] == "src:long talk.m4a"][0]
    assert tl["est_detail"] == row["est_detail"], \
        "the computed breakdown must reach the row the shell renders"

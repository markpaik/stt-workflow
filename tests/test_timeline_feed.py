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

import pytest

from gui import server as srv
from stt import (archive, config, control, holds, manifest, recorder, status,
                 summarize, unknowns)
from conftest import SPLIT_VOICE_BASE, SPLIT_VOICE_UID, mfile, split_voice_meeting

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
    base = "2026-05-01 Redo Voice"
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


def test_a_parked_source_says_it_is_set_aside(sandbox):
    """A source the batch set aside after failing does not re-run on the
    next batch: the note says what does run it."""
    p = _source("Corrupt Clip.wav")
    manifest.record_failure(p.name, p, "ffmpeg could not decode", unreadable=True)
    _history({"name": p.name, "ok": False,
              "summary": "ffmpeg could not decode Corrupt Clip.wav: x",
              "at": "2026-07-09T02:41:55"})
    r = _row(srv.gather_state(), id="src:Corrupt Clip.wav")
    assert r["state"] == "failed" and r.get("parked") is True
    assert "set aside" in r["retry_note"] and "Retry" in r["retry_note"]
    assert "next batch" not in r["retry_note"]


def test_a_dismissed_parked_source_never_reads_as_waiting(sandbox):
    """Dismiss hides a parked failure. The file must not fall through to
    "waiting": no automatic run will pick it."""
    p = _source("Corrupt Clip.wav")
    manifest.record_failure(p.name, p, "x", unreadable=True)
    _history({"name": p.name, "ok": False, "summary": "x",
              "at": "2026-07-09T02:41:55"})
    assert srv.dismiss_failure(p.name) is True
    st = srv.gather_state()
    assert not [r for r in st["timeline"] if r.get("id") == "src:Corrupt Clip.wav"]


def test_the_menu_bar_does_not_count_a_parked_source_as_queued(sandbox):
    from gui import menubar
    p = _source("Corrupt Clip.wav")
    _source("Fine.m4a")
    manifest.record_failure(p.name, p, "x", unreadable=True)
    assert menubar.gather()["queued"] == ["Fine.m4a"]


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
#
# The panel no longer draws a tray band (docs/ui-refresh phase 2), but the
# ranked `tray` key stays the data behind two sections: its urgent kinds
# (recorder_stall, failed, dupe_files, dupe_meetings) render as Now rows, and
# its chronic kinds (unknown_voice, review) are counted by the To finish
# eyebrow's "Name N voices" and "Review flags". The client half is pinned in
# tests/test_new_shell.py; these pin the data it reads.

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


def test_tray_has_no_flag_review_entry(sandbox):
    """Plan row 8: flags left the list. A meeting with flagged lines adds no
    tray entry, and its row still carries the count for the Has flags chip."""
    _meeting("Flagged 04082026", date="2026-04-08",
             segments=[{"start": 0.0, "end": 3.0, "text": "a b c d e", "flags": ["x"]},
                       {"start": 3.0, "end": 5.0, "text": "f g h i j", "flags": ["y"]},
                       {"start": 5.0, "end": 5.3, "text": "ok", "flags": ["z"]}])  # minor
    st = srv.gather_state()
    assert not [t for t in st["tray"] if t["kind"] == "review"]
    assert _row(st, id="Flagged 04082026")["review_substantial"] == 2


def test_tray_ranks_stall_failed_unknown(sandbox, monkeypatch, tmp_path):
    """All three kinds at once must come back in strict rank order. A flagged
    meeting adds no entry (plan row 8)."""
    _recording(monkeypatch, tmp_path, stalled=True)                   # recorder_stall
    _source("Broken.mp4")
    _history({"name": "Broken.mp4", "ok": False, "summary": "decode error",
              "at": "2026-07-09T02:41:55"})                           # failed
    _meeting("Flagged 04082026", date="2026-04-08",                   # no entry
             segments=[{"start": 0.0, "end": 3.0, "text": "a b c d e", "flags": ["x"]}])
    _meeting("m")   # the unknown's ref must resolve to a real meeting
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U007": {"file": "U007.npy", "meetings": ["m"]}}}))           # unknown_voice
    kinds = [t["kind"] for t in srv.gather_state()["tray"]]
    assert kinds == ["recorder_stall", "failed", "unknown_voice"]


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
    # right now" signal next to the queued-behind-another relabel_pending),
    # and `jobs` after it (the relabel queue's own running/queued/last report),
    # and `needs_you` (the one short list, unified plan Phase 1 step 8),
    # and `voicememos` (sync on or off and its go-live day, for /next Settings)
    assert set(st) == BASELINE_KEYS | {"timeline", "tray", "relabel_running",
                                       "jobs", "needs_you", "voicememos"}
    assert set(st["voicememos"]) == {"enabled", "since"}
    assert isinstance(st["relabel_running"], bool)
    assert st["jobs"] == {"running": None, "queued": [], "last": None}, \
        "no queue in this process: the empty shape, never a missing key"
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


# ---------- the finish record (docs/ui-refresh, phase 1) ----------
#
# Every meeting row carries `finish: {title, voices, flags, done, voice_secs}`,
# computed from counts gather_state already holds. The meeting page's finish
# strip walks title, voices, and flags in that order, and a row whose `done` is
# true opens exactly as it did before the strip existed. voice_secs (phase 2)
# is the talk time of the meeting's longest-talking unnamed voice: the To
# finish eyebrow's "Name N voices" opens the meeting holding the largest first.
# voice_uids (phase 2 QA) names the unknown-registry voices among the unnamed
# ones, so the eyebrow can count what the queue will ask about: each uid once
# across meetings, each transcript-local voice once in its own meeting.

FIN_SPEAKERS = [{"id": "SPEAKER_00", "display": "Alex Rivera", "name": "Alex Rivera"},
                {"id": "SPEAKER_01", "display": "Speaker 2"},
                {"id": "SPEAKER_02", "display": "Speaker 3"}]
FIN_SUBSTANTIAL = {"start": 10.0, "end": 14.0, "speaker": "SPEAKER_00",
                   "text": "the budget closes on Friday", "flags": ["low_conf"]}
FIN_MINOR = {"start": 20.0, "end": 20.4, "speaker": "SPEAKER_00",
             "text": "so", "flags": ["low_conf"]}


def _finish_of(base):
    return _row(srv.gather_state(), id=base)["finish"]


@pytest.fixture
def every_voice_asks(monkeypatch):
    """These tests pin how voices are COUNTED. The fixture voices talk for a
    few seconds, under the one-minute floor that hides a short voice behind
    a count (plan Phase 1 step 4), so the floor is set to 0 here. The floor
    itself is pinned in tests/test_title_rules.py."""
    monkeypatch.setattr(srv, "VOICE_ASK_MIN_SECS", 0.0)


def test_finish_on_a_meeting_waiting_for_its_title(sandbox, every_voice_asks):
    _meeting("Recording 09262026 0915", date="2026-09-26", reviewed=False,
             ai_title="Budget Review", speakers=[dict(s) for s in FIN_SPEAKERS],
             segments=[dict(FIN_SUBSTANTIAL), dict(FIN_MINOR)])
    r = _row(srv.gather_state(), id="Recording 09262026 0915")
    assert r["state"] == "needs_name"
    assert r["finish"] == {"title": False, "voices": 2, "flags": 1, "done": False,
                           "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}


def test_finish_on_a_ready_meeting_with_open_voices_and_flags(sandbox, every_voice_asks):
    _meeting("Budget Review 09262026", speakers=[dict(s) for s in FIN_SPEAKERS],
             segments=[dict(FIN_SUBSTANTIAL), dict(FIN_MINOR)])
    r = _row(srv.gather_state(), id="Budget Review 09262026")
    assert r["state"] == "ready"
    # the minor crumb is not a finish step: only substantial lines count.
    # Plan row 8: voices and flags no longer block done
    assert r["finish"] == {"title": True, "voices": 2, "flags": 1, "done": True,
                           "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}
    assert r["review_substantial"] == r["finish"]["flags"]


def test_finish_skips_dismissed_voices_and_minor_flags(sandbox, every_voice_asks):
    _meeting("Grumble 09262026", speakers=[dict(s) for s in FIN_SPEAKERS],
             segments=[dict(FIN_MINOR)])
    summarize.dismiss_voice("Grumble 09262026", "SPEAKER_02")
    assert _finish_of("Grumble 09262026") == {
        "title": True, "voices": 1, "flags": 0, "done": True,
        "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}
    summarize.dismiss_voice("Grumble 09262026", "SPEAKER_01")
    assert _finish_of("Grumble 09262026") == {
        "title": True, "voices": 0, "flags": 0, "done": True,
        "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}


def test_finish_on_a_meeting_with_only_flags_left(sandbox, every_voice_asks):
    _meeting("Flags Only 09262026", speakers=[dict(FIN_SPEAKERS[0])],
             segments=[dict(FIN_SUBSTANTIAL), dict(FIN_SUBSTANTIAL, start=30.0, end=35.0)])
    assert _finish_of("Flags Only 09262026") == {
        "title": True, "voices": 0, "flags": 2, "done": True,
        "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}


def test_finish_on_a_finished_meeting(sandbox, every_voice_asks):
    # the one speaker is named, nothing is flagged, and a missing `reviewed`
    # key reads as reviewed (pre-inbox meetings never flood the inbox)
    _meeting("Done Deal 09262026", speakers=[dict(FIN_SPEAKERS[0])])
    assert _finish_of("Done Deal 09262026") == {
        "title": True, "voices": 0, "flags": 0, "done": True,
        "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}


def test_finish_carries_the_longest_unnamed_voice_talk_time(sandbox, every_voice_asks):
    # Speaker 2 talks 9.5s over two lines, Speaker 3 4s, the named speaker 30s
    # (never a naming target), and a textless line counts for nothing
    _meeting("Talkers 09262026", speakers=[dict(s) for s in FIN_SPEAKERS],
             segments=[{"start": 0.0, "end": 30.0, "speaker": "SPEAKER_00", "text": "hello"},
                       {"start": 30.0, "end": 36.0, "speaker": "SPEAKER_01", "text": "yes"},
                       {"start": 36.0, "end": 39.5, "speaker": "SPEAKER_01", "text": "and"},
                       {"start": 40.0, "end": 44.0, "speaker": "SPEAKER_02", "text": "no"},
                       {"start": 44.0, "end": 99.0, "speaker": "SPEAKER_02", "text": "  "}])
    assert _finish_of("Talkers 09262026")["voice_secs"] == 9.5
    # dismissing the talker leaves the next-largest unnamed voice
    summarize.dismiss_voice("Talkers 09262026", "SPEAKER_01")
    assert _finish_of("Talkers 09262026")["voice_secs"] == 4.0
    # no unnamed voice left: nothing to queue
    summarize.dismiss_voice("Talkers 09262026", "SPEAKER_02")
    assert _finish_of("Talkers 09262026") == {
        "title": True, "voices": 0, "flags": 0, "done": True,
        "voice_secs": 0.0, "voice_uids": [], "short_voices": 0}


def test_finish_names_the_registry_voices_among_the_open_ones(sandbox, every_voice_asks):
    """voice_uids lists the unknown-registry uids among a meeting's unnamed
    voices, matched as the page's mUnknownUid matches them (the display, and
    this meeting in the voice's meetings). A hidden voice still counts: its
    chip stays, so the meeting still asks about it. A voice dismissed here, a
    named one, and a transcript-local one never appear."""
    named = {"id": "SPEAKER_00", "display": "Alex Rivera", "name": "Alex Rivera"}
    _meeting("Vendor Demo 09262026",
             speakers=[dict(named), {"id": "SPEAKER_01", "display": "Speaker 7"},
                       {"id": "SPEAKER_02", "display": "Speaker 3"}])
    _meeting("Board Prep 09252026",
             speakers=[dict(named), {"id": "SPEAKER_01", "display": "Speaker 7"},
                       {"id": "SPEAKER_02", "display": "Speaker 12"},
                       {"id": "SPEAKER_03", "display": "Speaker 13"}])
    # U013 is registered, but not for Board Prep: its display alone is no match
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U007": {"file": "U007.npy",
                 "meetings": ["Vendor Demo 09262026", "Board Prep 09252026"]},
        "U012": {"file": "U012.npy", "meetings": ["Board Prep 09252026"],
                 "archived": True},
        "U013": {"file": "U013.npy", "meetings": ["Vendor Demo 09262026"]}}}))
    vendor = _finish_of("Vendor Demo 09262026")
    board = _finish_of("Board Prep 09252026")
    assert (vendor["voices"], vendor["voice_uids"]) == (2, ["U007"])
    assert (board["voices"], board["voice_uids"]) == (3, ["U007", "U012"])
    # dismissed in Board Prep only: it leaves that meeting's record alone
    summarize.dismiss_voice("Board Prep 09252026", "SPEAKER_01")
    assert _finish_of("Board Prep 09252026")["voice_uids"] == ["U012"]
    assert _finish_of("Vendor Demo 09262026")["voice_uids"] == ["U007"]



def test_finish_counts_a_split_voice_once(sandbox, every_voice_asks):
    """A board meeting's strip read "Voices 22" while its To finish row read
    "Voices 21". The diarizer split one person into two clusters that carry
    one global id, so both display "Speaker 68", and naming that id names
    both: one voice. The row's count (finish.voices) is 21 voices in 22
    unnamed clusters, the uid is listed once, and the strip's own count over
    the same meeting is pinned to this number in tests/test_new_shell.py."""
    clusters, _ = split_voice_meeting()
    d = json.loads(mfile(SPLIT_VOICE_BASE, ".json").read_text())
    assert len([s for s in d["speakers"] if not s.get("name")]) == clusters == 22
    st = srv.gather_state()
    f = _row(st, id=SPLIT_VOICE_BASE)["finish"]
    assert f["voices"] == 21
    assert f["voice_uids"].count(SPLIT_VOICE_UID) == 1
    assert len(f["voice_uids"]) == 17          # 18 registry clusters, 17 uids
    # the voice queue's "Name N voices" counts the same 21: 17 registry voices
    # and the 4 transcript-local ones
    assert f["voices"] - len(f["voice_uids"]) == 4


def test_rows_name_their_people_once_most_talk_first(sandbox, every_voice_asks):
    """A To finish row lists named people, never raw labels, so the row
    carries them: each name once (a named split voice is two clusters with
    one name, a manual line's person counts too), the most talk first, and
    no unnamed, dismissed, or raw-id cluster."""
    spk = [{"id": "SPEAKER_00", "display": "Grace Lindqvist", "name": "Grace Lindqvist"},
           {"id": "SPEAKER_01", "display": "Elena Ruiz", "name": "Elena Ruiz"},
           {"id": "SPEAKER_02", "display": "Grace Lindqvist", "name": "Grace Lindqvist"},
           {"id": "SPEAKER_03", "display": "Speaker 7"},
           {"id": "SPEAKER_04", "display": "SPEAKER_04"},
           {"id": "MANUAL_1", "display": "Marcus Hale", "name": "Marcus Hale"}]
    seg = lambda sp, a, b: {"start": a, "end": b, "speaker": sp, "text": "words"}
    segs = [seg("SPEAKER_00", 0, 10), seg("SPEAKER_01", 10, 60), seg("SPEAKER_02", 60, 90),
            seg("SPEAKER_03", 90, 300), seg("MANUAL_1", 300, 305)]
    _meeting("People Ready 09232026", speakers=spk, segments=segs)
    _meeting("People Waiting 09232026", speakers=spk, segments=segs, reviewed=False)
    st = srv.gather_state()
    for base in ("People Ready 09232026", "People Waiting 09232026"):
        r = _row(st, id=base)
        # Elena 50s, Grace 10s + 30s = 40s, Marcus 5s
        assert r["named"] == ["Elena Ruiz", "Grace Lindqvist", "Marcus Hale"], base
        assert r["finish"]["voices"] == 1


# ---------- running long, and the order the queue runs in (scheduling) ----------

def test_a_stage_past_its_estimate_reports_its_overrun_on_the_row(sandbox, monkeypatch):
    """A 4-hour board meeting's diarization runs past its estimate. The row
    and the active entry say by how much (the panel shows "running long ·
    +Xm" in place of a frozen countdown), and the queue total carries a tail
    for the file that is a fraction of its elapsed stage time."""
    _source("Board 09232026.m4a")
    _source("Standup 09242026.m4a")
    status.start_run(["Board 09232026.m4a", "Standup 09242026.m4a"])
    status.set_stage("Board 09232026.m4a", "diarizing", duration=4 * 3600.0,
                     progress=0.87, base="Board 09232026")
    est = status.stage_estimates(4 * 3600.0)
    d = status.read()
    entry = d["active"]["Board 09232026.m4a"]
    entry["stage_since"] = time.monotonic() - (est["diarizing"] + 1800)   # 30 min over
    entry["progress_at"] = time.time() - 3600                             # hook silent
    status.STATUS_PATH.write_text(json.dumps(d))
    monkeypatch.setattr(srv, "_est_duration", lambda p: 1200.0)
    _running(monkeypatch)
    st = srv.gather_state()
    a = st["active"]["Board 09232026.m4a"]
    assert 1795 <= a["over_sec"] <= 1805
    r = _row(st, source_file="Board 09232026.m4a")
    assert r["state"] == "processing" and r["over"] == a["over_sec"]
    elapsed = est["diarizing"] + 1800
    tail = status.stage_tail("diarizing") * elapsed
    assert abs(a["eta_sec"] - (tail + est["writing"])) < 5
    waiting = [f for f in st["queue"] if f["name"] == "Standup 09242026.m4a"][0]
    assert abs(st["overall_eta_sec"] - (a["eta_sec"] + waiting["est_min"] * 60)) < 5
    # within its estimate a row counts down and carries no overrun
    entry["stage_since"] = time.monotonic() - 60
    status.STATUS_PATH.write_text(json.dumps(d))
    st = srv.gather_state()
    assert "over_sec" not in st["active"]["Board 09232026.m4a"]
    assert _row(st, source_file="Board 09232026.m4a")["over"] is None


def test_the_queue_eta_counts_a_summary_for_each_file_still_to_finish(sandbox, monkeypatch):
    """A run into the library writes a description after each file, about
    75 s each. The queue ETA counted only transcription, so it fell short
    by a summary per file. Now each waiting file and each active file whose
    summary has not started adds one. A run that writes no descriptions
    adds none, and a file in its summarizing stage counts its own."""
    _source("Board 09232026.m4a")
    _source("Standup 09242026.m4a")
    monkeypatch.setattr(srv, "_est_duration", lambda p: 1200.0)
    _running(monkeypatch)

    def eta(summarizes, stage="transcribing"):
        status.start_run(["Board 09232026.m4a", "Standup 09242026.m4a"],
                         summarizes=summarizes)
        kw = {"est_sec": 40.0} if stage == "summarizing" else {
            "duration": 600.0, "progress": 0.5}
        status.set_stage("Board 09232026.m4a", stage, base="Board 09232026", **kw)
        d = status.read()
        d["summary_secs"] = [70.0, 80.0, 90.0]
        status.STATUS_PATH.write_text(json.dumps(d))
        return srv.gather_state()["overall_eta_sec"]
    plain, with_summary = eta(False), eta(True)
    assert abs(with_summary - plain - 2 * 80.0) <= 2     # the waiting and the active file
    plain, with_summary = eta(False, "summarizing"), eta(True, "summarizing")
    assert abs(with_summary - plain - 80.0) <= 2         # only the waiting file


def _lengths(monkeypatch, secs):
    """Stub the shared length helper by file name (seconds of audio)."""
    from pathlib import Path

    from stt import queue_order
    monkeypatch.setattr(queue_order, "duration", lambda p: secs.get(Path(p).name))


def test_waiting_rows_carry_the_order_they_will_run_in(sandbox, monkeypatch):
    """Idle, or under a run that sweeps the folders, every waiting file runs
    shortest first: the rows carry that place as run_order. Ties break by
    name, a held file has no place, and an unknown length goes last."""
    _lengths(monkeypatch, {"Board A.m4a": 3 * 3600, "Check-in.m4a": 1200,
                           "Board B.m4a": 3 * 3600, "Budget.m4a": 5400,
                           "Parked.m4a": 60})
    for n in ("Board A.m4a", "Check-in.m4a", "Board B.m4a", "Budget.m4a",
              "Mystery.m4a"):
        _source(n)
    _source("Parked.m4a", held=True)
    st = srv.gather_state()
    order = {r["source_file"]: r.get("run_order") for r in st["timeline"]
             if r["state"] in ("waiting", "held")}
    ranked = sorted((v, k) for k, v in order.items() if v is not None)
    assert [k for _, k in ranked] == ["Check-in.m4a", "Budget.m4a", "Board A.m4a",
                                      "Board B.m4a", "Mystery.m4a"]
    assert order["Parked.m4a"] is None

    # a sweeping run: a file that landed mid-run takes its place by length
    status.start_run(["Budget.m4a", "Board A.m4a", "Board B.m4a"], sweep=True)
    status.set_stage("Check-in.m4a", "transcribing", duration=1200.0)
    _source("Quick note.m4a")
    _lengths(monkeypatch, {"Board A.m4a": 3 * 3600, "Board B.m4a": 3 * 3600,
                           "Budget.m4a": 5400, "Quick note.m4a": 300})
    _running(monkeypatch)
    st = srv.gather_state()
    ranked = sorted((r["run_order"], r["source_file"]) for r in st["timeline"]
                    if r["state"] == "waiting")
    assert [k for _, k in ranked][:4] == ["Quick note.m4a", "Budget.m4a",
                                          "Board A.m4a", "Board B.m4a"]


def test_a_run_over_its_own_list_keeps_that_list_first(sandbox, monkeypatch):
    """A queued job or --files run processes its own files, in its pending
    order, and then ends: the folder's other files wait for the next run."""
    _lengths(monkeypatch, {"Long job.m4a": 7200, "Short job.m4a": 1800,
                           "Tiny other.m4a": 120})
    for n in ("Long job.m4a", "Short job.m4a", "Tiny other.m4a"):
        _source(n)
    status.start_run(["Short job.m4a", "Long job.m4a"], sweep=False)
    _running(monkeypatch)
    st = srv.gather_state()
    assert st["pending"] == ["Short job.m4a", "Long job.m4a"]
    ranked = sorted((r["run_order"], r["source_file"]) for r in st["timeline"]
                    if r["state"] == "waiting")
    assert [k for _, k in ranked] == ["Short job.m4a", "Long job.m4a", "Tiny other.m4a"]


def test_the_panel_reads_lengths_from_the_shared_helper(sandbox, monkeypatch):
    """The panel's estimate and the batch's order read one helper
    (stt.queue_order), so the list can never disagree with the run."""
    from stt import queue_order
    _source("one.m4a")
    monkeypatch.setattr(queue_order, "duration", lambda p: 1234.0)
    assert srv._est_duration(config.source_dir() / "one.m4a") == 1234.0

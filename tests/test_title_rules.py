"""The title rules (unified plan, Phase 1 step 3, rows 8, 17 and 23).

Pinned here: which recording names count as a label, the route a file
arrived by, the state a new meeting starts in, the Redo carry list, the
`_finish` table for every title state, the naming row, and the two Keeps
(one meeting, and bulk). Every name is synthetic.
"""
import json
import types

import pytest

from gui import server as srv
from stt import audio, config, pipeline, summarize, titles
from conftest import mfile


def _meeting(base, *, segments=None, **extra):
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": "2026-10-01",
         "processed_at": "2026-10-01T10:00:00",
         "speakers": [{"id": "SPEAKER_00", "display": "Avery Sample", "name": "Avery Sample"}],
         "segments": segments or [], "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    return d


def _row(st, base):
    return next(r for r in st["timeline"] if r.get("id") == base)


def _json(base):
    return json.loads(mfile(base, ".json").read_text())


# ---------- what counts as a label ----------

@pytest.mark.parametrize("name", [
    "Budget Review.m4a", "Budget Review 9.14.m4a", "Weekly Sync (2)",
    "Planning with Avery", "3rd Grade Math", "100 Day Plan",
    "Committee of the Whole 05212026.m4a"])
def test_a_typed_name_is_a_label(name):
    assert titles.is_label(name)


@pytest.mark.parametrize("name", [
    "", "Recording 09212026 1556.m4a", "Recording", "New Recording 12",
    "New Recording", "MTG 2026-11-03", "MTG", "Untitled", "Meeting",
    "GMT20260715-160418_Recording_1920x1200.mp4", "zoom_0.mp4", "10302026",
    # address-style: the owner's decision is that these count as no label
    "1200 Example Ave", "1200 Example Ave 2", "45 N. Sample St",
    "7 Placeholder Road", "310 Test Blvd.m4a"])
def test_a_default_marker_generic_or_address_name_is_no_label(name):
    assert not titles.is_label(name)


def test_is_address_names_only_the_address_style():
    assert titles.is_address("1200 Example Ave")
    assert not titles.is_address("Budget Review")
    assert not titles.is_address("3rd Grade Math")


# ---------- routes ----------

def test_route_reads_the_public_downloader_state(sandbox, monkeypatch, tmp_path):
    state = tmp_path / "pub" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"items": {
        "k1": {"file": "Board Session 05212026.m4a"},
        "k2": {"file": "Study Session 10212026.m4a"}}}))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(state))
    assert titles.route_for("Board Session 05212026.m4a") == "public_download"
    assert titles.route_for("Budget Review.m4a") == "voice_memo"
    assert titles.route_for("Recording 09212026 1556.m4a") == "native"
    assert titles.route_for("GMT20260715-160418_Recording_1920x1200.mp4") == "conference"
    assert titles.route_for("lecture.mp3") == "other"


def test_no_public_state_means_no_public_downloads(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PUBLIC_STATE", str(tmp_path / "missing.json"))
    assert titles.public_files() == frozenset()
    assert titles.route_for("Board Session 05212026.m4a") == "voice_memo"


def test_a_file_from_the_recorder_folder_is_native(sandbox):
    p = config.recordings_dir() / "Team Huddle.m4a"
    assert titles.route_for(p.name, p) == "native"


def test_initial_title_follows_the_chain():
    assert titles.initial_title("Board Session.m4a", "public_download") == (
        "provisional", "public_download")
    assert titles.initial_title("Budget Review.m4a", "voice_memo") == ("provisional", "label")
    assert titles.initial_title("New Recording 3.m4a", "voice_memo") == ("needs_title", "model")
    assert titles.initial_title("1200 Example Ave.m4a", "voice_memo") == (
        "needs_title", "model")


# ---------- the pipeline writes the keys, and a Redo carries them ----------

def _fake_asr(words=True):
    w = [{"start": 0.2, "end": 0.5, "word": "hello"},
         {"start": 0.6, "end": 0.9, "word": "there"}] if words else []
    return types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": "fake-asr", "text": " ".join(x["word"] for x in w), "words": w})


def _process(monkeypatch, src, words=True):
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr(words))
    monkeypatch.setattr(audio, "to_wav16k", lambda s, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    return pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)


def test_a_new_labeled_meeting_arrives_provisional(sandbox, monkeypatch):
    src = config.PROJECT_DIR / "Budget Review 10012026.m4a"
    src.write_bytes(b"x")
    res = _process(monkeypatch, src)
    d = _json(res["base"])
    assert (d["title_state"], d["title_source"], d["route"]) == (
        "provisional", "label", "voice_memo")
    assert d["reviewed"] is False          # a provisional title is not a human check
    assert "no_speech" not in d


@pytest.mark.parametrize("name", ["New Recording 4 10012026.m4a",
                                  "1200 Example Ave 10012026.m4a"])
def test_a_new_meeting_without_a_label_arrives_needs_title(sandbox, monkeypatch, name):
    src = config.PROJECT_DIR / name
    src.write_bytes(b"x")
    d = _json(_process(monkeypatch, src)["base"])
    assert (d["title_state"], d["title_source"]) == ("needs_title", "model")


def test_a_redo_keeps_every_carried_key(sandbox, monkeypatch):
    src = config.PROJECT_DIR / "Budget Review 10012026.m4a"
    src.write_bytes(b"x")
    base = _process(monkeypatch, src)["base"]
    j = mfile(base, ".json")
    d = json.loads(j.read_text())
    carried = {"title_state": "confirmed", "title_source": "manual",
               "route": "conference", "recorded_at": "2026-10-01T09:00:00-04:00",
               "date_source": "recorder", "source_name": "raw name.m4a",
               "audio_fp": "fp-synthetic", "title": "Budget Review"}
    d.update(carried)
    j.write_text(json.dumps(d))
    # a Redo hands process_file the stored audio inside the meeting folder
    stored = config.meeting_file(base, ".m4a")
    stored.write_bytes(b"x")
    _process(monkeypatch, stored)
    after = _json(base)
    for k, v in carried.items():
        assert after[k] == v, k


def test_a_redo_of_a_meeting_from_before_the_rule_adds_no_state(sandbox, monkeypatch):
    base = "Legacy Sync 05012026"
    _meeting(base, reviewed=True)
    stored = config.meeting_file(base, ".m4a")
    _process(monkeypatch, stored)
    d = _json(base)
    assert "title_state" not in d and d["reviewed"] is True
    assert d["route"] == "voice_memo"      # route is a fact, filled in when missing


def test_a_silent_file_sets_no_speech_and_a_rerun_with_speech_drops_it(sandbox, monkeypatch):
    src = config.PROJECT_DIR / "Quiet Room 10012026.m4a"
    src.write_bytes(b"x")
    base = _process(monkeypatch, src, words=False)["base"]
    assert _json(base)["no_speech"] is True
    stored = config.meeting_file(base, ".m4a")
    stored.write_bytes(b"x")
    _process(monkeypatch, stored, words=True)
    assert "no_speech" not in _json(base)


# ---------- the _finish table (plan row 8) ----------

@pytest.mark.parametrize("state,reviewed,title,done", [
    (None, False, False, False),          # before the rule: falls back to reviewed
    (None, True, True, True),
    (None, None, True, True),             # no reviewed key at all reads as reviewed
    ("needs_title", False, False, False),
    ("provisional", False, True, True),
    ("confirmed", True, True, True),
    ("confirmed", False, True, True),     # a bulk Keep leaves reviewed False
])
def test_finish_follows_the_title_state(sandbox, monkeypatch, state, reviewed, title, done):
    extra = {}
    if state:
        extra["title_state"] = state
    if reviewed is not None:
        extra["reviewed"] = reviewed
    # an open voice and a flagged line: neither blocks done any more
    _meeting("Rule Check 10012026",
             speakers=[{"id": "SPEAKER_00", "display": "Speaker 2"}],
             segments=[{"start": 0.0, "end": 90.0, "speaker": "SPEAKER_00",
                        "text": "the budget closes on Friday", "flags": ["low_conf"]}],
             **extra)
    f = _row(srv.gather_state(), "Rule Check 10012026")["finish"]
    assert (f["title"], f["done"]) == (title, done)
    assert f["voices"] == 1 and f["flags"] == 1


@pytest.mark.parametrize("state,reviewed,row_state", [
    ("needs_title", False, "needs_name"),
    ("provisional", False, "ready"),
    ("confirmed", False, "ready"),
    (None, False, "needs_name"),
    (None, True, "ready"),
])
def test_the_naming_row_opens_only_for_needs_title_or_a_legacy_waiting_meeting(
        sandbox, state, reviewed, row_state):
    extra = {"reviewed": reviewed}
    if state:
        extra["title_state"] = state
    _meeting("Row Check 10012026", **extra)
    assert _row(srv.gather_state(), "Row Check 10012026")["state"] == row_state


def test_the_meta_carries_the_title_keys(sandbox):
    _meeting("Keys 10012026", title_state="provisional", title_source="label",
             route="voice_memo")
    m = next(x for x in srv.gather_state()["meetings"] if x["base"] == "Keys 10012026")
    assert (m["title_state"], m["title_source"], m["route"]) == (
        "provisional", "label", "voice_memo")


# ---------- the two Keeps (plan row 17) ----------

def test_a_bulk_keep_confirms_the_title_and_leaves_reviewed_alone(sandbox):
    _meeting("Bulk One 10012026", reviewed=False, title_state="provisional",
             title_source="label")
    h = srv.Handler.__new__(srv.Handler)
    r = h._bulk_one("accept", "Bulk One 10012026", None, False)
    assert r["ok"]
    d = _json("Bulk One 10012026")
    assert d["title_state"] == "confirmed" and d["reviewed"] is False
    assert d["title_source"] == "label"


def test_a_keep_on_one_meeting_confirms_and_marks_it_reviewed(sandbox):
    _meeting("Single One 10012026", reviewed=False, title_state="needs_title",
             title_source="model")
    r = summarize.apply_meeting_edits("Single One 10012026", reviewed=True,
                                      title_state="confirmed")
    assert r["ok"] and r["title_state"] == "confirmed"
    d = _json("Single One 10012026")
    assert d["title_state"] == "confirmed" and d["reviewed"] is True


def test_a_title_edit_confirms_the_title_as_manual(sandbox):
    _meeting("Edit Me 10012026", reviewed=False, title_state="needs_title",
             title_source="model")
    r = summarize.rename_meeting("Edit Me 10012026", "Budget Review")
    d = _json(r["base"])
    assert (d["title_state"], d["title_source"]) == ("confirmed", "manual")
    assert d["reviewed"] is False          # an edit alone claims no review


def test_an_unknown_title_state_is_refused(sandbox):
    _meeting("Odd 10012026")
    r = summarize.apply_meeting_edits("Odd 10012026", title_state="auto")
    assert not r["ok"]


# ---------- the voice exemptions (Phase 1 step 4) ----------

def _voices_meeting(base, talk_secs, **extra):
    """A meeting with one unnamed voice per entry of talk_secs (seconds)."""
    speakers, segs, t = [], [], 0.0
    for i, secs in enumerate(talk_secs):
        sid = f"SPEAKER_{i:02d}"
        speakers.append({"id": sid, "display": f"Speaker {i + 2}"})
        segs.append({"start": t, "end": t + secs, "speaker": sid, "text": "words"})
        t += secs
    return _meeting(base, speakers=speakers, segments=segs, **extra)


def test_a_voice_under_a_minute_hides_behind_a_count(sandbox):
    _voices_meeting("Mixed Voices 10012026", [75.0, 59.0, 3.0])
    f = _row(srv.gather_state(), "Mixed Voices 10012026")["finish"]
    assert f["voices"] == 1 and f["short_voices"] == 2
    assert f["voice_secs"] == 75.0
    # the legend can still name any of them: the full map stays whole
    m = next(x for x in srv.gather_state()["meetings"]
             if x["base"] == "Mixed Voices 10012026")
    assert len(m["unnamed_clusters"]) == 3


def test_a_public_download_asks_nothing(sandbox):
    _voices_meeting("Board Session 10012026", [600.0, 300.0], route="public_download")
    f = _row(srv.gather_state(), "Board Session 10012026")["finish"]
    assert f["voices"] == 0 and f["short_voices"] == 0 and f["voice_secs"] == 0.0


def test_public_downloads_are_found_from_the_downloader_state(sandbox, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"items": {"k": {"file": "Board Session 10012026.m4a"}}}))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(state))
    _voices_meeting("Board Session 10012026", [600.0])     # no stored route
    st = srv.gather_state()
    assert _row(st, "Board Session 10012026")["finish"]["voices"] == 0
    m = next(x for x in st["meetings"] if x["base"] == "Board Session 10012026")
    assert m["route"] == "public_download"


def test_a_registry_voice_heard_only_in_public_downloads_does_not_nag(sandbox):
    _voices_meeting("Board Session 10012026", [600.0], route="public_download")
    _voices_meeting("Team Sync 10012026", [600.0], route="voice_memo")
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U082": {"file": "U082.npy", "meetings": ["Board Session 10012026"]},
        "U020": {"file": "U020.npy",
                 "meetings": ["Board Session 10012026", "Team Sync 10012026"]}}}))
    tray = srv.gather_state()["tray"]
    assert [t["target"] for t in tray if t["kind"] == "unknown_voice"] == ["U020"]

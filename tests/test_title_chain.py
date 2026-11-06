"""The owner's title chain: a typed Voice Memos title wins, a default title
is not a typed title (a default address is kept as a location note), then
the named speakers plus the topic, then the local model's draft. Every
automatic title stays provisional. Every name is synthetic."""
import json
import os
import types
from datetime import datetime

import run_batch
from conftest import mfile
from stt import audio, config, pipeline, provenance, summarize, titles


def _process(monkeypatch, src):
    w = [{"start": 0.2, "end": 0.5, "word": "hello"}]
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: types.SimpleNamespace(
        transcribe=lambda wav, progress=None: {"engine": "fake", "text": "hello",
                                               "words": w}))
    monkeypatch.setattr(audio, "to_wav16k", lambda s, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 600.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(provenance, "probe_tags", lambda p: {"tags": {}, "duration": None})
    provenance._probe_cache.clear()
    return pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)


def _staged(name, side):
    d = config.PROJECT_DIR / "voicememos"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(b"synthetic memo")
    t = datetime(2026, 10, 29, 10, 0).timestamp()
    os.utime(p, (t, t))
    provenance.sidecar_path(p).write_text(json.dumps(side))
    return p


def _json(base):
    return json.loads(config.meeting_file(base, ".json").read_text())


def test_a_typed_voice_memo_title_wins(sandbox, monkeypatch):
    src = _staged("Budget Review 10292026.m4a", {
        "vm_uuid": "VM-1", "recorded_at": "2026-10-29T09:00:00-05:00",
        "title": "Budget Review", "title_typed": True, "location_note": None})
    d = _json(_process(monkeypatch, src)["base"])
    assert (d["title_state"], d["title_source"]) == ("provisional", "label")
    assert d["title"] == "Budget Review" and d["vm_uuid"] == "VM-1"
    assert (d["date"], d["date_source"]) == ("2026-10-29", "recorder")
    assert "location_note" not in d


def test_a_default_address_title_is_a_location_note_and_no_label(sandbox, monkeypatch):
    src = _staged("Voice Memo 10292026.m4a", {
        "vm_uuid": "VM-2", "recorded_at": "2026-10-29T09:00:00-05:00",
        "title": None, "title_typed": False, "location_note": "1200 Example Ave"})
    d = _json(_process(monkeypatch, src)["base"])
    assert (d["title_state"], d["title_source"]) == ("needs_title", "model")
    assert d["location_note"] == "1200 Example Ave"
    assert d["source_name"] == "Voice Memo 10292026.m4a"


def _model_meeting(base, speakers, segments, **extra):
    d = {"meeting_id": "m-" + base[:4], "source_file": f"{base}.m4a",
         "source_name": f"{base}.m4a", "date": "2026-10-29",
         "title_state": "needs_title", "title_source": "model", "reviewed": False,
         "speakers": speakers, "segments": segments, "words": [],
         "ai_title": "Spring Budget Planning", "ai_summary": "drafted"}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("x")
    return d


SPK = [{"id": "S0", "display": "Avery Sample", "name": "Avery Sample"},
       {"id": "S1", "display": "Blake Example", "name": "Blake Example"},
       {"id": "S2", "display": "Speaker 7", "name": None}]
SEGS = [{"start": 0, "end": 10, "speaker": "S1", "text": "a"},
        {"start": 10, "end": 15, "speaker": "S0", "text": "b"},
        {"start": 15, "end": 60, "speaker": "S2", "text": "c"}]


def test_named_speakers_plus_topic_come_next(sandbox):
    _model_meeting("Voice Memo 10292026", SPK, SEGS)
    r = summarize.apply_auto_title("Voice Memo 10292026")
    assert r["ok"]
    new = r["base"]
    assert new == "2026-10-29 Spring Budget Planning with Blake Example and Avery Sample"
    d = _json(new)
    assert (d["title_state"], d["title_source"]) == ("provisional", "model")
    assert d["reviewed"] is False                       # never a human check
    assert d["source_name"] == "Voice Memo 10292026.m4a"  # the raw name stays


def test_the_owner_is_left_out_of_the_speakers(sandbox, monkeypatch):
    monkeypatch.setattr(config, "mic_speaker", lambda: "Avery Sample")
    d = _model_meeting("Voice Memo 10292026", SPK, SEGS)
    assert titles.auto_title(d, me="Avery Sample") == "Spring Budget Planning with Blake Example"


def test_the_model_draft_is_last(sandbox):
    _model_meeting("Voice Memo 10292026", [SPK[2]], SEGS[2:])
    r = summarize.apply_auto_title("Voice Memo 10292026")
    assert r["base"] == "2026-10-29 Spring Budget Planning"
    assert _json(r["base"])["title_state"] == "provisional"


def test_more_than_two_speakers_read_and_others():
    d = {"ai_title": "Planning", "speakers": [
        {"id": "a", "name": "A One"}, {"id": "b", "name": "B Two"}, {"id": "c", "name": "C Three"}],
        "segments": []}
    assert titles.auto_title(d) == "Planning with A One, B Two and others"


def test_the_automatic_title_happens_once_and_never_over_a_person(sandbox):
    _model_meeting("Voice Memo 10292026", SPK, SEGS)
    new = summarize.apply_auto_title("Voice Memo 10292026")["base"]
    assert summarize.apply_auto_title(new).get("skipped")
    _model_meeting("Kept Title 10302026", SPK, SEGS, title_state="confirmed",
                   title_source="manual")
    assert summarize.apply_auto_title("Kept Title 10302026").get("skipped")
    _model_meeting("Typed Label 10302026", SPK, SEGS, title_state="provisional",
                   title_source="label")
    assert summarize.apply_auto_title("Typed Label 10302026").get("skipped")
    # a meeting from before the rule (no title_state) is never renamed
    _model_meeting("Legacy 10312026", SPK, SEGS, title_state=None, title_source=None)
    assert summarize.apply_auto_title("Legacy 10312026").get("skipped")


def test_no_topic_means_no_rename(sandbox):
    _model_meeting("Voice Memo 10292026", SPK, SEGS, ai_title="Untitled Meeting")
    assert summarize.apply_auto_title("Voice Memo 10292026").get("skipped")


def test_the_batch_titles_after_the_summary(sandbox, monkeypatch):
    _model_meeting("Voice Memo 10292026", SPK, SEGS, ai_summary=None, ai_title=None)

    def fake_suggest(base):
        j = config.meeting_file(base, ".json")
        d = json.loads(j.read_text())
        d["ai_title"], d["ai_summary"] = "Spring Budget Planning", "drafted"
        j.write_text(json.dumps(d))
        return {}
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", fake_suggest)
    assert run_batch.summarize_one("Voice Memo 10292026.m4a", "Voice Memo 10292026")
    assert "2026-10-29 Spring Budget Planning with Blake Example and Avery Sample" in \
        config.meeting_bases()


def test_an_already_summarized_meeting_still_gets_its_automatic_title(sandbox, monkeypatch):
    """A meeting whose summary landed earlier (a failed title attempt, a
    re-queued file) still takes the title chain's last step."""
    _model_meeting("Voice Memo 10292026", SPK, SEGS)   # ai_summary already set
    calls = []
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "suggest_title", lambda b: calls.append(b) or {})
    assert run_batch.summarize_one("Voice Memo 10292026.m4a", "Voice Memo 10292026") is False
    assert calls == []                                   # no second summary
    assert "2026-10-29 Spring Budget Planning with Blake Example and Avery Sample" in \
        config.meeting_bases()

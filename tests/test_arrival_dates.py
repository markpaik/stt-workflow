"""True dates for new arrivals (unified plan Phase 2 step 2).

A brand-new meeting gets `source_name`, `audio_fp`, `date_source` and
`recorded_at` at its first write, from one rule shared with the folder
stamp. A Redo writes none of them. Every name here is synthetic.
"""
import json
import os
import types
from datetime import date, datetime, timedelta

import pytest

from stt import audio, config, dates, pipeline, provenance
from conftest import mfile


def _fake_asr():
    w = [{"start": 0.2, "end": 0.5, "word": "hello"}]
    return types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": "fake-asr", "text": "hello", "words": w})


def _process(monkeypatch, src):
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(audio, "to_wav16k", lambda s, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 600.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(provenance, "probe_tags",
                        lambda p: {"tags": {}, "duration": None})
    provenance._probe_cache.clear()
    return pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)


def _src(name, mtime_day=None, body=b"synthetic audio bytes"):
    p = config.PROJECT_DIR / name
    p.write_bytes(body)
    if mtime_day:
        t = datetime.fromisoformat(mtime_day + "T15:00:00").timestamp()
        os.utime(p, (t, t))
    return p


def _json(base):
    return json.loads(mfile(base, ".json").read_text())


def test_a_typed_8_digit_date_is_typed(sandbox, monkeypatch):
    d = _json(_process(monkeypatch, _src("Budget Review 10012026.m4a", "2026-10-20"))["base"])
    assert (d["date"], d["date_source"], d["recorded_at"]) == ("2026-10-01", "typed", None)
    assert d["source_name"] == "Budget Review 10012026.m4a"
    assert d["audio_fp"]


def test_a_short_typed_date_beats_the_transfer_day(sandbox, monkeypatch):
    res = _process(monkeypatch, _src("Budget Review 9.14.m4a", "2026-09-29"))
    d = _json(res["base"])
    assert (d["date"], d["date_source"]) == ("2026-09-14", "short_typed")
    # the folder stamp follows the same rule
    assert res["base"] == "Budget Review 9.14 09142026"


def test_a_short_date_at_a_year_boundary_resolves_to_the_past(sandbox, monkeypatch):
    d = _json(_process(monkeypatch, _src("Year End 12.30.m4a", "2027-01-08"))["base"])
    assert (d["date"], d["date_source"]) == ("2026-12-30", "short_typed")


def test_a_dash_short_date_parses():
    assert dates.short_date("Coaching 10-5", "2026-10-20") == "2026-10-05"
    assert dates.short_date("Coaching 9.14", "2026-09-29") == "2026-09-14"


def test_no_date_in_the_name_falls_back_to_the_file_day(sandbox, monkeypatch):
    d = _json(_process(monkeypatch, _src("Team Huddle.m4a", "2026-10-20"))["base"])
    assert (d["date"], d["date_source"], d["recorded_at"]) == ("2026-10-20", "mtime", None)


def test_a_future_typed_date_is_never_used(sandbox, monkeypatch):
    fut = (date.today() + timedelta(days=40)).strftime("%m%d%Y")
    d = _json(_process(monkeypatch, _src(f"Retreat Plan {fut}.m4a", "2026-10-20"))["base"])
    assert d["date_source"] == "mtime" and d["date"] == "2026-10-20"


def test_the_native_recorder_name_gives_a_start_time(sandbox, monkeypatch):
    d = _json(_process(monkeypatch, _src("Recording 10012026 1405.m4a", "2026-10-01"))["base"])
    assert d["date_source"] == "recorder"
    assert d["recorded_at"].startswith("2026-10-01T14:05:00")


def test_a_voice_memo_tag_counts_only_when_it_passes_the_per_file_check(sandbox, monkeypatch):
    src = _src("Team Huddle.m4a", "2026-10-20")
    mt = src.stat().st_mtime
    start = datetime.fromtimestamp(mt - 1200).astimezone()
    tags = {"tags": {"voice-memo-uuid": "u1",
                     "creation_time": start.isoformat()}, "duration": 600.0}
    provenance._probe_cache.clear()
    monkeypatch.setattr(provenance, "probe_tags", lambda p: tags)
    assert provenance.fresh(src)[0] == "recorder"
    # a tag near the export time fails: the recording would end after the file
    late = datetime.fromtimestamp(mt - 30).astimezone()
    tags["tags"]["creation_time"] = late.isoformat()
    provenance._probe_cache.clear()
    assert provenance.fresh(src)[0] == "mtime"


def test_a_sync_sidecar_start_wins(sandbox, monkeypatch):
    src = _src("Budget Review 10202026.m4a", "2026-10-20")
    provenance.sidecar_path(src).write_text(json.dumps(
        {"recorded_at": "2026-10-19T13:39:33-04:00"}))
    d = _json(_process(monkeypatch, src)["base"])
    assert (d["date"], d["date_source"]) == ("2026-10-19", "recorder")
    assert d["recorded_at"].startswith("2026-10-19T13:39:33")


def test_a_redo_writes_no_new_arrival_keys_on_a_legacy_meeting(sandbox, monkeypatch):
    base = "Legacy Sync 05012026"
    mfile(base, ".json").write_text(json.dumps({
        "source_file": f"{base}.m4a", "date": "2026-05-01", "segments": [],
        "speakers": [], "words": []}))
    stored = mfile(base, ".m4a")
    stored.write_bytes(b"x")
    _process(monkeypatch, stored)
    d = _json(base)
    for k in ("source_name", "audio_fp", "date_source", "recorded_at"):
        assert k not in d, k
    assert d["date"] == "2026-05-01"


def test_a_redo_keeps_the_frozen_source_name_and_fingerprint(sandbox, monkeypatch):
    base = _process(monkeypatch, _src("Budget Review 10012026.m4a", "2026-10-20"))["base"]
    first = _json(base)
    stored = config.meeting_file(base, ".m4a")
    stored.write_bytes(b"different bytes now")
    _process(monkeypatch, stored)
    after = _json(base)
    for k in ("source_name", "audio_fp", "date_source", "recorded_at"):
        assert after[k] == first[k], k

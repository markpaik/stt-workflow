"""The empty-transcript guard (unified plan, Phase 1 step 5, row 25).

A transcript with 0 segments never reaches the model: Generate answers "No
speech found in this recording." and writes nothing, the batch writes no
summary, and the panel offers no Generate. Every name is synthetic."""
import json
import re
from pathlib import Path

import run_batch
from gui import server as srv
from stt import summarize
from conftest import mfile


def _silent(base="Quiet Room 10012026", **extra):
    d = {"source_file": f"{base}.m4a", "speakers": [], "segments": [], "words": [],
         "no_speech": True}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("")
    return base


def _boom(*a, **k):
    raise AssertionError("the model must not run on an empty transcript")


def test_generate_on_a_silent_meeting_never_calls_the_model(sandbox, monkeypatch):
    base = _silent()
    monkeypatch.setattr(summarize, "_generate", _boom)
    before = mfile(base, ".json").read_text()
    r = summarize.suggest_title(base)
    assert r == {"ok": False, "no_speech": True,
                 "error": "No speech found in this recording."}
    assert mfile(base, ".json").read_text() == before


def test_zero_segments_alone_trips_the_guard(sandbox, monkeypatch):
    base = _silent(no_speech=False)
    monkeypatch.setattr(summarize, "_generate", _boom)
    assert summarize.suggest_title(base)["no_speech"] is True


def test_the_batch_writes_no_summary_for_a_silent_meeting(sandbox, monkeypatch):
    base = _silent()
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "_generate", _boom)
    assert run_batch.summarize_one(f"{base}.m4a", base) is False
    assert "ai_summary" not in json.loads(mfile(base, ".json").read_text())


def test_the_meta_carries_no_speech(sandbox):
    base = _silent()
    m = next(x for x in srv.gather_state()["meetings"] if x["base"] == base)
    assert m["no_speech"] is True


def test_the_reader_offers_no_generate_on_a_silent_meeting():
    js = (Path(srv.__file__).resolve().parent / "static" / "app.js").read_text()
    fn = re.search(r"function _mSummaryInner\(m\)\{.*?\n\}", js, re.S).group(0)
    head = fn[:fn.index("const has=")]
    assert "if(m.no_speech)" in head
    assert "No speech found in this recording." in head
    assert "mGenSummary" not in head

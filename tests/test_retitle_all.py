"""tools/retitle_all.py: the dry run writes proposals and changes nothing,
--apply renames through apply_meeting_edits with a reversal journal, it
resumes and skips what is done, it refuses while the batch runs, it keeps
the twin rule on a collision, and --undo reverses it. The model is faked.
Every name is synthetic."""
import json
import sys
from pathlib import Path

import pytest

from stt import config, search, status, summarize
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import retitle_all as rt  # noqa: E402

DASH = "–"


def _m(base, mid, date="2026-10-01", **extra):
    d = {"meeting_id": mid, "source_file": f"{base}.m4a", "date": date,
         "speakers": [{"id": "Robin Cole", "name": "Robin Cole", "display": "Robin Cole"}],
         "segments": [{"start": 0.0, "end": 3.0, "speaker": "Robin Cole", "text": "hello"}],
         "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text("[00:00] Robin Cole: hello\n")
    config.index_set(mid, base)


def _reply(series_id, topic, person="none"):
    return (f"SERIES: {series_id}\nPERSON: {person}\nTOPIC: {topic}\n"
            "GIST: Hiring plan approved for spring.\nTAGS: hiring-staffing, budget-finance, goals-okrs\n"
            "DECISIONS:\n- Robin Cole posts the role.\nOPEN QUESTIONS:\n- none\n"
            "NEXT STEPS:\n- [Robin Cole] will post the role by Friday\n")


@pytest.fixture
def lib(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    (sandbox / "stt.env").write_text("STT_MIC_SPEAKER=Owen Park\n")
    _m("Robin Check-in 10-5 10012026", "mid-a", title_state="provisional", title_source="label")
    _m("LT Weekly Meeting 10022026", "mid-b", date="2026-10-02")
    _m("LT Weekly Meeting 10022026 (2)", "mid-c", date="2026-10-02")
    _m("Recording 10032026 1200", "mid-d", date="2026-10-03",
       title_state="needs_title", title_source="model")
    _m("Silent 10042026", "mid-e", date="2026-10-04", segments=[])
    replies = {"Robin Check-in": _reply("weekly_checkin", "Hiring Plan", "Robin Cole"),
               "LT Weekly": _reply("lt_weekly", "Budget"),
               "Recording": _reply("one_off", "Vendor Pricing Review")}
    calls = []

    def fake(prompt, **k):
        for key, r in replies.items():
            if f"current title (it may be a default name, messy, or wrong): {key}" in prompt:
                calls.append(key)
                return r
        raise AssertionError("unexpected prompt")
    monkeypatch.setattr(summarize, "_generate", fake)
    return calls


def _snapshot():
    root = config.meetings_dir()
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file() and ".locks" not in p.parts)


def test_the_dry_run_writes_proposals_and_changes_nothing(lib):
    before = _snapshot()
    doc = rt.dry_run(log=lambda *_: None)
    assert _snapshot() == before
    assert not rt.journal_path().exists()
    props = {p["old_base"]: p for p in doc["proposals"]}
    a = props["Robin Check-in 10-5 10012026"]
    assert a["new_base"] == f"Robin Weekly Check-in {DASH} Hiring Plan 10012026"
    assert a["series"] == "weekly_checkin" and a["person"] == "Robin"
    assert a["gist"] == "Hiring plan approved for spring."
    assert a["tags"] == ["hiring-staffing", "budget-finance", "goals-okrs"]
    assert "old title carries a date: 10-5" in a["flags"]
    # two meetings land on one name: the second gets the twin suffix
    b, c = props["LT Weekly Meeting 10022026"], props["LT Weekly Meeting 10022026 (2)"]
    assert b["new_base"] == f"LT Weekly Meeting {DASH} Budget 10022026"
    assert c["new_base"] == f"LT Weekly Meeting {DASH} Budget 10022026 (2)"
    assert props["Silent 10042026"]["skip"] == "no speech"
    assert rt.proposals_path().exists() and rt.proposals_path().with_suffix(".tsv").exists()
    counts = rt.series_counts(doc["proposals"])
    assert counts["lt_weekly"] == 2 and counts["weekly_checkin"] == 1


def test_a_second_dry_run_reuses_the_cache(lib):
    rt.dry_run(log=lambda *_: None)
    n = len(lib)
    rt.dry_run(log=lambda *_: None)
    assert len(lib) == n


def test_apply_renames_journals_resumes_and_keeps_registries_in_step(lib):
    rt.dry_run(log=lambda *_: None)
    lib.clear()
    r = rt.apply(log=lambda *_: None)
    assert r["ok"] and r["counts"]["applied"] == 4 and not lib     # no model call
    new_a = f"Robin Weekly Check-in {DASH} Hiring Plan 10012026"
    assert config.base_of("mid-a") == new_a
    d = json.loads(config.meeting_file(new_a, ".json").read_text())
    assert d["renamed_from"] == "Robin Check-in 10-5 10012026"
    assert d["ai_gist"] == "Hiring plan approved for spring."
    assert d["ai_series"] == "weekly_checkin"
    assert d["ai_next_steps"] == ["[Robin Cole] will post the role by Friday"]
    assert d["title_state"] == "provisional" and d["title_source"] == "model"
    # a needs_title meeting becomes provisional, never confirmed
    dd = json.loads(config.meeting_file(config.base_of("mid-d"), ".json").read_text())
    assert dd["title_state"] == "provisional"
    # a meeting from before the title rule keeps no title state
    db = json.loads(config.meeting_file(config.base_of("mid-b"), ".json").read_text())
    assert "title_state" not in db
    assert config.base_of("mid-c").endswith(" (2)")
    # search follows the rename
    hits = search.meetings("Hiring")
    assert any(h.get("base") == new_a for h in hits)
    ev = [e["event"] for e in rt.journal_entries()]
    assert ev.count("begin") == 4 and ev.count("apply") == 4
    # resumable: a second apply does nothing
    r2 = rt.apply(log=lambda *_: None)
    assert r2["counts"].get("applied", 0) == 0 and r2["counts"]["already done"] == 4


def test_apply_finishes_a_meeting_a_stop_left_half_done(lib, monkeypatch):
    rt.dry_run(log=lambda *_: None)
    real = summarize.store_description
    boom = {"n": 0}

    def once(base, desc, title=None):
        if boom["n"] == 0:
            boom["n"] += 1
            raise KeyboardInterrupt("stopped")
        return real(base, desc, title=title)
    monkeypatch.setattr(summarize, "store_description", once)
    with pytest.raises(KeyboardInterrupt):
        rt.apply(log=lambda *_: None)
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["applied"] == 4
    new_a = config.base_of("mid-a")
    assert json.loads(config.meeting_file(new_a, ".json").read_text())["ai_gist"]


def test_apply_refuses_while_the_batch_runs(lib):
    rt.dry_run(log=lambda *_: None)
    status.STATUS_PATH.write_text(json.dumps({"running": True}))
    r = rt.apply(log=lambda *_: None)
    assert r["ok"] is False and "batch is running" in r["error"]
    assert config.base_of("mid-a") == "Robin Check-in 10-5 10012026"
    assert rt.main(["--apply"]) == 2


def test_apply_skips_a_meeting_a_person_renamed_since_the_dry_run(lib):
    rt.dry_run(log=lambda *_: None)
    assert summarize.rename_meeting("Robin Check-in 10-5 10012026", "Robin Chat")["ok"]
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["renamed since the dry run"] == 1
    assert config.base_of("mid-a") == "Robin Chat 10012026"


def test_apply_needs_a_proposal_file(lib):
    r = rt.apply(log=lambda *_: None)
    assert r["ok"] is False and "dry run first" in r["error"]


def test_undo_reverses_the_names_and_fields(lib):
    rt.dry_run(log=lambda *_: None)
    rt.apply(log=lambda *_: None)
    plan = rt.undo(dry=True, log=lambda *_: None)
    assert plan["counts"]["would undo"] == 4
    assert config.base_of("mid-a") != "Robin Check-in 10-5 10012026"
    r = rt.undo(dry=False, log=lambda *_: None)
    assert r["counts"]["undone"] == 4
    assert config.base_of("mid-a") == "Robin Check-in 10-5 10012026"
    d = json.loads(config.meeting_file("Robin Check-in 10-5 10012026", ".json").read_text())
    assert d["title_source"] == "label" and "ai_gist" not in d
    assert config.base_of("mid-b") == "LT Weekly Meeting 10022026"


def test_the_dry_run_rests_between_model_calls(lib, monkeypatch):
    rests = []
    monkeypatch.setattr(config, "llm_rest", lambda sec, log=None, sleep=None: rests.append(sec))
    rt.dry_run(log=lambda *_: None)
    # four model calls, a rest after each but the last meeting in the list
    assert len(rests) == 4
    rests.clear()
    rt.dry_run(log=lambda *_: None)       # all cached: no model work, no rest
    assert rests == []

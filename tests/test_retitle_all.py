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
    assert a["new_base"] == f"2026-10-01 Robin Weekly Check-in {DASH} Hiring Plan"
    assert a["series"] == "weekly_checkin" and a["person"] == "Robin"
    assert a["gist"] == "Hiring plan approved for spring."
    assert a["tags"] == ["hiring-staffing", "budget-finance", "goals-okrs"]
    assert "old title carries a date: 10-5" in a["flags"]
    # two meetings land on one name: the second gets the twin suffix
    b, c = props["LT Weekly Meeting 10022026"], props["LT Weekly Meeting 10022026 (2)"]
    assert b["new_base"] == f"2026-10-02 LT Weekly Meeting {DASH} Budget"
    assert c["new_base"] == f"2026-10-02 LT Weekly Meeting {DASH} Budget (2)"
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
    new_a = f"2026-10-01 Robin Weekly Check-in {DASH} Hiring Plan"
    assert config.base_of("mid-a") == new_a
    d = json.loads(config.meeting_file(new_a, ".json").read_text())
    assert d["renamed_from"] == "Robin Check-in 10-5 10012026"
    assert d["ai_gist"] == "Hiring plan approved for spring."
    assert d["ai_series"] == "weekly_checkin"
    assert "ai_next_steps" not in d
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


def test_finding2_a_rename_that_landed_without_its_description_is_finished_and_undone(
        lib, monkeypatch):
    rt.dry_run(log=lambda *_: None)
    real = summarize.store_description
    state = {"fail": True}

    def flaky(base, desc, title=None):
        if state["fail"]:
            raise OSError("disk full")
        return real(base, desc, title=title)
    monkeypatch.setattr(summarize, "store_description", flaky)
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["failed"] == 4
    ev = [e["event"] for e in rt.journal_entries() if e["meeting_id"] == "mid-a"]
    assert ev == ["begin", "renamed", "fail"]
    # undo knows the folder moved, though no description landed
    assert rt.undo(dry=True, log=lambda *_: None)["counts"]["would undo"] == 4
    # a rerun finishes it instead of calling it "renamed since the dry run"
    state["fail"] = False
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["applied"] == 4
    assert "renamed since the dry run" not in r["counts"]
    d = json.loads(config.meeting_file(config.base_of("mid-a"), ".json").read_text())
    assert d["ai_gist"] == "Hiring plan approved for spring."


def test_finding8_apply_skips_a_meeting_whose_transcript_or_diar_changed(lib):
    rt.dry_run(log=lambda *_: None)
    # a text edit on one meeting, a new diar cache on another, a Redo on a third
    a = config.meeting_file("Robin Check-in 10-5 10012026", ".json")
    d = json.loads(a.read_text())
    d["words"] = [{"start": 0.0, "end": 1.0, "word": "changed"}]
    a.write_text(json.dumps(d))
    mfile("LT Weekly Meeting 10022026", ".diar.npz").write_bytes(b"new cache")
    c = config.meeting_file("Recording 10032026 1200", ".json")
    d = json.loads(c.read_text())
    d["ai_stale"] = True
    c.write_text(json.dumps(d))
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["changed since the dry run"] == 3
    assert r["counts"]["applied"] == 1
    assert config.base_of("mid-a") == "Robin Check-in 10-5 10012026"
    assert json.loads(c.read_text())["ai_stale"] is True     # never cleared


def test_finding8_a_proposal_without_signatures_is_never_applied(lib):
    rt.dry_run(log=lambda *_: None)
    doc = json.loads(rt.proposals_path().read_text())
    for p in doc["proposals"]:
        p.pop("words_sig", None)
    rt.proposals_path().write_text(json.dumps(doc))
    r = rt.apply(log=lambda *_: None)
    assert r["counts"]["changed since the dry run"] == 4 and "applied" not in r["counts"]


def test_cached_replies_are_re_parsed_with_the_current_parser(lib, monkeypatch):
    rt.dry_run(log=lambda *_: None)
    # a cache entry written by the old parser: a bad decisions list, no ctx
    f = next((rt.out_dir() / "cache").glob("mid-b.json"))
    c = json.loads(f.read_text())
    c["desc"]["raw"] = c["desc"]["raw"].replace("DECISIONS:", "**DECISIONS:**")
    c["desc"]["decisions"] = ["", "Robin Cole posts the role."]
    c["desc"].pop("ctx", None)
    f.write_text(json.dumps(c))
    lib.clear()
    doc = rt.dry_run(log=lambda *_: None)
    assert not lib                                   # no model call
    b = next(p for p in doc["proposals"] if p["meeting_id"] == "mid-b")
    assert b["decisions"] == ["Robin Cole posts the role."]


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
    assert config.base_of("mid-a") == "2026-10-01 Robin Chat"


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
    # the old title comes back, in the ISO-first folder form
    assert config.base_of("mid-a") == "2026-10-01 Robin Check-in 10-5"
    d = json.loads(config.meeting_file("2026-10-01 Robin Check-in 10-5", ".json").read_text())
    assert d["title_source"] == "label" and "ai_gist" not in d
    assert d["title"] == "Robin Check-in 10-5"
    assert config.base_of("mid-b") == "2026-10-02 LT Weekly Meeting"


def test_the_dry_run_rests_between_model_calls(lib, monkeypatch):
    rests = []
    monkeypatch.setattr(config, "llm_rest", lambda sec, log=None, sleep=None: rests.append(sec))
    rt.dry_run(log=lambda *_: None)
    # four model calls, a rest after each but the last meeting in the list
    assert len(rests) == 4
    rests.clear()
    rt.dry_run(log=lambda *_: None)       # all cached: no model work, no rest
    assert rests == []


def test_undo_without_apply_prints_the_plan_and_never_runs_the_model(lib, monkeypatch, capsys):
    """'--undo' alone is documented as a dry run of the reversal. It fell
    through into the full model dry run, which rewrote proposals.json."""
    rt.dry_run(log=lambda *_: None)
    rt.apply(log=lambda *_: None)
    before = rt.proposals_path().read_bytes()

    def boom(**k):
        raise AssertionError("--undo must never start the model dry run")
    monkeypatch.setattr(rt, "dry_run", boom)
    assert rt.main(["--undo"]) == 0
    out = capsys.readouterr().out
    assert "would undo" in out
    assert rt.proposals_path().read_bytes() == before

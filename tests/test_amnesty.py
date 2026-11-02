"""tools/amnesty.py (unified plan, Phase 1 step 6).

The dry run prints counts and writes nothing. The apply relabels the waiting
backlog without touching `reviewed`, moves no folder, skips an active
meeting, and journals every field, so undo restores one field at a time and
keeps an edit made after the amnesty. Every name is synthetic."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from stt import config, status, summarize
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import amnesty  # noqa: E402

SPOKEN = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "we will ship it"}]


def _m(base, *, segments=SPOKEN, **extra):
    d = {"meeting_id": "m-" + hashlib.sha1(base.encode()).hexdigest()[:10],
         "source_file": f"{base}.m4a", "date": "2026-10-01",
         "processed_at": "2026-10-01T10:00:00", "generated_at": "2026-10-01T10:05:00",
         "speakers": [], "segments": segments, "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text("stub")
    return d


@pytest.fixture
def store(sandbox, monkeypatch, tmp_path):
    """A small synthetic library: 3 labeled and 1 public download waiting, 2
    waiting with no label, 2 already reviewed, 1 from before the inbox, and
    1 silent meeting with an invented summary."""
    pub = tmp_path / "pubstate.json"
    pub.write_text(json.dumps({"items": {"k": {"file": "Board Session 05212026.m4a"}}}))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(pub))
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    _m("Budget Review 10012026", reviewed=False)
    _m("Planning with Avery 10022026", reviewed=False)
    _m("Weekly Sync 9.14 10032026", reviewed=False)
    _m("Board Session 05212026", reviewed=False)
    _m("Recording 10042026 1344", reviewed=False, ai_title="Draft From Model")
    _m("1200 Example Ave 10052026", reviewed=False, ai_title="Another Draft")
    _m("Kept Title 09012026", reviewed=True, title_state="confirmed")
    _m("Seen Before 09022026", reviewed=True)
    _m("Old Meeting 05012026")
    _m("Quiet Room 10062026", segments=[], reviewed=True,
       ai_summary="An invented summary.", ai_title="Invented", ai_next_steps=["x"])
    return tmp_path


def _snapshot():
    return {b: config.meeting_file(b, ".json").read_bytes()
            for b in config.meeting_bases()}


def _load(base):
    return json.loads(config.meeting_file(base, ".json").read_text())


def test_the_dry_run_prints_counts_and_writes_nothing(store, capsys):
    before = _snapshot()
    files_before = sorted(p for p in config.PROJECT_DIR.rglob("*") if p.is_file())
    assert amnesty.main([]) == 0
    out = capsys.readouterr().out
    assert "titles provisional: 4 (label 3, public_download 1)" in out
    assert "titles needs_title: 2 (model)" in out
    assert "no_speech set: 1" in out
    assert "ai_summary 1" in out
    assert _snapshot() == before
    assert sorted(p for p in config.PROJECT_DIR.rglob("*") if p.is_file()) == files_before
    assert not (store / "migration").exists()


def test_apply_relabels_the_backlog_and_never_touches_reviewed(store):
    reviewed = {b: _load(b).get("reviewed", "MISSING") for b in config.meeting_bases()}
    stamps = {b: _load(b)["generated_at"] for b in config.meeting_bases()}
    bases = set(config.meeting_bases())
    r = amnesty.apply()
    assert set(config.meeting_bases()) == bases           # no folder moved
    for b in bases:
        assert _load(b).get("reviewed", "MISSING") == reviewed[b], b
        assert _load(b)["generated_at"] == stamps[b], b
    got = {b: (_load(b).get("title_state"), _load(b).get("title_source")) for b in bases}
    assert got["Budget Review 10012026"] == ("provisional", "label")
    assert got["Board Session 05212026"] == ("provisional", "public_download")
    assert got["Recording 10042026 1344"] == ("needs_title", "model")
    assert got["1200 Example Ave 10052026"] == ("needs_title", "model")
    assert got["Kept Title 09012026"] == ("confirmed", None)
    assert got["Seen Before 09022026"] == (None, None)      # reviewed: no state needed
    assert got["Old Meeting 05012026"] == (None, None)
    assert _load("Board Session 05212026")["route"] == "public_download"
    assert all(_load(b).get("route") for b in bases)
    quiet = _load("Quiet Room 10062026")
    assert quiet["no_speech"] is True
    assert not {"ai_summary", "ai_title", "ai_next_steps"} & set(quiet)
    j = Path(r["journal"])
    assert j.stat().st_mode & 0o777 == 0o600
    assert j.parent.stat().st_mode & 0o777 == 0o700


def test_the_public_downloads_found_by_route(store):
    amnesty.apply()
    pub = [b for b in config.meeting_bases() if _load(b).get("route") == "public_download"]
    assert pub == ["Board Session 05212026"]


def test_apply_then_undo_restores_every_field(store):
    before = {b: _load(b) for b in config.meeting_bases()}
    r = amnesty.apply()
    u = amnesty.undo(r["run"], dry_run=False)
    assert u["kept"] == [] and u["missing"] == []
    assert {b: _load(b) for b in config.meeting_bases()} == before


def test_an_undo_dry_run_writes_nothing(store):
    r = amnesty.apply()
    after = _snapshot()
    u = amnesty.undo(r["run"], dry_run=True)
    assert u["restored"] and _snapshot() == after


def test_a_title_edited_after_the_amnesty_survives_undo(store):
    r = amnesty.apply()
    res = summarize.rename_meeting("Recording 10042026 1344", "Hiring Sync")
    assert res["ok"]
    new_base = res["base"]
    u = amnesty.undo(r["run"], dry_run=False)
    d = _load(new_base)
    # the person's edit wins over the undo, field by field
    assert (d["title_state"], d["title_source"]) == ("confirmed", "manual")
    assert {"base": new_base, "field": "title_state"} in u["kept"]
    # the route the amnesty filled in still comes back off
    assert "route" not in d


def test_undo_one_field_leaves_the_others(store):
    r = amnesty.apply()
    amnesty.undo(r["run"], fields=["title_state"], dry_run=False)
    d = _load("Budget Review 10012026")
    assert "title_state" not in d
    assert d["title_source"] == "label" and d["route"] == "voice_memo"


def test_an_active_meeting_is_skipped_and_reported(store, capsys):
    status.start_run(["Budget Review.m4a"])
    status.set_stage("Budget Review.m4a", "transcribing", base="Budget Review 10012026")
    before = config.meeting_file("Budget Review 10012026", ".json").read_bytes()
    r = amnesty.apply()
    assert {"base": "Budget Review 10012026", "why": "being processed"} in r["skipped"]
    assert config.meeting_file("Budget Review 10012026", ".json").read_bytes() == before
    amnesty.main([])
    assert "being processed: Budget Review 10012026" in capsys.readouterr().out


def test_a_second_apply_changes_nothing(store):
    amnesty.apply()
    after = _snapshot()
    r = amnesty.apply()
    assert r["applied"] == [] and _snapshot() == after


def test_the_silent_summary_comes_back_on_undo(store):
    r = amnesty.apply()
    amnesty.undo(r["run"], fields=["ai_summary"], dry_run=False)
    d = _load("Quiet Room 10062026")
    assert d["ai_summary"] == "An invented summary." and d["no_speech"] is True


def test_field_without_undo_is_refused(store):
    with pytest.raises(SystemExit):
        amnesty.main(["--field", "route"])

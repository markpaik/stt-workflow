"""Phase 3 store keys: the short-date parser, the date evidence in
stt/provenance.py, the backfill tool, the date-correction tool, and the
`title` and `date_source` upkeep in apply_meeting_edits. Synthetic data
only."""
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from stt import config, dates, provenance, status, summarize, titles
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import backfill_keys  # noqa: E402
import fix_dates  # noqa: E402

SPOKEN = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "we will ship it"}]


def _ts(iso_local: str) -> float:
    return datetime.fromisoformat(iso_local).timestamp()


def _m(base, *, date="2026-09-29", mtime="2026-09-29T18:00:00", audio=True, **extra):
    d = {"meeting_id": "m-" + hashlib.sha1(base.encode()).hexdigest()[:10],
         "source_file": f"{base}.m4a", "date": date,
         "source_mtime": _ts(mtime) if mtime else None,
         "duration_sec": 600.0,
         "processed_at": "2026-09-29T19:00:00", "arrived_at": "2026-09-29T19:00:00",
         "generated_at": "2026-09-29T19:05:00", "reviewed": False,
         "speakers": [], "segments": SPOKEN, "words": []}
    if mtime is None:
        d.pop("source_mtime")
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text("stub")
    if audio:
        a = mfile(base, ".m4a")
        a.write_bytes(b"synthetic audio " + base.encode())
        if mtime:
            os.utime(a, (_ts(mtime), _ts(mtime)))
    return d


def _load(base):
    return json.loads(config.meeting_file(base, ".json").read_text())


def _snapshot():
    root = config.meetings_dir()
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------- the parser ----------

@pytest.mark.parametrize("name,ref,want", [
    ("Weekly Sync 9.14", "2026-09-29", "2026-09-14"),
    ("Site Visit 10-5", "2026-10-30", "2026-10-05"),
    ("Team Check 10:21", "2026-10-30", "2026-10-21"),
    ("Planning 9.14.26", "2026-09-29", "2026-09-14"),
    ("Year End 12.30", "2027-01-08", "2026-12-30"),      # a year boundary resolves to the past
    ("Kickoff 9/2", "2026-09-29", "2026-09-02"),
])
def test_short_dates_parse_to_their_most_recent_past_day(name, ref, want):
    assert dates.short_date(name, ref) == want


@pytest.mark.parametrize("name,ref", [
    ("Budget Review 09292026", "2026-09-29"),   # an 8-digit stamp is not a short date
    ("Weekly Sync 9.30", "2026-09-29"),         # after the reference day, so last year: too far back
    ("Version 1.5 Review", "2026-09-29"),       # resolves to January: past the window
    ("Room 13.40", "2026-09-29"),               # no month 13
    ("Plain Title", "2026-09-29"),
    ("Weekly Sync 9.14", "not a date"),
])
def test_what_is_not_a_short_date(name, ref):
    assert dates.short_date(name, ref) is None


def test_title_of_peels_the_stamp_and_the_twin_suffix():
    assert dates.title_of("Weekly Sync 10302026 (2)") == "Weekly Sync"
    assert dates.title_of("Budget Review 10012026") == "Budget Review"
    assert dates.title_of("Recording 07112026 1814") == "Recording 07112026 1814"
    base = "Recording 07112026 1814"
    assert dates.restamp(dates.title_of(base), "2026-07-11") == base


# ---------- the tag check ----------

def _probe(start_utc, uuid=True, dur=600.0):
    tags = {"creation_time": start_utc}
    if uuid:
        tags["voice-memo-uuid"] = "00000000-synthetic"
    return {"tags": tags, "duration": dur}


def test_a_tag_that_ends_before_the_file_was_written_is_a_start_time():
    mt = datetime.fromisoformat("2026-10-29T12:00:00+00:00").timestamp()
    t = provenance.tag_start(_probe("2026-10-23T15:00:00.000000Z"), 600.0, mt)
    assert t is not None and t.utcoffset() is not None
    assert t.astimezone().isoformat().startswith(
        datetime.fromisoformat("2026-10-23T15:00:00+00:00").astimezone().date().isoformat())


def test_a_tag_near_the_export_time_is_rejected():
    """On an older export the tag is the export time: the recording would
    then end after the file was written."""
    mt = datetime.fromisoformat("2026-10-29T12:00:00+00:00").timestamp()
    near = "2026-10-29T11:59:30.000000Z"
    assert provenance.tag_start(_probe(near), 600.0, mt) is None


def test_a_tag_needs_the_voice_memo_uuid_and_a_file_time():
    mt = datetime.fromisoformat("2026-10-29T12:00:00+00:00").timestamp()
    assert provenance.tag_start(_probe("2026-10-23T15:00:00Z", uuid=False), 600.0, mt) is None
    assert provenance.tag_start(_probe("2026-10-23T15:00:00Z"), 600.0, None) is None


def test_the_tag_tolerance_is_sixty_seconds():
    mt = datetime.fromisoformat("2026-10-29T12:00:00+00:00").timestamp()
    ok = "2026-10-29T11:51:00Z"      # ends 60 s after the file time
    late = "2026-10-29T11:51:01Z"    # ends 61 s after
    assert provenance.tag_start(_probe(ok), 600.0, mt) is not None
    assert provenance.tag_start(_probe(late), 600.0, mt) is None


# ---------- explain and correct ----------

def test_the_stored_date_is_explained_not_changed():
    c = {"typed": ("2026-09-01", None), "mtime": ("2026-09-05", None)}
    assert provenance.explain("2026-09-01", c) == ("typed", None)
    assert provenance.explain("2026-09-03", c) == ("manual", None)
    c = {"mtime": ("2026-09-05", None)}
    assert provenance.explain("2026-09-05", c) == ("mtime", None)
    assert provenance.explain("2026-09-03", c) == ("manual", None)
    # thinner evidence stays cautious
    assert provenance.explain("2026-09-03", c, raw_known=False,
                              folder_iso="2026-09-03") == ("typed", None)
    assert provenance.explain("2026-09-03", c, raw_known=False) == ("mtime", None)
    assert provenance.explain("2026-09-03", c, mtime_known=False) == ("mtime", None)


def test_a_typed_or_manual_date_is_never_corrected():
    c = {"short_typed": ("2026-09-14", None), "mtime": ("2026-09-29", None)}
    assert provenance.correction({"date": "2026-09-29", "date_source": "manual"}, c) is None
    assert provenance.correction({"date": "2026-09-29", "date_source": "typed"}, c) is None
    assert provenance.correction({"date": "2026-09-29", "date_source": "mtime"}, c) == (
        "short_typed", "2026-09-14", None)


def test_the_recorder_wins_over_a_short_date_that_agrees_on_the_day():
    c = {"recorder": ("2026-10-05", "2026-10-05T15:20:00-04:00"),
         "short_typed": ("2026-10-05", None), "mtime": ("2026-10-30", None)}
    assert provenance.correction({"date": "2026-10-30"}, c)[0] == "recorder"
    assert provenance.correction({"date": "2026-10-05"}, c) is None


# ---------- the backfill tool ----------

@pytest.fixture
def store(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(tmp_path / "no-public.json"))
    monkeypatch.setattr(provenance, "probe_tags", lambda a: {"tags": {}, "duration": None})
    _m("Budget Review 09292026", source_file="Budget Review 09292026.m4a")
    _m("Weekly Sync 9.14 09292026", source_file="Weekly Sync 9.14.m4a")
    _m("Team Huddle 09292026", source_file="Team Huddle.m4a")
    _m("Renamed One 09012026", date="2026-09-01", renamed_from="Old Name 09292026",
       source_file="Renamed One 09012026.m4a")
    _m("Edited Date 09032026", date="2026-09-03", source_file="Edited Date.m4a",
       title_state="confirmed")
    return tmp_path


def test_the_backfill_dry_run_counts_and_writes_nothing(store, capsys):
    before = _snapshot()
    mig = store / "migration"
    backfill_keys.main([])
    out = capsys.readouterr().out
    assert "5 would change" in out
    assert "title 5" in out and "audio_fp 5" in out
    assert _snapshot() == before
    assert not mig.exists()
    assert not (config.meetings_dir() / ".locks").exists()


def test_the_backfill_adds_the_keys_and_leaves_everything_else(store):
    before = {b: _load(b) for b in config.meeting_bases()}
    r = backfill_keys.apply()
    assert len(r["applied"]) == 5
    for b, old in before.items():
        new = _load(b)
        added = set(new) - set(old)
        assert added == set(provenance.KEYS), b
        for k, v in old.items():
            assert new[k] == v, (b, k)        # generated_at included
        assert new["title"] == dates.title_of(b)
        assert new["audio_fp"]
    assert _load("Budget Review 09292026")["date_source"] == "typed"
    assert _load("Team Huddle 09292026")["date_source"] == "mtime"
    assert _load("Team Huddle 09292026")["source_name"] == "Team Huddle.m4a"
    assert _load("Renamed One 09012026")["source_name"] is None
    assert _load("Renamed One 09012026")["date_source"] == "typed"   # stamp, raw unknown
    assert _load("Edited Date 09032026")["date_source"] == "manual"
    assert _load("Weekly Sync 9.14 09292026")["date_source"] == "mtime"


def test_the_backfill_leaves_needs_you_alone(store):
    before = {b: (titles.needs_title(_load(b)), _load(b).get("arrived_at"),
                  _load(b).get("processed_at")) for b in config.meeting_bases()}
    backfill_keys.apply()
    after = {b: (titles.needs_title(_load(b)), _load(b).get("arrived_at"),
                 _load(b).get("processed_at")) for b in config.meeting_bases()}
    assert before == after


def test_the_backfill_never_recomputes_a_frozen_key(store):
    j = config.meeting_file("Team Huddle 09292026", ".json")
    d = json.loads(j.read_text())
    d["audio_fp"], d["source_name"] = "fp-kept", "kept.m4a"
    j.write_text(json.dumps(d))
    backfill_keys.apply()
    d = _load("Team Huddle 09292026")
    assert (d["audio_fp"], d["source_name"]) == ("fp-kept", "kept.m4a")


def test_the_backfill_undo_removes_only_what_it_wrote(store):
    before = {b: _load(b) for b in config.meeting_bases()}
    r = backfill_keys.apply()
    # a person edits one key after the run: undo keeps the edit
    j = config.meeting_file("Team Huddle 09292026", ".json")
    d = json.loads(j.read_text())
    d["date_source"] = "manual"
    j.write_text(json.dumps(d))
    dry = backfill_keys.undo(r["run"])
    assert dry["dry_run"] and _load("Budget Review 09292026").get("title")
    u = backfill_keys.undo(r["run"], dry_run=False)
    assert {"base": "Team Huddle 09292026", "field": "date_source"} in u["kept"]
    for b, old in before.items():
        new = _load(b)
        if b == "Team Huddle 09292026":
            new.pop("date_source")
        assert new == old, b


def test_the_backfill_undo_by_one_field(store):
    r = backfill_keys.apply()
    backfill_keys.undo(r["run"], fields=["audio_fp"], dry_run=False)
    d = _load("Budget Review 09292026")
    assert "audio_fp" not in d and "title" in d


def test_the_backfill_skips_an_active_meeting(store):
    status.start_run(["Team Huddle.m4a"])
    status.set_stage("Team Huddle.m4a", "transcribing", base="Team Huddle 09292026")
    before = config.meeting_file("Team Huddle 09292026", ".json").read_bytes()
    r = backfill_keys.apply()
    assert {"base": "Team Huddle 09292026", "why": "being processed"} in r["skipped"]
    assert config.meeting_file("Team Huddle 09292026", ".json").read_bytes() == before


def test_a_second_backfill_changes_nothing(store):
    backfill_keys.apply()
    assert backfill_keys.plan() == []


def test_the_backfill_works_on_a_store_that_already_has_the_amnesty_keys(store):
    j = config.meeting_file("Budget Review 09292026", ".json")
    d = json.loads(j.read_text())
    d.update(title_state="provisional", title_source="label", route="voice_memo")
    j.write_text(json.dumps(d))
    backfill_keys.apply()
    d = _load("Budget Review 09292026")
    assert d["title_state"] == "provisional" and d["title"] == "Budget Review"


# ---------- the date-correction tool ----------

@pytest.fixture
def dated(sandbox, monkeypatch, tmp_path):
    """Two short typed dates, one Voice Memos tag that proves an earlier
    start, one tag near the export time, one typed date, one manual date."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(tmp_path / "no-public.json"))
    _m("Weekly Sync 9.14 09292026", source_file="Weekly Sync 9.14.m4a")
    _m("Site Visit 9.17 09292026", source_file="Site Visit 9.17.m4a")
    _m("Planning Talk 10292026", date="2026-10-29", mtime="2026-10-29T12:00:00",
       source_file="Planning Talk.m4a")
    _m("Fresh Export 10292026", date="2026-10-29", mtime="2026-10-29T12:00:00",
       source_file="Fresh Export.m4a")
    _m("Typed Day 9.14 09292026", source_file="Typed Day 9.14 09292026.m4a",
       renamed_from=None)
    _m("Hand Set 9.1 09202026", date="2026-09-20", source_file="Hand Set 9.1.m4a")
    start = datetime.fromisoformat("2026-10-23T10:00:00").astimezone()
    export = datetime.fromisoformat("2026-10-29T11:59:50").astimezone()

    def probe(audio):
        name = Path(audio).name
        if name.startswith("Planning Talk"):
            return _probe(start.isoformat())
        if name.startswith("Fresh Export"):
            return _probe(export.isoformat())
        return {"tags": {}, "duration": None}
    monkeypatch.setattr(provenance, "probe_tags", probe)
    return tmp_path


def test_the_date_fix_dry_run_counts_and_writes_nothing(dated, capsys):
    before = _snapshot()
    fix_dates.main([])
    out = capsys.readouterr().out
    assert "3 corrections" in out
    assert "recorder 1, short_typed 2" in out
    assert _snapshot() == before
    assert not (dated / "migration").exists()


def test_the_date_fix_restamps_the_folder_and_records_the_source(dated):
    r = fix_dates.apply()
    now = {x["base"]: x["now"] for x in r["applied"]}
    assert now == {"Weekly Sync 9.14 09292026": "Weekly Sync 9.14 09142026",
                   "Site Visit 9.17 09292026": "Site Visit 9.17 09172026",
                   "Planning Talk 10292026": "Planning Talk 10232026"}
    for base in now.values():
        d = _load(base)
        assert dates.restamp(d["title"], d["date"]) == base
        assert d["date"] == dates.meeting_date(base[-8:])
    w = _load("Weekly Sync 9.14 09142026")
    assert (w["date"], w["date_source"], w["recorded_at"]) == ("2026-09-14", "short_typed", None)
    p = _load("Planning Talk 10232026")
    assert p["date_source"] == "recorder" and p["recorded_at"].startswith("2026-10-23T10:00:00")
    # the typed date, the hand-set date, and the export-time tag stay put
    assert _load("Typed Day 9.14 09292026")["date"] == "2026-09-29"
    assert _load("Hand Set 9.1 09202026")["date"] == "2026-09-20"
    assert _load("Fresh Export 10292026")["date"] == "2026-10-29"
    assert fix_dates.plan() == []


def test_the_date_fix_undo_puts_the_old_date_and_folder_back(dated):
    before = {b: _load(b) for b in config.meeting_bases()}
    r = fix_dates.apply()
    assert fix_dates.undo(r["run"])["restored"]          # dry run
    assert "Weekly Sync 9.14 09142026" in config.meeting_bases()
    u = fix_dates.undo(r["run"], dry_run=False)
    assert len(u["restored"]) == 3
    assert set(config.meeting_bases()) == set(before)
    for b, old in before.items():
        new = _load(b)
        assert new["date"] == old["date"], b
        assert new.get("date_source") == old.get("date_source"), b


def test_a_date_changed_after_the_fix_survives_undo(dated):
    r = fix_dates.apply()
    summarize.set_meeting_date("Site Visit 9.17 09172026", "2026-09-16")
    u = fix_dates.undo(r["run"], dry_run=False)
    assert {"base": "Site Visit 9.17 09162026"} in u["kept"]
    d = _load("Site Visit 9.17 09162026")
    assert (d["date"], d["date_source"]) == ("2026-09-16", "manual")


# ---------- apply_meeting_edits upkeep ----------

def test_a_panel_date_edit_is_manual_and_keeps_the_title_key(sandbox):
    _m("Budget Review 09292026", title="Budget Review", date_source="typed",
       recorded_at="2026-09-29T09:00:00-04:00")
    r = summarize.set_meeting_date("Budget Review 09292026", "2026-09-28")
    d = _load(r["base"])
    assert r["base"] == "Budget Review 09282026"
    assert (d["date_source"], d["title"], d["recorded_at"]) == ("manual", "Budget Review", None)


def test_a_title_edit_updates_the_title_key(sandbox):
    _m("Budget Review 09292026", title="Budget Review", date_source="mtime")
    r = summarize.rename_meeting("Budget Review 09292026", "Q3 Budget")
    d = _load(r["base"])
    assert d["title"] == "Q3 Budget"
    assert d["date_source"] == "mtime"            # the date did not move


def test_a_repair_passes_its_own_source_and_start_time(sandbox):
    _m("Planning Talk 10292026", date="2026-10-29")
    r = summarize.apply_meeting_edits("Planning Talk 10292026", date="2026-10-23",
                                      date_source="recorder",
                                      recorded_at="2026-10-23T10:00:00-04:00")
    d = _load(r["base"])
    assert (d["date_source"], d["recorded_at"]) == ("recorder", "2026-10-23T10:00:00-04:00")


def test_apply_meeting_edits_refuses_a_bad_source_or_start(sandbox):
    _m("Planning Talk 10292026", date="2026-10-29")
    assert not summarize.apply_meeting_edits(
        "Planning Talk 10292026", date="2026-10-23", date_source="phone")["ok"]
    assert not summarize.apply_meeting_edits(
        "Planning Talk 10292026", date="2026-10-23",
        recorded_at="2026-10-22T10:00:00-04:00")["ok"]


# ---------- the pipeline keeps `title` equal to the folder ----------

def test_the_pipeline_writes_the_title_key_on_arrival_and_on_a_redo(sandbox, monkeypatch):
    from test_title_rules import _process
    src = config.PROJECT_DIR / "Team Huddle 10012026.m4a"
    src.write_bytes(b"x")
    base = _process(monkeypatch, src)["base"]
    assert _load(base)["title"] == "Team Huddle"
    # a second, different recording with the same name lands as a twin
    src2 = config.PROJECT_DIR / "Team Huddle 10012026.m4a"
    src2.write_bytes(b"xy")
    os.utime(src2, (1_700_000_000, 1_700_000_000))
    twin = _process(monkeypatch, src2)["base"]
    assert twin == "Team Huddle 10012026 (2)"
    assert _load(twin)["title"] == "Team Huddle"
    # a Redo after a rename follows the folder, not a stale stored title
    j = config.meeting_file(base, ".json")
    d = json.loads(j.read_text())
    d["title"] = "Stale Name"
    j.write_text(json.dumps(d))
    stored = config.meeting_file(base, ".m4a")
    stored.write_bytes(b"x")
    _process(monkeypatch, stored)
    assert _load(base)["title"] == "Team Huddle"

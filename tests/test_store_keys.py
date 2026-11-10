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
import restamp_folders  # noqa: E402

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
    ("Team Check 10/21 ", "2026-10-30", "2026-10-21"),  # trailing space is fine
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
    ("Team Check 10:21", "2026-10-30"),         # a colon is a time, not a date
    # QA negatives: a date-like run that is not at the end, or is a time,
    # a range, or a version
    ("Grades 9-12 Planning", "2026-10-20"),
    ("1:1 with Lead", "2027-01-20"),
    ("Standup 9:30", "2026-10-20"),
    ("Grade 3/4 team", "2026-04-10"),
    ("v1.2 demo", "2026-01-20"),
    ("Demo v1.2", "2026-01-20"),
    # a trailing range or count, not a day: grades six to eight is not June 8
    ("Math PLC Grades 6-8", "2026-06-20"),
    ("Science Gr. 6-8", "2026-06-20"),
    ("Grade 3/4", "2026-04-10"),
    ("Kinder Team K 1-2", "2026-01-20"),
    ("Playgroup Ages 3-5", "2026-03-20"),
    ("Book Study Chapters 4-5", "2026-04-20"),
    ("Literacy Grades K-2 and 3-5", "2026-03-20"),
    ("Data Talk 6-8, 9-12", "2026-09-20"),
])
def test_what_is_not_a_short_date(name, ref):
    assert dates.short_date(name, ref) is None


def test_title_of_peels_the_stamp_and_the_twin_suffix():
    assert dates.title_of("2026-10-30 Weekly Sync (2)") == "Weekly Sync"
    assert dates.title_of("2026-10-01 Budget Review") == "Budget Review"
    assert dates.title_of("2026-07-11 Recording 1814") == "Recording 1814"
    # the older form still reads
    assert dates.title_of("Weekly Sync 10302026 (2)") == "Weekly Sync"
    assert dates.title_of("Budget Review 10012026") == "Budget Review"
    assert dates.title_of("Recording 07112026 1814") == "Recording 07112026 1814"
    for base in ("2026-07-11 Recording 1814", "2026-11-01 Retreat 12312026",
                 "2026-10-30 Weekly Sync (2)"):
        iso = dates.meeting_date(base)
        assert dates.stamp(dates.title_of(base), iso) + dates.twin_suffix(base) == base
        assert dates.restamp(base, iso) + dates.twin_suffix(base) == base


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


def test_a_torn_last_journal_line_never_stops_the_undo(store, capsys):
    """A crash inside one journal write leaves a torn last line. entries()
    called json.loads on it, so every undo of the run failed with a
    JSONDecodeError and restored nothing, not even the whole entries
    before it. The line is skipped now, as tools/iso_rename.py does."""
    before = {b: _load(b) for b in config.meeting_bases()}
    r = backfill_keys.apply()
    jp = store / "migration" / f"{r['run']}.jsonl"
    with open(jp, "a") as fh:
        fh.write('[1, 2]\n{"run": "' + r["run"] + '", "mee')
    assert backfill_keys.main(["--undo", r["run"], "--apply"]) == 0
    assert "Traceback" not in capsys.readouterr().err
    for b, old in before.items():
        assert _load(b) == old, b


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


def test_a_backfill_with_nothing_to_add_writes_no_journal(store, capsys):
    """A second --apply printed a run id, a journal path and an undo line,
    and left a zero-byte .jsonl for each run that changed nothing."""
    first = backfill_keys.apply()
    mig = store / "migration"
    journals = sorted(p.name for p in mig.glob("*.jsonl"))
    assert journals == [f"{first['run']}.jsonl"]
    assert backfill_keys.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "nothing to add" in out and "undo with" not in out and "journal:" not in out
    assert sorted(p.name for p in mig.glob("*.jsonl")) == journals
    r = backfill_keys.apply(run_id="backfill-20261108T085555")
    assert r == {"run": None, "journal": None, "applied": [], "skipped": []}
    assert not (mig / "backfill-20261108T085555.jsonl").exists()


def test_the_backfill_works_on_a_store_that_already_has_the_amnesty_keys(store):
    j = config.meeting_file("Budget Review 09292026", ".json")
    d = json.loads(j.read_text())
    d.update(title_state="provisional", title_source="label", route="voice_memo")
    j.write_text(json.dumps(d))
    backfill_keys.apply()
    d = _load("Budget Review 09292026")
    assert d["title_state"] == "provisional" and d["title"] == "Budget Review"


def test_the_backfill_undo_finds_a_meeting_renamed_after_the_run(store):
    """A store with no meeting ids journaled "meeting_id": null, so undo
    looked a meeting up by its old folder name. A rename after the run made
    it "no longer live", and its added keys stayed. apply() now stamps the
    ids first, as tools/fix_dates.py does."""
    for b in config.meeting_bases():
        j = config.meeting_file(b, ".json")
        d = json.loads(j.read_text())
        d.pop("meeting_id", None)
        j.write_text(json.dumps(d, indent=2))
    r = backfill_keys.apply()
    jp = store / "migration" / f"{r['run']}.jsonl"
    assert all(json.loads(x)["meeting_id"] for x in jp.read_text().splitlines())
    res = summarize.apply_meeting_edits("Team Huddle 09292026",
                                        to_base="Team Huddle Renamed 09292026")
    assert res.get("ok"), res
    new = res["base"]
    u = backfill_keys.undo(r["run"], dry_run=False)
    assert not u["missing"], u["missing"]
    d = _load(new)
    for k in ("source_name", "audio_fp", "date_source", "recorded_at"):
        assert k not in d, k


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
    assert now == {"Weekly Sync 9.14 09292026": "2026-09-14 Weekly Sync 9.14",
                   "Site Visit 9.17 09292026": "2026-09-17 Site Visit 9.17",
                   "Planning Talk 10292026": "2026-10-23 Planning Talk"}
    for base in now.values():
        d = _load(base)
        assert dates.stamp(d["title"], d["date"]) == base
        assert d["date"] == dates.meeting_date(base)
    w = _load("2026-09-14 Weekly Sync 9.14")
    assert (w["date"], w["date_source"], w["recorded_at"]) == ("2026-09-14", "short_typed", None)
    p = _load("2026-10-23 Planning Talk")
    assert p["date_source"] == "recorder" and p["recorded_at"].startswith("2026-10-23T10:00:00")
    # the typed date, the hand-set date, and the export-time tag stay put
    assert _load("Typed Day 9.14 09292026")["date"] == "2026-09-29"
    assert _load("Hand Set 9.1 09202026")["date"] == "2026-09-20"
    assert _load("Fresh Export 10292026")["date"] == "2026-10-29"
    assert fix_dates.plan() == []


def test_an_undo_given_another_tools_journal_is_no_such_run(dated, capsys):
    """Every journal sits in config.migration_dir() and every run id is a
    plain name, so 'fix_dates --undo amnesty-1' read an amnesty journal:
    e["new"]["date"] on a string raised TypeError, and 'backfill_keys
    --undo fixdates-1' raised KeyError 'field'. Each undo now refuses a
    journal of another shape, as it refuses an unknown run."""
    from stt import fieldjournal as fj
    mig = config.migration_dir()
    mig.mkdir(parents=True, exist_ok=True)
    base = config.meeting_bases()[0]
    (mig / "amnesty-1.jsonl").write_text(json.dumps(
        {"run": "amnesty-1", "meeting_id": None, "base": base, "field": "route",
         "old": {"$missing": True}, "new": "native"}) + "\n")
    r = fix_dates.apply()
    for call in (lambda: fix_dates.undo("amnesty-1"),
                 lambda: backfill_keys.undo(r["run"])):
        with pytest.raises(fj.NoSuchRun, match="is not a journal of"):
            call()
    assert fix_dates.main(["--undo", "amnesty-1"]) == 1
    assert "amnesty-1 is not a journal of fix_dates" in capsys.readouterr().err
    assert fix_dates.undo(r["run"])["restored"]            # its own still undoes


def test_the_date_fix_undo_puts_the_old_date_and_folder_back(dated):
    before = {b: _load(b) for b in config.meeting_bases()}
    r = fix_dates.apply()
    assert fix_dates.undo(r["run"])["restored"]          # dry run
    assert "2026-09-14 Weekly Sync 9.14" in config.meeting_bases()
    u = fix_dates.undo(r["run"], dry_run=False)
    assert len(u["restored"]) == 3
    # the undo puts the old DATE back, and a moved folder lands in the ISO form
    moved = {x["base"] for x in r["applied"]}
    where = {b: (dates.restamp(b, old["date"]) if b in moved else b)
             for b, old in before.items()}
    assert set(config.meeting_bases()) == set(where.values())
    for b, old in before.items():
        new = _load(where[b])
        assert new["date"] == old["date"], b
        assert new.get("date_source") == old.get("date_source"), b


def test_a_date_changed_after_the_fix_survives_undo(dated):
    r = fix_dates.apply()
    summarize.set_meeting_date("2026-09-17 Site Visit 9.17", "2026-09-16")
    u = fix_dates.undo(r["run"], dry_run=False)
    assert {"base": "2026-09-16 Site Visit 9.17"} in u["kept"]
    d = _load("2026-09-16 Site Visit 9.17")
    assert (d["date"], d["date_source"]) == ("2026-09-16", "manual")


def test_a_title_set_after_the_fix_survives_undo(dated, capsys):
    """Undo renamed the folder to the journaled name, which carries the
    title from before the run, so a title set after the run was lost. The
    undo now puts the old date in front of the current title."""
    r = fix_dates.apply()
    e = summarize.apply_meeting_edits("2026-09-14 Weekly Sync 9.14", title="Offsite Prep 9.14")
    assert e["ok"] and e["base"] == "2026-09-14 Offsite Prep 9.14"
    dry = fix_dates.undo(r["run"])
    assert {"base": "2026-09-14 Offsite Prep 9.14", "title_kept": True} in dry["restored"]
    fix_dates.main(["--undo", r["run"], "--apply"])
    assert "1 of those keep the title set after the run" in capsys.readouterr().out
    d = _load("2026-09-29 Offsite Prep 9.14")
    assert (d["title"], d["date"]) == ("Offsite Prep 9.14", "2026-09-29")
    assert not any("Weekly Sync" in b for b in config.meeting_bases())
    # the meetings nobody retitled come back under their journaled names
    assert "2026-09-29 Site Visit 9.17" in config.meeting_bases()
    assert "2026-10-29 Planning Talk" in config.meeting_bases()


# ---------- apply_meeting_edits upkeep ----------

def test_a_panel_date_edit_is_manual_and_keeps_the_title_key(sandbox):
    _m("Budget Review 09292026", title="Budget Review", date_source="typed",
       recorded_at="2026-09-29T09:00:00-04:00")
    r = summarize.set_meeting_date("Budget Review 09292026", "2026-09-28")
    d = _load(r["base"])
    assert r["base"] == "2026-09-28 Budget Review"
    assert (d["date_source"], d["title"], d["recorded_at"]) == ("manual", "Budget Review", None)


def test_a_title_edit_updates_the_title_key(sandbox):
    _m("Budget Review 09292026", title="Budget Review", date_source="mtime")
    r = summarize.rename_meeting("Budget Review 09292026", "Q3 Budget")
    d = _load(r["base"])
    assert d["title"] == "Q3 Budget"
    assert d["date_source"] == "mtime"            # the date did not move


@pytest.mark.parametrize("typed", ["Dana 1:1", "Q3/Q4 Review", "a" * 300, "李" * 80,
                                   "Planning Retreat 12312099"])
def test_the_title_key_and_the_folder_follow_the_store_contract(sandbox, typed):
    """docs/store-contract.md said `title` is the folder name without its
    stamp, "always to match the folder". The code keeps the typed title:
    'Dana 1:1' sits in '... Dana 1-1', and a long title is cut by bytes in
    the folder only. The contract now states that, and invariant 1 is the
    formula a reader can check."""
    from stt import names
    _m("Budget Review 09292026", title="Budget Review", date_source="mtime")
    r = summarize.rename_meeting("Budget Review 09292026", typed)
    b = r["base"]
    d = _load(b)
    assert d["title"] == names.clean_title(typed)          # the title as typed
    assert b == config.cap_name(dates.stamp(names.file_title(d["title"]), d["date"]))
    assert dates.shown_title(d["title"], b) == d["title"]

    doc = (Path(__file__).resolve().parent.parent / "docs" / "store-contract.md").read_text()
    row = next(ln for ln in doc.splitlines() if ln.startswith("| `title` |"))
    assert "always to match the folder" not in row
    assert "`names.file_title`" in row and "`config.cap_name`" in row
    assert ("`config.cap_name(dates.stamp(names.file_title(title), date))` plus the "
            "twin suffix") in doc


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
    assert twin == "2026-10-01 Team Huddle (2)"
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


# ---------- the date badge ----------

def test_the_panel_meta_carries_the_date_source(sandbox):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
    import server
    _m("Budget Review 09292026", date_source="mtime", recorded_at=None)
    meta = server._meeting_meta(config.meeting_file("Budget Review 09292026", ".json"),
                                config.meetings_dir())
    assert meta["date_source"] == "mtime" and meta["recorded_at"] is None


# ---------- QA fixes ----------

def test_a_null_source_name_fills_once_a_manifest_record_links_the_meeting(store):
    backfill_keys.apply()
    base = "Renamed One 09012026"
    assert _load(base)["source_name"] is None
    # the doctor relinks a record to this meeting after the backfill
    config.MANIFEST_PATH.write_text(json.dumps({"processed": {"Old Name.m4a": {
        "mtime": 1.0, "outputs": [str(config.meeting_file(base, ".json"))]}}}))
    rows = backfill_keys.plan()
    assert [r["changes"] for r in rows if r["base"] == base] == [
        {"source_name": "Old Name.m4a"}]
    backfill_keys.apply()
    assert _load(base)["source_name"] == "Old Name.m4a"
    # a stored name is still never replaced
    config.MANIFEST_PATH.write_text(json.dumps({"processed": {"Other.m4a": {
        "mtime": 1.0, "outputs": [str(config.meeting_file(base, ".json"))]}}}))
    assert backfill_keys.plan() == []


def test_source_file_after_a_redo_is_not_a_source_name():
    j = "/nowhere/x.json"
    # never redone: arrived_at equals processed_at
    d = {"source_file": "Raw Label.m4a", "processed_at": "2026-09-29T19:00:00",
         "arrived_at": "2026-09-29T19:00:00"}
    assert provenance.source_name("Raw Label 09292026", d, j, {}) == "Raw Label.m4a"
    # redone: the Redo wrote the stored audio's name
    d = {"source_file": "Raw Label 09292026.m4a", "processed_at": "2026-10-02T09:00:00",
         "arrived_at": "2026-09-29T19:00:00"}
    assert provenance.source_name("Raw Label 09292026", d, j, {}) is None
    # no arrived_at: a source_file equal to the folder name proves nothing
    d = {"source_file": "Raw Label 09292026.m4a", "processed_at": "2026-10-02T09:00:00"}
    assert provenance.source_name("Raw Label 09292026", d, j, {}) is None
    assert provenance.source_name("Raw Label 09292026 (2)", d, j, {}) is None
    d = {"source_file": "Raw Label.m4a", "processed_at": "2026-10-02T09:00:00"}
    assert provenance.source_name("Raw Label 09292026", d, j, {}) == "Raw Label.m4a"


def test_an_accept_with_the_stored_date_keeps_the_date_source(sandbox):
    """/api/accept_meeting's call when the person leaves the date as it was."""
    _m("Planning Talk 10052026", date="2026-10-05", date_source="recorder",
       recorded_at="2026-10-05T09:30:00-04:00")
    r = summarize.apply_meeting_edits("Planning Talk 10052026", title=None,
                                      date="2026-10-05", category=None,
                                      reviewed=True, title_state="confirmed")
    d = _load(r["base"])
    assert (d["date_source"], d["recorded_at"]) == ("recorder", "2026-10-05T09:30:00-04:00")
    _m("Guess Day 10052026", date="2026-10-05", date_source="mtime")
    r = summarize.apply_meeting_edits("Guess Day 10052026", date="2026-10-05", reviewed=True)
    assert r["base"] == "2026-10-05 Guess Day"     # the older name moves to the ISO form
    assert _load("2026-10-05 Guess Day")["date_source"] == "mtime"
    # a real change is still a person's choice
    r = summarize.apply_meeting_edits("2026-10-05 Guess Day", date="2026-10-04")
    assert _load(r["base"])["date_source"] == "manual"


def test_a_restore_keeps_the_date_source(sandbox):
    from stt import archive
    _m("Legacy Name", date="2026-10-05", date_source="typed", title="Legacy Name")
    assert archive.archive_meeting("Legacy Name")["ok"]
    r = archive.restore_meeting("Legacy Name")
    assert r["ok"] and r["base"] == "2026-10-05 Legacy Name"
    d = _load(r["base"])
    assert (d["date_source"], d["title"]) == ("typed", "Legacy Name")


def test_restamp_folders_keeps_the_date_source_and_leaves_twins_alone(sandbox, monkeypatch):
    _m("Legacy Name", date="2026-10-05", date_source="typed", title="Legacy Name")
    _m("Scan 20260715-160418", date="2026-07-15", date_source="mtime")
    _m("Team Sync 10052026", date="2026-10-05", date_source="typed")
    _m("Team Sync 10052026 (2)", date="2026-10-05", date_source="typed")
    plan = {b: w for b, w, _ in restamp_folders.plan()}
    assert plan == {"Legacy Name": "2026-10-05 Legacy Name",
                    "Scan 20260715-160418": "2026-07-15 Scan 20260715-160418",
                    "Team Sync 10052026": "2026-10-05 Team Sync",
                    "Team Sync 10052026 (2)": "2026-10-05 Team Sync (2)"}
    monkeypatch.setattr(sys, "argv", ["restamp_folders.py", "--apply"])
    assert restamp_folders.main() == 0
    assert _load("2026-10-05 Legacy Name")["date_source"] == "typed"
    assert _load("2026-07-15 Scan 20260715-160418")["date_source"] == "mtime"
    assert _load("2026-10-05 Team Sync (2)")["date_source"] == "typed"
    from stt import doctor
    assert not [f for f in doctor.check(registry=False) if f.kind == "name_mismatch"]


def test_the_date_fix_undo_restores_renamed_from(dated):
    j = config.meeting_file("Site Visit 9.17 09292026", ".json")
    d0 = json.loads(j.read_text())
    d0["renamed_from"] = "Site Visit 9.17 old name"
    j.write_text(json.dumps(d0, indent=2))
    before = {b: _load(b).get("renamed_from", "<none>") for b in config.meeting_bases()}
    r = fix_dates.apply()
    moved = {x["base"] for x in r["applied"]}
    fix_dates.undo(r["run"], dry_run=False)
    for b, rf in before.items():
        now = dates.restamp(b, _load_any_date(b)) if b in moved else b
        assert _load(now).get("renamed_from", "<none>") == rf, b


def _load_any_date(base):
    return dates.meeting_date(base)


def test_the_date_fix_undo_keeps_each_twin_on_its_own_name(sandbox, monkeypatch, tmp_path):
    """Two same-name same-date meetings both move to 9.14. Undo walks the
    journal in reverse, so the twin rule gave the plain name to the meeting
    that had been ' (2)'."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setenv("STT_PUBLIC_STATE", str(tmp_path / "no-public.json"))
    monkeypatch.setattr(provenance, "probe_tags", lambda a: {"tags": {}, "duration": None})
    a = _m("2026-09-29 Twin 9.14", source_file="Twin 9.14.m4a")
    b = _m("2026-09-29 Twin 9.14 (2)", source_file="Twin 9.14 (2).m4a")
    r = fix_dates.apply()
    assert len(r["applied"]) == 2
    fix_dates.undo(r["run"], dry_run=False)
    assert _load("2026-09-29 Twin 9.14")["meeting_id"] == a["meeting_id"]
    assert _load("2026-09-29 Twin 9.14 (2)")["meeting_id"] == b["meeting_id"]


def test_the_date_fix_undo_works_on_meetings_with_no_stored_id(dated):
    for b in config.meeting_bases():
        j = config.meeting_file(b, ".json")
        d = json.loads(j.read_text())
        d.pop("meeting_id", None)
        j.write_text(json.dumps(d, indent=2))
    before = {b: _load(b)["date"] for b in config.meeting_bases()}
    r = fix_dates.apply()
    assert len(r["applied"]) == 3
    u = fix_dates.undo(r["run"], dry_run=False)
    assert len(u["restored"]) == 3 and not u["missing"]
    assert sorted(_load(b)["date"] for b in config.meeting_bases()) == sorted(before.values())

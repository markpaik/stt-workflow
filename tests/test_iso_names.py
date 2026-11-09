"""The ISO-first naming rule, "YYYY-MM-DD <title>" with twins as
"YYYY-MM-DD <title> (2)": both name forms parse everywhere, a typed date
is handled as before, the .txt header names the meeting's date and title
and follows a rename, and tools/iso_rename.py moves an older library across
with a journal, resume and undo. Every name is synthetic."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from stt import (config, dates, groups, identify, manifest, output, provenance,
                 summarize, titles, unknowns)
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import iso_rename as ir  # noqa: E402

HEADER = "{name}  |  12.0 min  |  2 speaker(s)  |  processed 2026-10-01 09:00"


def _m(base, date="2026-10-01", mid=None, header_name=None, **extra):
    d = {"source_file": f"{base}.m4a", "date": date, "duration_sec": 720.0,
         "speakers": [], "segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                                       "text": "hello"}], "words": []}
    if mid:
        d["meeting_id"] = mid
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text(
        HEADER.format(name=header_name or f"{base}.m4a") + "\n\n[00:00] Voice 1: hello\n")
    mfile(base, ".m4a").write_bytes(b"audio")
    mfile(base, ".diar.npz").write_bytes(b"diar")
    if mid:
        config.index_set(mid, base)
    return d


def _first_line(base):
    return config.meeting_file(base, ".txt").read_text().split("\n", 1)[0]


# ---------- the rule ----------

@pytest.mark.parametrize("name,iso,title", [
    ("2026-08-03 Weekly Check-in", "2026-08-03", "Weekly Check-in"),
    ("2026-08-03 Weekly Check-in (2)", "2026-08-03", "Weekly Check-in"),
    ("Weekly Check-in 08032026", "2026-08-03", "Weekly Check-in"),      # older form
    ("Weekly Check-in 08032026 (2)", "2026-08-03", "Weekly Check-in"),
    ("2026-10-28 LT Weekly Meeting – Budget Reallocation", "2026-10-28",
     "LT Weekly Meeting – Budget Reallocation"),
])
def test_both_forms_parse_to_one_title_and_date(name, iso, title):
    assert dates.meeting_date(name) == iso
    assert dates.title_of(name) == title
    assert dates.restamp(name, iso) == f"{iso} {title}"


def test_the_iso_form_is_the_one_written():
    assert dates.stamp("Weekly", "2026-08-03") == "2026-08-03 Weekly"
    assert dates.stamp("Weekly", "not a date") == "Weekly"
    assert dates.is_iso_form("2026-08-03 Weekly (2)")
    assert not dates.is_iso_form("Weekly 08032026")
    assert not dates.is_iso_form("2026-13-03 Weekly")       # not a real date
    assert dates.stamp_date("2026-08-03 Weekly") == "2026-08-03"
    assert dates.stamp_date("Weekly 08032026") == "2026-08-03"
    assert dates.stamp_date("Recording 08032026 1814") is None   # mid-name is no stamp
    # a leading ISO stamp is THE stamp: trailing digits after it stay
    assert dates.title_of("2026-11-01 Retreat 12312026") == "Retreat 12312026"
    # a year range or a bare year is never a stamp
    assert dates.title_of("2026-27 Budget") == "2026-27 Budget"
    assert dates.strip_stamp("2026-08-03") == "2026-08-03"


def test_canonical_moves_a_typed_stamp_to_the_front():
    assert dates.canonical("Review 06152026", "2026-06-15") == "2026-06-15 Review"
    assert dates.canonical("2026-06-15 Review", "2026-06-15") == "2026-06-15 Review"
    # an explicit date replaces a past typed stamp
    assert dates.canonical("Review 06152026", "2026-06-16") == "2026-06-16 Review"
    # a FUTURE stamp names an event: it stays in the title
    assert dates.canonical("Retreat 12312026", "2026-05-01", today="2026-06-01") == \
        "2026-05-01 Retreat 12312026"
    assert dates.canonical("Plain", "2026-05-01") == "2026-05-01 Plain"
    assert dates.canonical("Review 06152026", None) == "2026-06-15 Review"
    assert dates.canonical("Plain", None) == "Plain"


def test_a_rename_keeps_title_equal_to_the_folder_without_its_date(sandbox):
    _m("Weekly Check-in 08032026", date="2026-08-03")
    r = summarize.rename_meeting("Weekly Check-in 08032026", "Kim Weekly Check-in – Plans")
    assert r["ok"] and r["base"] == "2026-08-03 Kim Weekly Check-in – Plans"
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["title"] == "Kim Weekly Check-in – Plans" == dates.title_of(r["base"])


def test_repeated_titles_on_one_day_get_the_twin_suffix(sandbox):
    _m("2026-08-03 Kim Weekly Check-in")
    _m("Voice Memo 1", date="2026-08-03")
    _m("Voice Memo 2", date="2026-08-03")
    _m("Voice Memo 3", date="2026-08-10")
    r1 = summarize.rename_meeting("Voice Memo 1", "Kim Weekly Check-in")
    r2 = summarize.rename_meeting("Voice Memo 2", "Kim Weekly Check-in")
    r3 = summarize.rename_meeting("Voice Memo 3", "Kim Weekly Check-in")
    assert r1["base"] == "2026-08-03 Kim Weekly Check-in (2)"
    assert r2["base"] == "2026-08-03 Kim Weekly Check-in (3)"
    assert r3["base"] == "2026-08-10 Kim Weekly Check-in"   # another day: no suffix
    d = json.loads(config.meeting_file(r1["base"], ".json").read_text())
    assert d["title"] == "Kim Weekly Check-in"


def test_saving_a_twin_under_its_own_title_keeps_its_number(sandbox):
    """restamp() peels the ' (N)' suffix and _unique_base took the lowest
    free number, so a no-op save moved '(3)' to '(2)' and renamed the
    folder, its id and every reference. A twin saved under the title it
    already has keeps its own number."""
    from stt import archive
    _m("2026-05-05 Race Name", date="2026-05-05", title="Race Name")
    _m("2026-05-05 Race Name (3)", date="2026-05-05", title="Race Name")
    _m("2026-05-05 Race Name (4)", date="2026-05-05", title="Race Name")
    assert archive.archive_meeting("2026-05-05 Race Name")["ok"]      # (2) is free
    for twin in ("2026-05-05 Race Name (3)", "2026-05-05 Race Name (4)"):
        r = summarize.rename_meeting(twin, "Race Name")
        assert r == {"ok": True, "renamed": [], "base": twin}
        # the typed stamp, the Keep path and a re-sent date are all no-ops too
        assert summarize.rename_meeting(twin, "2026-05-05 Race Name")["base"] == twin
        assert summarize.apply_meeting_edits(twin, title="Race Name",
                                             date="2026-05-05")["base"] == twin
        assert summarize.apply_meeting_edits(twin, title="Race Name",
                                             title_state="confirmed")["base"] == twin
    # a case-only fix on a twin keeps the number as well
    r = summarize.rename_meeting("2026-05-05 Race Name (3)", "race name")
    assert r["base"] == "2026-05-05 race name (3)"
    # a real change still takes the lowest free number
    _m("2026-05-05 Other", date="2026-05-05", title="Other")
    assert summarize.rename_meeting("2026-05-05 Other", "Race Name")["base"] == (
        "2026-05-05 Race Name (2)")


@pytest.mark.parametrize("typed,folder", [
    ("Dana Fox 1:1", "Dana Fox 1-1"),
    ("Standup 9:30", "Standup 9-30"),
    ("Vendor Demo: Assessment Platform", "Vendor Demo - Assessment Platform"),
    ("Q3/Q4 Planning", "Q3-Q4 Planning"),
    ("Agenda:", "Agenda"),
    ("Standup 9:30 01222026", "Standup 9-30"),
])
def test_a_colon_in_a_typed_title_is_kept_in_a_safe_form(sandbox, typed, folder):
    """_sanitize_name deleted the colon, so '1:1' became '11' and read as
    another meeting. On disk a colon or slash becomes a hyphen, and the
    title the panel shows keeps what was typed."""
    from gui import server as srv
    _m("Voice Memo 1", date="2026-01-22")
    r = summarize.rename_meeting("Voice Memo 1", typed)
    assert r["ok"] and r["base"] == f"2026-01-22 {folder}"
    shown = dates.title_of(typed) if typed != "Agenda:" else "Agenda:"
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["title"] == shown
    srv._meet_cache.clear()
    meta = srv._meeting_meta(config.meeting_file(r["base"], ".json"), config.meetings_dir())
    assert meta["title"] == shown


def test_an_old_folder_that_kept_its_colon_is_no_doctor_finding(sandbox):
    """A folder from before the sanitizer can hold a colon, with the same
    title. The doctor compared the folder with the title's safe form only,
    so it reported every such folder as a name mismatch."""
    from stt import doctor
    _m("2026-01-15 Vendor Demo: Assessment Platform", date="2026-01-15",
       title="Vendor Demo: Assessment Platform")
    assert not [f for f in doctor.check(registry=False) if f.kind == "name_mismatch"]
    # saving the title it already has leaves that folder where it is
    base = "2026-01-15 Vendor Demo: Assessment Platform"
    assert summarize.rename_meeting(base, "Vendor Demo: Assessment Platform") == {
        "ok": True, "renamed": [], "base": base}
    # a real change still lands on the safe form
    r = summarize.rename_meeting(base, "Vendor Demo: Scoring")
    assert r["base"] == "2026-01-15 Vendor Demo - Scoring"


def test_saving_a_colon_title_again_changes_nothing(sandbox):
    from stt import doctor
    _m("Voice Memo 1", date="2026-01-22")
    base = summarize.rename_meeting("Voice Memo 1", "Dana Fox 1:1")["base"]
    assert summarize.rename_meeting(base, "Dana Fox 1:1") == {
        "ok": True, "renamed": [], "base": base}
    # a date edit moves the folder and keeps the typed title
    r = summarize.set_meeting_date(base, "2026-01-23")
    assert r["base"] == "2026-01-23 Dana Fox 1-1"
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["title"] == "Dana Fox 1:1"
    # the doctor reads the folder as the title's safe form, not a mismatch
    assert not [f for f in doctor.check(registry=False)
                if f.kind == "name_mismatch" and f.subject == r["base"]]
    # the short-date reader sees the typed '1:1', never '1-1' as January 1
    assert "short_typed" not in provenance.candidates(
        r["base"], d, None, None, today="2026-02-01")
    # a hyphen typed on purpose replaces the colon
    r = summarize.rename_meeting(r["base"], "Dana Fox 1-1")
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert (r["renamed"], d["title"]) == ([], "Dana Fox 1-1")


def test_a_long_typed_title_fits_a_folder_name(sandbox):
    """names.file_title limits characters, not bytes: 120 CJK characters
    and the date are 371 bytes, past a macOS name, so the rename failed
    with ENAMETOOLONG and the generic 500. The folder name is now cut by
    bytes, and the title shown stays whole."""
    from stt import names
    _m("2026-07-04 Weekly", date="2026-07-04", title="Weekly")
    long = "会議" * 60
    r = summarize.rename_meeting("2026-07-04 Weekly", long)
    assert r["ok"], r
    assert len(r["base"].encode("utf-8")) <= config.MAX_BASE_BYTES
    assert r["base"].startswith("2026-07-04 会議")
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["title"] == long
    # two long titles that share the cut form are twins
    _m("2026-07-04 Other", date="2026-07-04", title="Other")
    r2 = summarize.rename_meeting("2026-07-04 Other", long + "二")
    assert r2["ok"] and r2["base"] == r["base"] + " (2)"
    assert names.file_title(long) == long      # the characters are all kept
    from stt import doctor
    assert not [f for f in doctor.check(registry=False)
                if f.kind == "name_mismatch" and f.subject.startswith("2026-07-04 会議")]


def test_a_twin_takes_the_plain_name_once_it_is_free(sandbox):
    """The keep-the-number rule kept '(3)' even after the plain name was
    deleted, so a twin could never drop its number. The number now stays
    only while the plain name is taken, live or archived."""
    from stt import archive
    _m("2026-05-05 Race Name", date="2026-05-05", title="Race Name")
    _m("2026-05-05 Race Name (3)", date="2026-05-05", title="Race Name")
    assert archive.delete_meeting("2026-05-05 Race Name")["ok"]
    r = summarize.rename_meeting("2026-05-05 Race Name (3)", "Race Name")
    assert r["base"] == "2026-05-05 Race Name"


@pytest.mark.parametrize("typed", ["Board Prep 07092026", "2026-07-09 Board Prep"])
def test_a_title_typed_with_a_date_sets_the_date(sandbox, typed):
    _m("Voice Memo 9", date="2026-07-20")
    r = summarize.rename_meeting("Voice Memo 9", typed)
    assert r["ok"] and r["base"] == "2026-07-09 Board Prep"
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert (d["date"], d["date_source"], d["title"]) == ("2026-07-09", "manual", "Board Prep")


def test_the_date_picker_beats_a_typed_stamp(sandbox):
    _m("Voice Memo 9", date="2026-07-20")
    r = summarize.apply_meeting_edits("Voice Memo 9", title="Board Prep 07092026",
                                      date="2026-07-11")
    assert r["ok"] and r["base"] == "2026-07-11 Board Prep"


# ---------- every reader takes both forms ----------

def test_the_recorder_name_reads_in_both_forms(sandbox):
    for stem in ("2026-07-11 Recording 1814", "Recording 07112026 1814"):
        assert titles.route_for(f"{stem}.m4a") == "native"
        assert not titles.is_label(stem)
        assert groups._stamp_hour(stem) == 18
        c = provenance.candidates(stem, {}, f"{stem}.m4a", None, probe=None,
                                  today="2026-11-01")
        assert c["recorder"][0] == "2026-07-11"
        assert "T18:14:00" in c["recorder"][1]


def test_the_panel_title_strips_either_stamp(sandbox):
    from gui import server as srv
    assert srv._display_title("2026-08-03 Kim Weekly Check-in") == "Kim Weekly Check-in"
    assert srv._display_title("Kim Weekly Check-in 08032026") == "Kim Weekly Check-in"


def test_a_typed_name_in_either_form_is_typed(sandbox, tmp_path):
    for name in ("Budget Review 10012026.m4a", "2026-10-01 Budget Review.m4a"):
        c = provenance.candidates("x", {}, name, None, probe=None, today="2026-11-01")
        assert c["typed"] == ("2026-10-01", None)


def test_the_doctor_reads_an_iso_folder_as_in_step(sandbox):
    from stt import doctor
    _m("2026-08-03 Kim Weekly Check-in", date="2026-08-03", title="Kim Weekly Check-in")
    _m("2026-08-03 Kim Weekly Check-in (2)", date="2026-08-03", title="Kim Weekly Check-in")
    _m("Old Form 08032026", date="2026-08-03", title="Old Form")
    mm = {f.subject: f.detail for f in doctor.check(registry=False)
          if f.kind == "name_mismatch"}
    assert list(mm) == ["Old Form 08032026"]
    assert "iso_rename" in mm["Old Form 08032026"]


# ---------- the .txt header ----------

def test_the_header_names_the_date_and_title(sandbox):
    assert output.header_name("2026-08-03 Kim Weekly (2)", {}) == "2026-08-03 Kim Weekly"
    assert output.header_name("Kim Weekly 08032026", {}) == "2026-08-03 Kim Weekly"
    assert output.header_name("x", {"title": "Kim Weekly", "date": "2026-08-04"}) == \
        "2026-08-04 Kim Weekly"
    assert output.header_name("No Date", {}) == "No Date"


def test_a_rename_and_a_date_edit_refresh_the_header(sandbox):
    _m("Weekly Check-in 08032026", date="2026-08-03",
       header_name="Weekly Check-in.m4a")
    r = summarize.rename_meeting("Weekly Check-in 08032026", "Kim Weekly Check-in")
    assert _first_line(r["base"]) == HEADER.format(name="2026-08-03 Kim Weekly Check-in")
    r = summarize.set_meeting_date(r["base"], "2026-08-05")
    assert _first_line(r["base"]) == HEADER.format(name="2026-08-05 Kim Weekly Check-in")
    body = config.meeting_file(r["base"], ".txt").read_text()
    assert body.endswith("\n\n[00:00] Voice 1: hello\n")     # the transcript is untouched
    # the model still never reads the header line
    assert "Kim Weekly" not in summarize._model_text(r["base"])


def test_a_txt_without_a_header_is_left_alone(sandbox, tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("[00:00] Voice 1: hello\n")
    assert output.refresh_txt_header(p, "2026-08-03 X") is False
    assert p.read_text() == "[00:00] Voice 1: hello\n"


def test_the_pipeline_header_names_the_meeting(sandbox, monkeypatch, tmp_path):
    import subprocess

    from stt import pipeline
    from stt.audio import FFMPEG
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    src = tmp_path / "Kim Weekly Check-in 08032026.m4a"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-ac", "1", "-c:a", "aac",
                    str(src)], check=True, capture_output=True)
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    assert res["base"] == "2026-08-03 Kim Weekly Check-in"
    assert _first_line(res["base"]).startswith("2026-08-03 Kim Weekly Check-in  |  ")


# ---------- tools/iso_rename.py ----------

@pytest.fixture
def lib(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    _m("Kim Weekly Check-in – Plans 08032026", date="2026-08-03", mid="mid-a",
       title="Kim Weekly Check-in – Plans", header_name="Kim Weekly Check-in.m4a")
    _m("Kim Weekly Check-in – Plans 08032026 (2)", date="2026-08-03", mid="mid-b",
       title="Kim Weekly Check-in – Plans")
    _m("Recording 08042026 0930", date="2026-08-04", mid="mid-c")
    _m("Drifted 08052026", date="2026-08-06", mid="mid-d")
    _m("2026-08-07 Already Done", date="2026-08-07", mid="mid-e",
       header_name="2026-08-07 Already Done")
    # an ISO name that a stale header still names by the old file
    _m("2026-08-08 Header Only", date="2026-08-08", mid="mid-f",
       header_name="Header Only.m4a")
    # a meeting another person's voice was heard in, and the manifest entry
    old = "Kim Weekly Check-in – Plans 08032026"
    identify.enroll("Dana Fox", np.random.default_rng(1).normal(size=32), source=old)
    unknowns.save({"speakers": {"U001": {"file": "U001.npy", "meetings": [old],
                                         "meeting_ids": ["mid-a"]}}})
    src = config.source_dir() / "Kim Weekly Check-in.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio")
    m = manifest.load()
    manifest.mark(m, src.name, src.stat().st_mtime,
                  [config.meeting_file(old, ".txt"), config.meeting_file(old, ".json")])
    manifest.save(m)
    return src


def _tree():
    root = config.meetings_dir()
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file() and ".locks" not in p.parts)


def test_the_dry_run_lists_old_to_new_and_changes_nothing(lib, capsys):
    before = _tree()
    assert ir.main([]) == 0
    out = capsys.readouterr().out
    assert _tree() == before
    assert not ir.journal_path().exists()
    assert "DRY RUN" in out and str(ir.plan_path()) in out
    rows = {r["old_base"]: r for r in json.loads(ir.plan_path().read_text())["rows"]}
    a = rows["Kim Weekly Check-in – Plans 08032026"]
    assert a["new_base"] == "2026-08-03 Kim Weekly Check-in – Plans" and a["rename"]
    assert a["header"] is True
    b = rows["Kim Weekly Check-in – Plans 08032026 (2)"]
    assert b["new_base"] == "2026-08-03 Kim Weekly Check-in – Plans (2)"
    assert not any("collision" in f for f in b["flags"])    # it was a twin already
    c = rows["Recording 08042026 0930"]
    assert c["new_base"] == "2026-08-04 Recording 0930"
    assert any(f.startswith("title changes") for f in c["flags"])
    d = rows["Drifted 08052026"]
    assert d["new_base"] == "2026-08-06 Drifted"          # the stored date wins
    assert any("differs from stored date" in f for f in d["flags"])
    assert rows["2026-08-07 Already Done"]["rename"] is False
    assert rows["2026-08-07 Already Done"]["header"] is False
    h = rows["2026-08-08 Header Only"]
    assert h["rename"] is False and h["header"] is True
    assert f"{a['old_base']}  ->  {a['new_base']}" in out
    import stat
    assert stat.S_IMODE(ir.plan_path().stat().st_mode) == 0o600


def test_the_dry_run_flags_no_title_change_the_apply_step_keeps(sandbox,
                                                               monkeypatch, tmp_path):
    """A title '1:1' on a folder in the old form ('Kim 1-1 08032026')
    keeps '1:1' when the apply step moves it, so the dry run's
    "title changes: '1:1' -> '1-1'" flagged a change that never lands."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    _m("Kim 1-1 08032026", date="2026-08-03", mid="mid-k", title="Kim 1:1")
    assert ir.main([]) == 0
    rows = {r["old_base"]: r for r in json.loads(ir.plan_path().read_text())["rows"]}
    r = rows["Kim 1-1 08032026"]
    assert r["new_base"] == "2026-08-03 Kim 1-1" and r["new_title"] == "Kim 1:1"
    assert not any(f.startswith("title changes") for f in r["flags"])
    assert ir.main(["--apply"]) == 0
    d = json.loads(config.meeting_file("2026-08-03 Kim 1-1", ".json").read_text())
    assert d["title"] == "Kim 1:1"


def test_a_collision_with_an_iso_name_gets_the_twin_suffix(lib):
    _m("2026-08-03 Kim Weekly Check-in – Plans (3)", date="2026-08-03")
    _m("2026-08-09 Standup", date="2026-08-09")
    _m("Standup 08092026", date="2026-08-09")
    rows = {r["old_base"]: r for r in ir.plan(log=lambda *_: None)["rows"]}
    assert rows["Standup 08092026"]["new_base"] == "2026-08-09 Standup (2)"
    assert any("collision" in f for f in rows["Standup 08092026"]["flags"])
    ir.apply(log=lambda *_: None)
    assert config.meeting_dir("2026-08-09 Standup (2)").is_dir()
    assert config.meeting_dir("2026-08-09 Standup").is_dir()


def test_apply_renames_follows_the_stores_and_rewrites_headers(lib):
    ir.plan(log=lambda *_: None)
    r = ir.apply(log=lambda *_: None)
    assert r["ok"], r
    assert r["counts"]["renamed"] == 4 and r["counts"]["header only"] == 2
    new = "2026-08-03 Kim Weekly Check-in – Plans"
    assert config.base_of("mid-a") == new
    assert config.base_of("mid-b") == new + " (2)"
    assert config.base_of("mid-c") == "2026-08-04 Recording 0930"
    assert config.base_of("mid-d") == "2026-08-06 Drifted"
    assert sorted(p.name for p in config.meeting_dir(new).iterdir()) == [
        f"{new}.diar.npz", f"{new}.json", f"{new}.m4a", f"{new}.txt"]
    d = json.loads(config.meeting_file(new, ".json").read_text())
    assert d["title"] == "Kim Weekly Check-in – Plans" and d["date"] == "2026-08-03"
    assert "date_source" not in d          # the same date: no new date source
    # registries, voiceprint references and the manifest followed the folder
    assert identify.load_registry()["Dana Fox"]["sources"] == [new]
    assert unknowns.load()["speakers"]["U001"]["meetings"] == [new]
    assert manifest.is_processed(manifest.load(), lib.name, lib.stat().st_mtime)
    # every header names the date and title
    for base in config.meeting_bases():
        assert _first_line(base).startswith(output.header_name(base, {}) + "  |  ")
    # resumable: a second run finds everything done and changes nothing
    before = _tree()
    r2 = ir.apply(log=lambda *_: None)
    assert r2["counts"] == {"already done": 6}
    assert _tree() == before


def test_apply_resumes_a_rename_that_landed_before_it_was_journaled(lib):
    ir.plan(log=lambda *_: None)
    real = summarize.apply_meeting_edits
    seen = []

    def stop_after_first(base, **kw):
        res = real(base, **kw)
        seen.append(base)
        if len(seen) == 1:
            raise KeyboardInterrupt      # the run dies right after the move
        return res
    summarize.apply_meeting_edits = stop_after_first
    try:
        with pytest.raises(KeyboardInterrupt):
            ir.apply(log=lambda *_: None)
    finally:
        summarize.apply_meeting_edits = real
    r = ir.apply(log=lambda *_: None)
    assert r["ok"] and r["counts"].get("failed") is None
    assert config.base_of("mid-a") == "2026-08-03 Kim Weekly Check-in – Plans"
    assert sum(1 for e in ir.journal_entries() if e.get("recovered")) == 1


def test_apply_refuses_while_the_batch_or_a_relabel_runs(lib, monkeypatch):
    from stt import control, status
    ir.plan(log=lambda *_: None)
    status.STATUS_PATH.write_text(json.dumps({"running": True}))
    r = ir.apply(log=lambda *_: None)
    assert r["ok"] is False and "batch is running" in r["error"]
    assert ir.main(["--apply"]) == 2
    status.STATUS_PATH.write_text(json.dumps({"running": False}))
    monkeypatch.setattr(control, "relabel_running", lambda: True)
    r = ir.apply(log=lambda *_: None)
    assert r["ok"] is False and "relabel" in r["error"]
    assert config.base_of("mid-a") == "Kim Weekly Check-in – Plans 08032026"
    assert not ir.journal_path().exists()


def test_apply_skips_a_meeting_renamed_since_the_dry_run(lib):
    ir.plan(log=lambda *_: None)
    assert summarize.rename_meeting("Drifted 08052026", "Someone Renamed It")["ok"]
    r = ir.apply(log=lambda *_: None)
    assert r["counts"]["renamed since the dry run"] == 1
    assert config.base_of("mid-d") == "2026-08-06 Someone Renamed It"


def test_undo_puts_names_fields_and_headers_back(lib):
    before = {b: (json.loads(config.meeting_file(b, ".json").read_text()), _first_line(b))
              for b in config.meeting_bases()}
    ir.plan(log=lambda *_: None)
    ir.apply(log=lambda *_: None)
    plan = ir.undo(dry=True, log=lambda *_: None)
    assert plan["counts"]["would undo"] == 6
    assert config.base_of("mid-a") != "Kim Weekly Check-in – Plans 08032026"
    r = ir.undo(dry=False, log=lambda *_: None)
    assert r["counts"]["undone"] == 6
    assert sorted(config.meeting_bases()) == sorted(before)
    for b, (d0, h0) in before.items():
        d = json.loads(config.meeting_file(b, ".json").read_text())
        for k in ("title", "source_file", "date"):
            assert d.get(k) == d0.get(k), (b, k)
        assert "renamed_from" not in d
        assert _first_line(b) == h0
    old = "Kim Weekly Check-in – Plans 08032026"
    assert identify.load_registry()["Dana Fox"]["sources"] == [old]
    assert manifest.is_processed(manifest.load(), lib.name, lib.stat().st_mtime)
    # undone meetings can be moved again
    ir.plan(log=lambda *_: None)
    assert ir.apply(log=lambda *_: None)["counts"]["renamed"] == 4


def test_undo_takes_off_a_date_the_move_added(sandbox, monkeypatch, tmp_path):
    """A legacy folder whose json has no 'date': apply stored the name's
    date with date_source 'typed', and undo left both behind."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    _m("Board Prep Session 09102025", mid="mid-z")
    j = config.meeting_file("Board Prep Session 09102025", ".json")
    d0 = json.loads(j.read_text())
    d0.pop("date")
    j.write_text(json.dumps(d0, indent=2))

    ir.plan(log=lambda *_: None)
    ir.apply(log=lambda *_: None)
    moved = json.loads(config.meeting_file(config.base_of("mid-z"), ".json").read_text())
    assert moved["date"] == "2025-09-10" and moved["date_source"] == "typed"
    assert ir.undo(dry=False, log=lambda *_: None)["counts"]["undone"] == 1

    d = json.loads(j.read_text())
    assert "date" not in d and "date_source" not in d and "renamed_from" not in d


def test_a_date_outside_2000_to_next_year_is_refused(sandbox):
    """0001-01-01 was accepted and renamed the folder. A year outside
    dates._valid's 2000-2099 is not read back as a stamp either, so the next
    edit stacked a second one: '2999-01-01 1999-01-01 Board Prep Session'."""
    from datetime import date
    _m("2026-07-04 Board Prep Session", date="2026-07-04")
    last = date.today().year + 1
    for bad in ("0001-01-01", "1999-12-31", "2999-01-01", f"{last + 1}-01-01"):
        r = summarize.set_meeting_date("2026-07-04 Board Prep Session", bad)
        assert r == {"ok": False, "error": f"date should be between 2000 and {last}"}, bad
        r = summarize.apply_meeting_edits("2026-07-04 Board Prep Session", date=bad)
        assert r["ok"] is False
    assert config.meeting_bases() == ["2026-07-04 Board Prep Session"]
    # both ends of the range still land, and each edit replaces the stamp
    r = summarize.set_meeting_date("2026-07-04 Board Prep Session", "2000-01-01")
    assert r["ok"] and r["base"] == "2000-01-01 Board Prep Session"
    r = summarize.set_meeting_date(r["base"], f"{last}-12-31")
    assert r["ok"] and r["base"] == f"{last}-12-31 Board Prep Session"


def test_the_classic_date_inputs_carry_the_same_range():
    js = (Path(__file__).resolve().parent.parent / "gui/static/app.js").read_text()
    # summarize.apply_meeting_edits takes 2000 to next year
    assert js.count('min="2000-01-01" max="${new Date().getFullYear()+1}-12-31"') == 3


def test_a_title_loses_bidi_controls_and_zero_width_spaces(sandbox):
    """Rename accepted U+202E: the folder '2025-05-28 <U+202E>gnp.txt' showed
    in Finder as '2025-05-28 txt.png'. A title strips the same characters a
    person name refuses, and a line break becomes a space."""
    _m("2025-05-28 Planning", date="2025-05-28")
    r = summarize.rename_meeting("2025-05-28 Planning", "‮gnp.txt")
    assert r["ok"] and r["base"] == "2025-05-28 gnp.txt"
    r = summarize.rename_meeting(r["base"], "Budget​ Review⁦⁩")
    assert r["base"] == "2025-05-28 Budget Review"
    r = summarize.rename_meeting(r["base"], "Budget\nReview Notes\x85End")
    assert r["base"] == "2025-05-28 Budget Review Notes End"
    for f in config.meeting_dir(r["base"]).iterdir():
        assert not any(c in f.name for c in "‮​⁦⁩\n")
    # a joiner is part of real text and stays
    assert summarize._sanitize_name("Caf‍e") == "Caf‍e"

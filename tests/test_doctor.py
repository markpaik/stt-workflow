"""stt/doctor.py and stt/housekeeping.py on a synthetic store: check never
writes, fix copies and journals before it writes, undo restores only what
still holds the fix's output, and the logs and the calibration log stay
under their caps."""
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from stt import config, control, doctor, holds, housekeeping, status
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import doctor as doctor_cli  # noqa: E402

SPOKEN = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "hello"}]


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


def _m(base, **extra):
    d = {"meeting_id": "m-" + hashlib.sha1(base.encode()).hexdigest()[:10],
         "source_file": f"{base}.m4a", "date": "2026-09-29",
         "generated_at": "2026-09-29T19:05:00", "speakers": [], "segments": SPOKEN,
         "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text("stub")
    return d


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()
            and "migration" not in p.parts and not p.name.endswith(".lock")}


@pytest.fixture
def lib(sandbox, monkeypatch, tmp_path):
    """Two stale manifest records that relink (one by time, one by title
    and date), one that retires, one flat path, one meeting with no record
    but with its own keys, a stale hold, a stale dismissal, a stale stop
    flag, and a folder whose name disagrees with its title."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    store = config.meetings_dir()
    _m("Budget Review 09292026", source_mtime=_ts("2026-09-29T18:00:00"))
    _m("Weekly Team Sync 09232026", date="2026-09-23")
    _m("Flat Era 05012026", date="2026-05-01")
    _m("Fresh Arrival 09302026", date="2026-09-30", source_name="Fresh Arrival.m4a",
       source_mtime=_ts("2026-09-30T08:00:00"))
    _m("Odd Folder", date="2026-09-01", title="Odd Folder")
    _m("Kept Linked 09012026", date="2026-09-01")
    old = config.PROJECT_DIR / "old"
    m = {"processed": {
        "budget.m4a": {"mtime": _ts("2026-09-29T18:00:00"),
                       "outputs": [str(old / "Budget 09292026" / "Budget 09292026.json"),
                                   str(old / "Budget 09292026" / "Budget 09292026.txt")]},
        "Weekly Team Sync.m4a": {"mtime": _ts("2026-09-23T17:00:00"),
                                 "outputs": [str(old / "Weekly Sync" / "Weekly Sync.json")]},
        "gone.m4a": {"mtime": _ts("2026-08-01T10:00:00"),
                     "outputs": [str(old / "Gone 08012026" / "Gone 08012026.json")]},
        "Flat Era.m4a": {"mtime": 1.0,
                         "outputs": [str(store / "Flat Era 05012026.json"),
                                     str(store / "Flat Era 05012026.txt")]},
        "Kept Linked.m4a": {"mtime": 2.0, "outputs": [
            str(config.meeting_file("Kept Linked 09012026", ".json"))]},
    }}
    config.MANIFEST_PATH.write_text(json.dumps(m, indent=2))
    holds.PATH.write_text(json.dumps(["Not Here.m4a"]))
    (config.PROJECT_DIR / "dismissed_failures.json").write_text(
        json.dumps({"Fixed Later.m4a": "2026-09-01T00:00:00"}))
    # the dismissed name later succeeded: its latest result is ok
    monkeypatch.setattr(status, "history", lambda: [
        {"name": "Fixed Later.m4a", "ok": True, "at": "2026-09-02T00:00:00"},
        {"name": "Fixed Later.m4a", "ok": False, "at": "2026-09-01T00:00:00"}])
    control.STOP_FLAG.write_text("x")
    return tmp_path


def _manifest():
    return json.loads(config.MANIFEST_PATH.read_text())["processed"]


def test_check_finds_every_kind_and_writes_nothing(lib):
    before = _tree(config.PROJECT_DIR)
    fs = doctor.check(registry=False)
    kinds = {f.kind for f in fs}
    assert {"manifest_stale", "manifest_flat", "manifest_unlinked", "stale_hold",
            "stale_dismissal", "stale_stop_flag", "name_mismatch",
            "missing_keys"} <= kinds
    mm = [f for f in fs if f.kind == "name_mismatch"]
    # a folder with no date at all, and the older-form folders the ISO move
    # (tools/iso_rename.py) has not reached yet, each with its own note
    assert [f.subject for f in mm if "older MMDDYYYY form" not in f.detail] == ["Odd Folder"]
    assert {f.subject for f in mm} == set(config.meeting_bases())
    assert _tree(config.PROJECT_DIR) == before
    assert not (lib / "migration").exists()


def test_the_manifest_plan_relinks_by_time_then_by_title_and_date(lib):
    ops = {a["key"]: a for a in doctor.manifest_plan()}
    assert ops["budget.m4a"]["op"] == "relink_mtime"
    assert ops["budget.m4a"]["meeting"] == "Budget Review 09292026"
    assert ops["Weekly Team Sync.m4a"]["op"] == "relink_title"
    assert ops["Weekly Team Sync.m4a"]["meeting"] == "Weekly Team Sync 09232026"
    assert ops["gone.m4a"]["op"] == "retire"
    assert ops["Flat Era.m4a"]["op"] == "flat"
    assert ops["Fresh Arrival.m4a"]["op"] == "link"
    assert "Kept Linked.m4a" not in ops


def test_the_fix_dry_run_writes_nothing(lib, capsys):
    before = _tree(config.PROJECT_DIR)
    doctor_cli.main(["fix"])
    out = capsys.readouterr().out
    assert "dry run" in out and "retire 1" in out
    assert _tree(config.PROJECT_DIR) == before


def test_fix_repairs_and_journals_then_undo_restores(lib):
    before = _tree(config.PROJECT_DIR)
    r = doctor.fix(dry_run=False, registry=False)
    assert r["run"] and not r.get("error")
    m = _manifest()
    assert m["budget.m4a"]["outputs"][0] == str(
        config.meeting_file("Budget Review 09292026", ".json"))
    assert m["budget.m4a"]["outputs"][1].endswith("Budget Review 09292026.txt")
    assert "gone.m4a" not in m
    assert m["Flat Era.m4a"]["outputs"][0] == str(
        config.meeting_file("Flat Era 05012026", ".json"))
    assert m["Fresh Arrival.m4a"]["mtime"] == _ts("2026-09-30T08:00:00")
    retired = json.loads((config.PROJECT_DIR / doctor.RETIRED_NAME).read_text())
    assert "gone.m4a" in retired
    assert json.loads(holds.PATH.read_text()) == []
    assert json.loads((config.PROJECT_DIR / "dismissed_failures.json").read_text()) == {}
    assert not control.STOP_FLAG.exists()
    jdir = lib / "migration"
    assert oct(jdir.stat().st_mode & 0o777) == "0o700"
    assert oct((jdir / f"{r['run']}.jsonl").stat().st_mode & 0o777) == "0o600"
    # after the fix, the store checks clean of everything fixable
    left = [f for f in doctor.check(registry=False) if f.fixable]
    assert left == []
    dry = doctor.undo(r["run"])
    assert dry["restored"] and _manifest() == m
    u = doctor.undo(r["run"], dry_run=False)
    assert not u["kept"]
    # every file is back but the stale stop flag, which undo never restores
    flag = str(control.STOP_FLAG.relative_to(config.PROJECT_DIR))
    assert u["skipped"] == [str(control.STOP_FLAG)]
    assert _tree(config.PROJECT_DIR) == {k: v for k, v in before.items() if k != flag}


def test_undo_says_what_it_cannot_undo_and_never_restores_a_stop_flag(
        sandbox, monkeypatch, tmp_path, capsys):
    """undo printed 'restored 3 file(s)' after a fix that also rotated a
    log, as if the whole run were back, and it put the stale stopping.flag
    back, so the next check reported stale_stop_flag again."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setattr(config, "LOG_DIR", sandbox / "logs")
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 1000)
    logs = config.LOG_DIR
    logs.mkdir(exist_ok=True)
    (logs / "big.log").write_bytes(b"x" * 12000)
    control.STOP_FLAG.write_text("x")
    holds.PATH.write_text(json.dumps(["Not Here.m4a"]))
    r = doctor.fix(dry_run=False, registry=False)
    assert any(d.startswith("rotated 1 log") for d in r["done"])
    assert not control.STOP_FLAG.exists()
    capsys.readouterr()
    assert doctor_cli.main(["undo", r["run"], "--apply"]) == 0
    out = capsys.readouterr().out
    assert "not restored: stopping.flag" in out
    assert "not undone: rotated 1 log(s)" in out and ".gz" in out
    assert not control.STOP_FLAG.exists()
    assert json.loads(holds.PATH.read_text()) == ["Not Here.m4a"]
    assert "stale_stop_flag" not in {f.kind for f in doctor.check(registry=False)}


def test_a_torn_last_journal_line_never_stops_the_doctor_undo(lib, capsys):
    """A kill inside one journal write, or a hand edit, leaves a line that
    is not JSON. `doctor undo` raised a JSONDecodeError traceback on it and
    restored nothing, though every whole entry before it was intact."""
    before = _tree(config.PROJECT_DIR)
    r = doctor.fix(dry_run=False, registry=False)
    with open(lib / "migration" / f"{r['run']}.jsonl", "a") as fh:
        fh.write('garbage\n{"file": "' + str(holds.PATH) + '", "sha_af')
    capsys.readouterr()
    assert doctor_cli.main(["undo", r["run"], "--apply"]) == 0
    assert "Traceback" not in capsys.readouterr().err
    flag = str(control.STOP_FLAG.relative_to(config.PROJECT_DIR))
    assert _tree(config.PROJECT_DIR) == {k: v for k, v in before.items() if k != flag}


def test_undo_keeps_a_file_changed_since(lib):
    r = doctor.fix(dry_run=False, registry=False)
    holds.hold("New Hold.m4a")
    u = doctor.undo(r["run"], dry_run=False)
    assert str(holds.PATH) in u["kept"]
    assert json.loads(holds.PATH.read_text()) == ["New Hold.m4a"]


def test_fix_refuses_while_a_batch_runs(lib):
    status.start_run(["x.m4a"])
    before = _tree(config.PROJECT_DIR)
    r = doctor.fix(dry_run=False, registry=False)
    assert r.get("error") and r["run"] is None
    assert control.STOP_FLAG.exists()
    assert _manifest() == json.loads(before["manifest.json"])["processed"]


def test_a_dead_batchs_running_flag_neither_hides_findings_nor_blocks_fix(lib):
    """A hard kill skips end_run, so status.json keeps running true with a
    dead pid. check skipped the stale stop flag and the scratch audio, and
    fix refused 'a batch is running', until the next batch ran."""
    import subprocess
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()                                  # reaped: its pid is free
    status.STATUS_PATH.write_text(json.dumps({"running": True, "pid": dead.pid}))
    config.WORK_DIR.mkdir(exist_ok=True)
    (config.WORK_DIR / "x.wav").write_bytes(b"scratch")
    kinds = {f.kind for f in doctor.check(registry=False)}
    assert {"stale_stop_flag", "work_scratch"} <= kinds
    r = doctor.fix(dry_run=False, registry=False)
    assert not r.get("error") and r["run"]
    assert not control.STOP_FLAG.exists()


def test_registry_repairs_copy_the_voiceprints_first_and_undo(lib, monkeypatch):
    vp = config.VOICEPRINTS_DIR
    (vp / "unknowns.json").write_text(json.dumps({"next": 1, "speakers": {}}))
    (vp / "registry.json").write_text("{}")
    before = _tree(vp)
    calls = []

    def fake_fix(findings, voiceprints_dir=None, meetings_dir=None):
        calls.append(len(findings))
        (vp / "unknowns.json").write_text(json.dumps({"next": 9, "speakers": {}}))
        (vp / "U009.npy").write_bytes(b"new")
        return ["healed"]
    from stt import registry_doctor
    monkeypatch.setattr(registry_doctor, "fix", fake_fix)
    f = [doctor.Finding("registry_stale_next", "unknowns.json", "x", True)]
    r = doctor.fix(f, dry_run=False)
    assert calls == [1] and "healed" in r["done"]
    assert (lib / "migration" / r["run"] / "voiceprints" / "unknowns.json").exists()
    doctor.undo(r["run"], dry_run=False)
    assert _tree(vp) == before


@pytest.mark.parametrize("raw", [b'{"processed": {"a.m4a": {', b"\xff\xfe\x00bad", b""])
def test_a_damaged_manifest_is_reported_and_never_planned_over(lib, raw):
    """A truncated manifest read as an empty one: check named nothing
    about it, and fix planned a fresh record for every meeting over it."""
    config.MANIFEST_PATH.write_bytes(raw)
    fs = doctor.check(registry=False)
    bad = [f for f in fs if f.kind == "manifest_unreadable"]
    assert len(bad) == 1 and bad[0].subject == "manifest.json" and not bad[0].fixable
    assert not {"manifest_stale", "manifest_flat", "manifest_unlinked"} & {f.kind for f in fs}
    assert doctor.manifest_plan() == []
    r = doctor.fix(dry_run=False, registry=False)
    assert r["plan"]["manifest"] == {}
    assert config.MANIFEST_PATH.read_bytes() == raw
    # no manifest at all is no finding: a new home has none yet
    config.MANIFEST_PATH.unlink()
    assert "manifest_unreadable" not in {f.kind for f in doctor.check(registry=False)}


@pytest.mark.parametrize("raw", ['{"processed": []}', '{"processed": null}', "[1]",
                                 '"x"', "null"])
def test_a_manifest_of_the_wrong_shape_is_reported_not_raised(lib, raw):
    """fj.read_json passes any valid JSON through, so a processed list or
    null, or a manifest that is no object, raised AttributeError and
    aborted the whole check."""
    config.MANIFEST_PATH.write_text(raw)
    fs = doctor.check(registry=False)
    assert "manifest_unreadable" in {f.kind for f in fs}
    assert doctor.fix(dry_run=False, registry=False)["plan"]["manifest"] == {}
    assert config.MANIFEST_PATH.read_text() == raw


@pytest.mark.parametrize("rec", ["x", None, [1]])
def test_a_manifest_record_of_the_wrong_shape_is_reported_not_raised(lib, rec):
    m = json.loads(config.MANIFEST_PATH.read_text())
    m["processed"]["Fresh Arrival.m4a"] = rec
    config.MANIFEST_PATH.write_text(json.dumps(m))
    fs = doctor.check(registry=False)
    assert [f.subject for f in fs if f.kind == "manifest_bad_record"] == ["Fresh Arrival.m4a"]
    ops = {a["key"]: a["op"] for a in doctor.manifest_plan()}
    assert ops["budget.m4a"] == "relink_mtime"        # the good records still plan
    assert "Fresh Arrival.m4a" not in ops             # never linked over
    doctor.fix(dry_run=False, registry=False)
    assert json.loads(config.MANIFEST_PATH.read_text())["processed"]["Fresh Arrival.m4a"] == rec


@pytest.mark.parametrize("mtime", [float("nan"), float("inf"), 1e20, "abc", [1]])
def test_a_record_time_that_is_no_timestamp_never_aborts_the_check(lib, mtime):
    """mtime NaN raised ValueError and 1e999 OverflowError in
    date.fromtimestamp; the record now just has no time."""
    m = json.loads(config.MANIFEST_PATH.read_text())
    m["processed"]["gone.m4a"]["mtime"] = mtime
    config.MANIFEST_PATH.write_text(json.dumps(m))
    ops = {a["key"]: a["op"] for a in doctor.manifest_plan()}
    assert ops["gone.m4a"] == "retire"
    assert "manifest_stale" in {f.kind for f in doctor.check(registry=False)}


@pytest.mark.parametrize("field, value, links", [
    ("duration_sec", "abc", True), ("duration_sec", [1], True),
    ("source_mtime", "abc", False), ("source_mtime", [1], False),
    ("source_mtime", float("nan"), False), ("source_name", ["a"], False)])
def test_a_meeting_field_of_the_wrong_shape_never_aborts_the_check(
        lib, monkeypatch, field, value, links):
    """duration_sec 'abc' raised ValueError in check --deep, and a text
    source_mtime raised in the relink by time and in the link."""
    from stt import provenance
    monkeypatch.setattr(provenance, "probe_tags", lambda a: {"duration": 600.0})
    base = "Fresh Arrival 09302026"
    mfile(base, ".m4a").write_bytes(b"audio")
    j = config.meeting_file(base, ".json")
    d = json.loads(j.read_text())
    d[field] = value
    j.write_text(json.dumps(d))
    kinds = {f.kind for f in doctor.check(registry=False, deep=True)}
    assert "manifest_stale" in kinds
    # a link needs a real source name and a real time
    assert ("Fresh Arrival.m4a" in {a["key"] for a in doctor.manifest_plan()}) is links


def test_a_meeting_json_that_is_no_object_is_reported_not_raised(lib):
    j = config.meeting_file("Odd Folder", ".json")
    j.write_text("[1]")
    fs = doctor.check(registry=False, deep=True)
    assert [f.subject for f in fs if f.kind == "unreadable_json"] == ["Odd Folder"]


def test_a_missing_store_retires_no_record_and_says_so(lib):
    """With the meetings folder unmounted or moved, every record looked
    stale, and fix moved them all to manifest_retired.json, so the next
    batch would transcribe every source again."""
    store = config.meetings_dir()
    before = config.MANIFEST_PATH.read_bytes()
    store.rename(store.with_name("meetings.away"))
    fs = doctor.check(registry=False)
    away = [f for f in fs if f.kind == "store_missing"]
    assert len(away) == 1 and "5 manifest record(s)" in away[0].detail
    assert not {"manifest_stale", "manifest_flat"} & {f.kind for f in fs}
    r = doctor.fix(dry_run=False, registry=False)
    assert r["plan"]["manifest"] == {}
    assert config.MANIFEST_PATH.read_bytes() == before
    assert not (config.PROJECT_DIR / doctor.RETIRED_NAME).exists()
    # an empty folder is the same: no meeting to check a record against
    store.mkdir()
    assert doctor.manifest_plan() == []
    assert "store_missing" in {f.kind for f in doctor.check(registry=False)}


def test_a_missing_source_folder_releases_no_hold_and_says_so(lib):
    """With the source folder unmounted or moved, every held name looked
    gone, and fix released the hold on a private file, so the next batch
    ran it."""
    src = config.source_dir()
    holds.PATH.write_text(json.dumps(["Private Memo.wav"]))
    (src / "Private Memo.wav").write_bytes(b"x")
    assert "stale_hold" not in {f.kind for f in doctor.check(registry=False)}
    src.rename(src.with_name("source.away"))
    fs = doctor.check(registry=False)
    assert "stale_hold" not in {f.kind for f in fs}
    away = [f for f in fs if f.kind == "source_missing"]
    assert len(away) == 1 and "1 queue hold(s)" in away[0].detail
    r = doctor.fix(dry_run=False, registry=False)
    assert not any("stale hold" in d for d in r["done"])
    assert json.loads(holds.PATH.read_text()) == ["Private Memo.wav"]


def test_a_denied_source_folder_counts_as_there_and_releases_no_hold(lib):
    """A source folder under a parent with no search right (or one macOS
    privacy guards) gives PermissionError on a stat. check and fix raised
    it from _source_away and from _stale_holds' exists()."""
    locked = config.PROJECT_DIR / "locked"
    src = locked / "src"
    src.mkdir(parents=True)
    (config.PROJECT_DIR / "stt.env").write_text(f"STT_ICLOUD_DIR={src}\n")
    holds.PATH.write_text(json.dumps(["Private Memo.wav"]))
    (src / "Private Memo.wav").write_bytes(b"x")
    os.chmod(locked, 0)
    try:
        if os.access(src, os.F_OK):
            pytest.skip("this user can stat a mode-000 folder's children")
        fs = doctor.check(registry=False)
        assert not {"stale_hold", "source_missing"} & {f.kind for f in fs}
        r = doctor.fix(dry_run=False, registry=False)
        assert not any("stale hold" in d for d in r["done"])
    finally:
        os.chmod(locked, 0o700)
    assert json.loads(holds.PATH.read_text()) == ["Private Memo.wav"]


def test_the_batch_end_report_holds_counts_and_no_names(lib):
    doctor.run_check_at_batch_end()
    rep = (config.PROJECT_DIR / doctor.REPORT_PATH_NAME).read_text()
    assert "Budget" not in rep and "Odd Folder" not in rep
    d = json.loads(rep)
    assert d["findings"]["manifest_stale"] == 3 and d["meetings"] == 6


# ---------- housekeeping ----------

def test_a_log_rotates_at_its_cap_and_keeps_three(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox / "logs")
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 100)
    logs = config.LOG_DIR
    logs.mkdir()
    log = logs / "stt.err.log"
    for i in range(5):
        log.write_bytes(f"gen{i}".encode() * 40)
        assert housekeeping.rotate_logs() == ["stt.err.log"]
        assert log.stat().st_size == 0               # truncated in place
    names = sorted(p.name for p in logs.iterdir())
    assert names == ["stt.err.log", "stt.err.log.1.gz", "stt.err.log.2.gz",
                     "stt.err.log.3.gz"]
    assert gzip.decompress((logs / "stt.err.log.1.gz").read_bytes()).startswith(b"gen4")
    assert gzip.decompress((logs / "stt.err.log.3.gz").read_bytes()).startswith(b"gen2")


def test_a_small_log_is_left_alone_and_a_dry_run_writes_nothing(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox / "logs")
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 100)
    config.LOG_DIR.mkdir()
    small = config.LOG_DIR / "gui.err.log"
    small.write_bytes(b"x" * 10)
    big = config.LOG_DIR / "stt.out.log"
    big.write_bytes(b"y" * 200)
    assert housekeeping.rotate_logs(dry_run=True) == ["stt.out.log"]
    assert big.stat().st_size == 200 and small.stat().st_size == 10
    assert sorted(p.name for p in config.LOG_DIR.iterdir()) == ["gui.err.log", "stt.out.log"]


def test_an_appending_writer_keeps_writing_to_the_same_file(sandbox, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", sandbox / "logs")
    """launchd holds the log open: rotation must not rename it away."""
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 10)
    config.LOG_DIR.mkdir()
    log = config.LOG_DIR / "stt.err.log"
    with open(log, "a") as fh:
        fh.write("before rotation\n")
        fh.flush()
        housekeeping.rotate_logs()
        fh.write("after\n")
        fh.flush()
    assert log.read_text() == "after\n"


def _seed_generations(logs, name="a.log"):
    for i in (1, 2, 3):
        (logs / f"{name}.{i}.gz").write_bytes(gzip.compress(f"old{i}\n".encode()))


def _generations(logs, name="a.log"):
    return [gzip.decompress((logs / f"{name}.{i}.gz").read_bytes()) for i in (1, 2, 3)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file")
def test_a_log_the_batch_cannot_write_keeps_every_generation(sandbox):
    """rotate() shifted the generations, then failed at the truncate. The log
    stayed over the cap and each run shifted again: after three runs every
    generation held the same copy and old1 to old3 were gone."""
    logs = sandbox / "logs"
    logs.mkdir()
    _seed_generations(logs)
    log = logs / "a.log"
    log.write_bytes(b"X" * 50)
    log.chmod(0o444)
    try:
        for _ in range(3):
            with pytest.raises(PermissionError):
                housekeeping.rotate(log, 10, 3)
        assert _generations(logs) == [b"old1\n", b"old2\n", b"old3\n"]
        assert sorted(p.name for p in logs.iterdir()) == [
            "a.log", "a.log.1.gz", "a.log.2.gz", "a.log.3.gz"]
    finally:
        log.chmod(0o644)
    assert housekeeping.rotate(log, 10, 3) is True       # writable again: it rotates
    assert _generations(logs) == [b"X" * 50, b"old1\n", b"old2\n"]


def test_a_failed_copy_moves_no_generation_and_leaves_no_temp(sandbox, monkeypatch):
    """A full disk fails the gzip copy: nothing shifts, the log keeps its lines."""
    import errno
    logs = sandbox / "logs"
    logs.mkdir()
    _seed_generations(logs)
    log = logs / "a.log"
    log.write_bytes(b"Y" * 50)

    def full(path, mode="rb", *a, **k):
        if "w" in mode:
            open(path, "wb").close()             # the temp exists, then the disk fills
            raise OSError(errno.ENOSPC, "No space left on device")
        return gzip.GzipFile(path, mode)
    monkeypatch.setattr(housekeeping.gzip, "open", full)
    with pytest.raises(OSError):
        housekeeping.rotate(log, 10, 3)
    assert _generations(logs) == [b"old1\n", b"old2\n", b"old3\n"]
    assert log.read_bytes() == b"Y" * 50
    assert not list(logs.glob("*.tmp"))


def test_one_log_that_fails_never_stops_the_others(sandbox, monkeypatch, capsys):
    """rotate_logs had no per-file try: the first failure ended the loop,
    rotate_all swallowed it, and every later log went unrotated with no
    sign."""
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 10)
    logs = sandbox / "logs"
    logs.mkdir()
    for n in ("a.log", "b.log", "c.log"):
        (logs / n).write_bytes(b"Z" * 50)
    real = housekeeping.rotate

    def stuck(path, *a, **k):
        if path.name == "a.log":
            raise PermissionError(13, "Permission denied")
        return real(path, *a, **k)
    monkeypatch.setattr(housekeeping, "rotate", stuck)
    assert housekeeping.rotate_logs(logs) == ["b.log", "c.log"]
    assert (logs / "b.log").stat().st_size == 0 and (logs / "c.log").stat().st_size == 0
    assert "could not rotate logs/a.log" in capsys.readouterr().err
    monkeypatch.setattr(config, "LOG_DIR", logs)
    (logs / "c.log").write_bytes(b"Z" * 50)
    assert housekeeping.rotate_all()["logs"] == ["c.log"]


def test_a_leftover_temp_gzip_is_not_a_log(sandbox, monkeypatch):
    """A process killed between the gzip and the replace left
    c.log.1.gz.tmp. It was listed as a log and rotated into
    c.log.1.gz.tmp.1.gz. The next rotation of c.log replaces it."""
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 10)
    logs = sandbox / "logs"
    logs.mkdir()
    (logs / "c.log.1.gz.tmp").write_bytes(b"Z" * 50)
    (logs / "c.log").write_bytes(b"C" * 50)
    assert [f.name for f in housekeeping.log_files(logs)] == ["c.log"]
    assert housekeeping.rotate_logs(logs) == ["c.log"]
    assert sorted(p.name for p in logs.iterdir()) == ["c.log", "c.log.1.gz"]
    assert gzip.decompress((logs / "c.log.1.gz").read_bytes()) == b"C" * 50


@pytest.mark.parametrize("name", ["STT_LOG_MAX_BYTES", "STT_CALIBRATION_MAX_BYTES"])
@pytest.mark.parametrize("raw", ["0", "-5"])
def test_a_cap_below_one_byte_keeps_the_default(tmp_path, name, raw):
    """A cap of 0 or less rotated every file on every run."""
    import subprocess
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), name: raw})
    r = subprocess.run([sys.executable, "-c", "from stt import housekeeping as h; "
                        "print(h.LOG_MAX_BYTES, h.CALIBRATION_MAX_BYTES)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "10000000 10000000"
    assert name in r.stderr and "using 10000000" in r.stderr


def test_an_empty_file_never_rotates_and_keeps_every_generation(sandbox):
    """At a cap of 0 an empty log rotated into an empty name.1.gz, and the
    real history moved one step closer to deletion."""
    logs = sandbox / "logs"
    logs.mkdir()
    _seed_generations(logs)
    log = logs / "a.log"
    log.write_bytes(b"")
    for _ in range(3):
        assert housekeeping.rotate(log, 0, 3) is False
    assert _generations(logs) == [b"old1\n", b"old2\n", b"old3\n"]
    log.write_bytes(b"X")
    assert housekeeping.rotate(log, 0, 3) is True       # a file with bytes still rotates
    assert _generations(logs) == [b"X", b"old1\n", b"old2\n"]


def test_two_rotations_of_one_file_at_once_do_not_collide(sandbox):
    """The batch and the doctor's fix both rotate. Two rotations at once
    shared name.1.gz.tmp: one os.replace took it and the other raised
    FileNotFoundError, which cost the rest of that pass."""
    import fcntl
    import threading
    logs = sandbox / "logs"
    logs.mkdir()
    log = logs / "x.log"
    log.write_bytes(b"DATA\n" * 100)
    with open(log, "rb") as held:                  # another rotation holds it
        fcntl.flock(held, fcntl.LOCK_EX)
        assert housekeeping.rotate(log, 10, 3) is False
    assert sorted(p.name for p in logs.iterdir()) == ["x.log"]
    assert log.stat().st_size == 500
    for _ in range(20):
        log.write_bytes(b"DATA\n" * 100)
        for g in logs.glob("*.gz"):
            g.unlink()
        start, results = threading.Barrier(2), []

        def go():
            start.wait()
            try:
                results.append(housekeeping.rotate(log, 10, 3))
            except Exception as e:
                results.append(e)
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == [False, True], results
        assert sorted(p.name for p in logs.iterdir()) == ["x.log", "x.log.1.gz"]
        assert gzip.decompress((logs / "x.log.1.gz").read_bytes()) == b"DATA\n" * 100


def test_calibration_is_capped_with_one_gzip(sandbox, monkeypatch):
    monkeypatch.setattr(housekeeping, "CALIBRATION_MAX_BYTES", 50)
    cal = config.CALIBRATION_LOG
    for i in range(3):
        cal.write_text(json.dumps({"n": i, "pad": "z" * 60}) + "\n")
        assert housekeeping.cap_calibration() is True
    gz = sorted(p.name for p in cal.parent.iterdir() if p.name.startswith("calibration"))
    assert gz == ["calibration.jsonl", "calibration.jsonl.1.gz"]
    assert b'"n": 2' in gzip.decompress((cal.parent / "calibration.jsonl.1.gz").read_bytes())
    assert cal.stat().st_size == 0
    cal.write_text("{}\n")
    assert housekeeping.cap_calibration() is False


# ---------- QA fixes: dismissals and same-day twins ----------

def test_a_dismissal_clears_only_when_the_latest_result_is_ok(sandbox, monkeypatch):
    (config.PROJECT_DIR / "dismissed_failures.json").write_text(json.dumps({
        "Fixed Later.m4a": "x", "Still Failing.m4a": "x", "Never Ran.m4a": "x",
        "Failed Again.m4a": "x"}))
    monkeypatch.setattr(status, "history", lambda: [      # newest first
        {"name": "Failed Again.m4a", "ok": False, "at": "2026-09-03T00:00:00"},
        {"name": "Fixed Later.m4a", "ok": True, "at": "2026-09-02T00:00:00"},
        {"name": "Failed Again.m4a", "ok": True, "at": "2026-09-02T00:00:00"},
        {"name": "Still Failing.m4a", "ok": False, "at": "2026-09-01T00:00:00"},
        {"name": "Fixed Later.m4a", "ok": False, "at": "2026-09-01T00:00:00"}])
    assert doctor._stale_dismissals() == ["Fixed Later.m4a"]


@pytest.fixture
def twins(sandbox, monkeypatch, tmp_path):
    """Two same-day meetings with the same title words, and two stale
    records that tie on both."""
    monkeypatch.setenv("STT_MIGRATION_DIR", str(tmp_path / "migration"))
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    _m("Team Sync 09232026", date="2026-09-23", arrived_at="2026-09-23T18:00:00")
    _m("Team Sync 09232026 (2)", date="2026-09-23", arrived_at="2026-09-23T11:00:00")
    old = config.PROJECT_DIR / "old"
    m = {"processed": {
        "Team Sync a.m4a": {"mtime": _ts("2026-09-23T17:00:00"),
                            "processed_at": "2026-09-23T18:00:05",
                            "outputs": [str(old / "Team Sync A" / "Team Sync A.json")]},
        "Team Sync b.m4a": {"mtime": _ts("2026-09-23T10:00:00"),
                            "processed_at": "2026-09-23T11:00:05",
                            "outputs": [str(old / "Team Sync B" / "Team Sync B.json")]},
    }}
    config.MANIFEST_PATH.write_text(json.dumps(m, indent=2))
    return tmp_path


def test_same_day_twins_pair_one_to_one_by_time(twins):
    ops = {a["key"]: a for a in doctor.manifest_plan()}
    assert ops["Team Sync b.m4a"]["op"] == "relink_tie"
    assert ops["Team Sync b.m4a"]["meeting"] == "Team Sync 09232026 (2)"
    assert ops["Team Sync a.m4a"]["meeting"] == "Team Sync 09232026"
    assert ops["Team Sync a.m4a"]["outputs"][0] == str(
        config.meeting_file("Team Sync 09232026", ".json"))


def test_a_tie_pairing_is_journaled_and_undo_still_restores(twins):
    before = _tree(config.PROJECT_DIR)
    r = doctor.fix(dry_run=False, registry=False)
    m = _manifest()
    assert m["Team Sync a.m4a"]["relinked"] == "relink_tie"
    lines = [json.loads(x) for x in
             (twins / "migration" / f"{r['run']}.jsonl").read_text().splitlines()]
    assert sorted(e["tie"] for e in lines if "tie" in e) == ["Team Sync a.m4a",
                                                             "Team Sync b.m4a"]
    doctor.undo(r["run"], dry_run=False)
    assert _tree(config.PROJECT_DIR) == before


def test_a_tie_with_more_records_than_meetings_retires(twins):
    m = json.loads(config.MANIFEST_PATH.read_text())
    m["processed"]["Team Sync c.m4a"] = {
        "mtime": _ts("2026-09-23T12:00:00"),
        "outputs": [str(config.PROJECT_DIR / "old" / "Team Sync C" / "Team Sync C.json")]}
    config.MANIFEST_PATH.write_text(json.dumps(m))
    ops = {a["key"]: a["op"] for a in doctor.manifest_plan()}
    assert set(ops.values()) == {"retire"}


@pytest.mark.parametrize("tool, argv", [
    ("backfill_keys", ["--undo", "{run}", "--apply"]),
    ("fix_dates", ["--undo", "{run}", "--apply"]),
    ("amnesty", ["--undo", "{run}", "--apply"]),
    ("doctor", ["undo", "{run}", "--apply"]),
])
@pytest.mark.parametrize("run", ["nonexist", "../../x", "/etc/passwd", ".hidden", ""])
def test_an_unknown_undo_run_is_one_line_and_exit_1(sandbox, tmp_path, monkeypatch,
                                                    capsys, tool, argv, run):
    """A mistyped run id printed a full traceback ending in FileNotFoundError,
    and '../../x' was joined into the journal path unchecked."""
    import importlib
    jdir = tmp_path / "journals"
    jdir.mkdir()
    monkeypatch.setenv("STT_MIGRATION_DIR", str(jdir))
    mod = importlib.import_module(tool)
    args = [a.format(run=run) for a in argv] + ["--journal-dir", str(jdir)]
    assert mod.main(args) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("no such run: ") and "\n" not in err
    assert "Traceback" not in err


def test_a_run_id_with_a_slash_never_becomes_a_path(tmp_path):
    from stt import fieldjournal
    for bad in ("../../x", "a/b", "/abs", ".x", "", None, "x" * 200):
        with pytest.raises(fieldjournal.NoSuchRun):
            fieldjournal.journal_path(bad, tmp_path)
    assert fieldjournal.journal_path("backfill-20261109T101500", tmp_path) == \
        tmp_path / "backfill-20261109T101500.jsonl"


def test_check_reports_a_folder_with_no_json_or_an_unreadable_one(sandbox, monkeypatch):
    """A folder with no meeting json (an interrupted run, a hand copy) or
    with a json that is not JSON reached no reader: the panel listed
    nothing, Archive and Delete refused it as no meeting, and the check
    said 'clean: nothing to report'. Each one is reported now, and how to
    clear it. A batch that runs makes its folder before its json, so the
    folder check waits for it."""
    _m("2026-08-04 Fine", title="Fine")
    md = config.meetings_dir()
    (md / "2026-08-02 Empty Folder").mkdir()
    (md / "2026-08-03 No Json").mkdir()
    (md / "2026-08-03 No Json" / "2026-08-03 No Json.txt").write_text("stub")
    (md / "2026-08-05 Audio Only").mkdir()
    (md / "2026-08-05 Audio Only" / "2026-08-05 Audio Only.m4a").write_bytes(b"a")
    (md / "2026-08-01 Corrupt One").mkdir()
    (md / "2026-08-01 Corrupt One" / "2026-08-01 Corrupt One.json").write_text("{bad json")
    (md / ".archive").mkdir(exist_ok=True)
    found = {(f.kind, f.subject): f for f in doctor.check(registry=False)}
    bad = found[("unreadable_json", "2026-08-01 Corrupt One")]
    assert not bad.fixable and "not readable JSON" in bad.detail
    for name in ("2026-08-02 Empty Folder", "2026-08-03 No Json"):
        assert "next batch start removes it" in found[("folder_without_json", name)].detail
    assert "holds audio" in found[("folder_without_json", "2026-08-05 Audio Only")].detail
    assert not any(s.startswith(".") or s == "2026-08-04 Fine" for k, s in found
                   if k in ("unreadable_json", "folder_without_json"))
    monkeypatch.setattr(doctor, "_batch_running", lambda: True)
    kinds = {f.kind for f in doctor.check(registry=False)}
    assert "folder_without_json" not in kinds and "unreadable_json" in kinds


# ---------- the speaker engine ----------

def _no_diar_env(monkeypatch):
    for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "STT_DIARIZER", "STT_SPEAKRS_AHC",
              "STT_SPEAKRS_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)


def test_the_doctor_says_when_there_is_no_hugging_face_token(sandbox, monkeypatch):
    """Both engines need the gated pyannote embedder, and the batch stops at
    its start without a token. The doctor said nothing about it."""
    from stt import speakrs_diar
    _no_diar_env(monkeypatch)
    monkeypatch.setattr(config, "HF_TOKEN", None)
    monkeypatch.setattr(config, "resolve_hf_token", lambda: None)
    monkeypatch.setattr(speakrs_diar, "available", lambda: (True, ""))
    kinds = {f.kind: f for f in doctor.check(registry=False)}
    assert "diarizer_no_token" in kinds
    assert "HF_TOKEN" in kinds["diarizer_no_token"].detail
    (sandbox / "stt.env").write_text("HF_TOKEN=hf_synthetic\n")
    assert "diarizer_no_token" not in {f.kind for f in doctor.check(registry=False)}


def test_the_doctor_says_when_speakrs_falls_back_to_pyannote(sandbox, monkeypatch):
    """A missing helper or model set showed only as a warning line per file
    in the log. The doctor names it once, and says nothing when pyannote
    is the engine the owner picked."""
    from stt import speakrs_diar
    _no_diar_env(monkeypatch)
    monkeypatch.setattr(config, "resolve_hf_token", lambda: "hf_synthetic")
    monkeypatch.setattr(speakrs_diar, "available",
                        lambda: (False, "speakrs helper not built (x); run init.sh"))
    found = [f for f in doctor.check(registry=False) if f.kind == "diarizer_fallback"]
    assert len(found) == 1 and "run init.sh" in found[0].detail
    assert "slower pyannote path" in found[0].detail
    (sandbox / "stt.env").write_text("STT_DIARIZER=pyannote\n")
    assert "diarizer_fallback" not in {f.kind for f in doctor.check(registry=False)}
    (sandbox / "stt.env").write_text("")
    monkeypatch.setattr(speakrs_diar, "available", lambda: (True, ""))
    monkeypatch.setattr(speakrs_diar, "SANDBOX_EXEC", str(sandbox / "no-sandbox-exec"))
    found = [f for f in doctor.check(registry=False) if f.kind == "diarizer_fallback"]
    assert len(found) == 1 and "no-sandbox-exec missing" in found[0].detail


def test_the_doctor_names_an_unreadable_diarizer_setting(sandbox, monkeypatch):
    """STT_DIARIZER, STT_SPEAKRS_AHC and STT_SPEAKRS_TIMEOUT_SEC warned at
    import and fell back, but the doctor's bad_setting list left them out."""
    _no_diar_env(monkeypatch)
    (sandbox / "stt.env").write_text(
        "STT_DIARIZER=speakr\nSTT_SPEAKRS_AHC=abc\nSTT_SPEAKRS_TIMEOUT_SEC=0\n")
    bad = {f.subject: f.detail for f in doctor.check(registry=False)
           if f.kind == "bad_setting"}
    assert set(bad) == {"STT_DIARIZER", "STT_SPEAKRS_AHC", "STT_SPEAKRS_TIMEOUT_SEC"}
    assert "using speakrs" in bad["STT_DIARIZER"]
    assert "using 0.55" in bad["STT_SPEAKRS_AHC"]
    assert "using 3600" in bad["STT_SPEAKRS_TIMEOUT_SEC"]
    (sandbox / "stt.env").write_text(
        "STT_DIARIZER=Pyannote\nSTT_SPEAKRS_AHC=0.6\nSTT_SPEAKRS_TIMEOUT_SEC=900\n")
    assert not [f for f in doctor.check(registry=False) if f.kind == "bad_setting"]


def test_the_doctor_names_a_shell_style_line_that_sets_nothing(sandbox, monkeypatch):
    """stt.env is not shell. 'export STT_DIARIZER=pyannote' read as the key
    'export STT_DIARIZER', run.sh and setup.sh skipped the line, and the
    setting stayed at its default with no word anywhere."""
    _no_diar_env(monkeypatch)
    (sandbox / "stt.env").write_text(
        "export STT_DIARIZER=pyannote\nMY KEY=1\nSTT_PUNCTUATE=1\n_OK_2=x\n")
    bad = {f.subject: f.detail for f in doctor.check(registry=False)
           if f.kind == "bad_setting"}
    assert set(bad) == {"export STT_DIARIZER", "MY KEY"}
    assert "no 'export'" in bad["export STT_DIARIZER"]


def test_stt_env_example_lists_every_diarizer_setting():
    text = (Path(__file__).resolve().parent.parent / "stt.env.example").read_text()
    for name in ("STT_DIARIZER", "STT_SPEAKRS_AHC", "STT_SPEAKRS_TIMEOUT_SEC",
                 "STT_SPEAKRS_BIN", "STT_SPEAKRS_MODELS", "HF_TOKEN"):
        assert f"{name}=" in text, name
    assert "BOTH engines" in text


def test_the_doctor_says_why_verify_does_nothing(sandbox, monkeypatch):
    """Whisper was the second opinion for a Parakeet transcript. With it
    gone, STT_VERIFY=1 on Parakeet adds no flags, and only a log line said
    so. The doctor names it. A cloud engine still has Parakeet to compare
    with, and the default (off) needs no word."""
    monkeypatch.delenv("STT_VERIFY", raising=False)
    monkeypatch.delenv("STT_ASR_BACKEND", raising=False)

    def kinds():
        return {f.kind: f for f in doctor.check(registry=False)}
    assert "verify_no_second_engine" not in kinds()
    (sandbox / "stt.env").write_text("STT_VERIFY=1\n")
    f = kinds()["verify_no_second_engine"]
    assert f.subject == "STT_VERIFY" and not f.fixable
    assert "does nothing" in f.detail and "parakeet" in f.detail
    (sandbox / "stt.env").write_text("STT_VERIFY=1\nSTT_ASR_BACKEND=parakeet\n")
    assert "verify_no_second_engine" in kinds()
    (sandbox / "stt.env").write_text("STT_VERIFY=1\nSTT_ASR_BACKEND=cloud:scribe\n")
    assert "verify_no_second_engine" not in kinds()
    (sandbox / "stt.env").write_text("STT_VERIFY=0\n")
    assert "verify_no_second_engine" not in kinds()
    (sandbox / "stt.env").write_text("")
    monkeypatch.setenv("STT_VERIFY", "1")                # the environment counts too
    assert "verify_no_second_engine" in kinds()

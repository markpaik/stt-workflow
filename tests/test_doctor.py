"""stt/doctor.py and stt/housekeeping.py on a synthetic store: check never
writes, fix copies and journals before it writes, undo restores only what
still holds the fix's output, and the logs and the calibration log stay
under their caps."""
import gzip
import hashlib
import json
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
        json.dumps({"Never Ran.m4a": "2026-09-01T00:00:00"}))
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
    assert [f.subject for f in fs if f.kind == "name_mismatch"] == ["Odd Folder"]
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
    assert _tree(config.PROJECT_DIR) == before


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

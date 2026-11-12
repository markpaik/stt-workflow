"""stt/mirror.py: the transcript mirror (STT_MIRROR_DIR) against a temp
mirror folder. Every name here is synthetic.

The rules these pin: the setting is off by default and checked like the
other folder settings; the folder holds one byte copy of each live
meeting's .txt and nothing else; a write happens only when the content
differs, through a temp file and a rename; a rename, a delete, an archive
and a strict meeting each take the old file away, and a restore brings it
back; a file the mirror did not write, or one that changed after, is
never touched; a missing or unwritable folder is one problem line and a
retry, never an exception."""
import json
import os
import stat
import time

import pytest

from stt import config, mirror
from conftest import mfile

WEEKLY = "2026-08-03 Weekly Check-in"
BUDGET = "2026-08-04 Budget Review"


@pytest.fixture
def drive(sandbox, tmp_path_factory):
    """A mirror folder outside the data home, named in the sandbox stt.env."""
    d = tmp_path_factory.mktemp("drive")
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    return d


_ids = iter(range(1, 10_000))


def _txt(base, line="Alex Rivera: the numbers look right.", strict=False):
    head = f"{base}  |  1.0 min  |  1 speaker(s)"
    if strict:
        head += "  |  STRICT mode (no smoothing/open-set reassignment)"
    return f"{head}\n\n[00:01] {line}\n"


def _meeting(base, line="Alex Rivera: the numbers look right.", strict=False,
             mid=None, txt=None):
    mid = mid or f"{next(_ids):016x}"
    d = {"meeting_id": mid, "source_file": f"{base}.m4a", "duration_sec": 60.0,
         "strict": strict, "date": base[:10], "title": base[11:],
         "speakers": [], "segments": [], "words": []}
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text(txt if txt is not None else _txt(base, line))
    mfile(base, ".m4a").write_bytes(b"audio")
    mfile(base, ".diar.npz").write_bytes(b"cache")
    return mid


def _names(d):
    return sorted(os.listdir(d))


def _set_json(base, **kv):
    p = config.meeting_file(base, ".json")
    d = json.loads(p.read_text())
    d.update(kv)
    p.write_text(json.dumps(d))


# ---------- the setting ----------

def test_the_mirror_is_off_by_default(sandbox):
    _meeting(WEEKLY)
    assert mirror.mirror_dir() is None and mirror.context() is None
    r = mirror.reconcile()
    assert r["off"] and r["ok"] and not r["ops"]
    mirror.touch(WEEKLY)
    assert mirror._worker["t"] is None       # nothing started
    assert not (sandbox / mirror.STATE_NAME).exists()


def test_a_relative_path_is_a_bad_setting_and_stays_off(sandbox):
    (sandbox / "stt.env").write_text("STT_MIRROR_DIR=Meeting Transcripts\n")
    assert "not an absolute folder path" in mirror.setting_problem()
    assert mirror.mirror_dir() is None
    assert [n for n, _ in config.setting_problems()] == ["STT_MIRROR_DIR"]


@pytest.mark.parametrize("where", ["meetings", "meetings/sub", "voiceprints", "."])
def test_a_folder_inside_the_library_or_the_data_home_is_refused(sandbox, where):
    """Inside the library the copies would read as flat meetings, and inside
    the data home (the checkout, by default) they would sit in a public
    repository's working tree."""
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={sandbox / where}\n")
    why = mirror.setting_problem()
    assert why and "the mirror stays off" in why
    assert mirror.mirror_dir() is None
    assert "STT_MIRROR_DIR" in [n for n, _ in config.setting_problems()]


def test_the_checkout_itself_is_refused(sandbox):
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={config.CODE_DIR / 'qa'}\n")
    assert "this checkout" in mirror.setting_problem()


def test_a_good_folder_is_read_fresh_from_stt_env(drive):
    assert mirror.setting_problem() is None
    assert mirror.mirror_dir() == drive
    assert "STT_MIRROR_DIR" not in [n for n, _ in config.setting_problems()]


def test_the_environment_is_the_fallback(sandbox, tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("envdrive")
    monkeypatch.setenv("STT_MIRROR_DIR", str(d))
    assert mirror.mirror_dir() == d


# ---------- write, and unchanged content ----------

def test_a_full_pass_writes_one_byte_copy_per_meeting_and_nothing_else(drive, sandbox):
    _meeting(WEEKLY)
    _meeting(BUDGET, line="Jordan Lee: we hold the line on travel.")
    r = mirror.reconcile()
    assert r["ok"] and r["write"] == 2
    assert _names(drive) == [f"{WEEKLY}.txt", f"{BUDGET}.txt"]
    for b in (WEEKLY, BUDGET):
        assert ((drive / f"{b}.txt").read_bytes()
                == config.meeting_file(b, ".txt").read_bytes())
    # the record lives in the data home, owner-only
    st = sandbox / mirror.STATE_NAME
    assert stat.S_IMODE(st.stat().st_mode) == 0o600
    rec = json.loads(st.read_text())["mirrors"][str(drive)]
    assert set(rec) == {f"{WEEKLY}.txt", f"{BUDGET}.txt"}


def test_unchanged_content_is_never_written_again(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    p = drive / f"{WEEKLY}.txt"
    before = p.stat()
    time.sleep(0.01)
    # the .txt rewritten with the same bytes (a relabel that changed nothing)
    src = config.meeting_file(WEEKLY, ".txt")
    src.write_bytes(src.read_bytes())
    r = mirror.reconcile()
    assert r["write"] == r["update"] == r["remove"] == 0
    assert [o["op"] for o in r["ops"]] == ["keep"]
    after = p.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_new_content_replaces_the_file_through_a_temp_file(drive, monkeypatch):
    _meeting(WEEKLY)
    mirror.reconcile()
    config.meeting_file(WEEKLY, ".txt").write_text(_txt(WEEKLY, "Alex Rivera: revised."))
    seen = []
    real = os.replace

    def spy(a, b):
        seen.append((os.path.basename(a), os.path.basename(b), os.path.dirname(a)))
        return real(a, b)
    monkeypatch.setattr(mirror.os, "replace", spy)
    r = mirror.reconcile()
    assert r["update"] == 1
    assert "Alex Rivera: revised." in (drive / f"{WEEKLY}.txt").read_text()
    tmp, dst, where = next(s for s in seen if s[1] == f"{WEEKLY}.txt")
    assert tmp.startswith(mirror.TMP_PREFIX) and where == str(drive)
    assert _names(drive) == [f"{WEEKLY}.txt"]      # no temp file left


def test_a_dry_run_writes_nothing(drive, sandbox):
    _meeting(WEEKLY)
    r = mirror.reconcile(dry_run=True)
    assert r["dry_run"] and r["write"] == 1
    assert _names(drive) == []
    assert not (sandbox / mirror.STATE_NAME).exists()


# ---------- rename, delete, archive, restore ----------

def _rename(old, new):
    """The folder and its files renamed, as summarize._move_folder does."""
    od = config.meeting_dir(old)
    for f in od.iterdir():
        f.rename(od / (new + f.name[len(old):]))
    od.rename(config.meeting_dir(new))


def test_a_renamed_meeting_replaces_its_old_file(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    new = "2026-08-03 Weekly Check-in with Finance"
    _rename(WEEKLY, new)
    # a pass over the new name alone finds the old file by meeting_id
    r = mirror.reconcile([new])
    assert r["write"] == 1 and r["remove"] == 1
    assert _names(drive) == [f"{new}.txt"]


def test_a_rename_that_changes_only_the_letter_case(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    new = "2026-08-03 weekly check-in"
    tmp = "2026-08-03 tmp-case"
    _rename(WEEKLY, tmp)
    _rename(tmp, new)
    r = mirror.reconcile([WEEKLY, new])
    assert r["ok"] and r["remove"] == 0
    assert _names(drive) == [f"{new}.txt"]
    rec = mirror._load(mirror.context())["files"]
    assert set(rec) == {f"{new}.txt"}


def test_a_name_handed_back_in_the_other_unicode_form_is_still_its_own(drive):
    """APFS keeps a name in the form it was given, and a sync program may
    give it back decomposed: the copy is the same file, so it is neither
    written again nor renamed."""
    import unicodedata
    base = unicodedata.normalize("NFC", "2026-08-07 Café Planning")
    _meeting(base)
    mirror.reconcile()
    nfd = unicodedata.normalize("NFD", f"{base}.txt")
    os.rename(drive / f"{base}.txt", drive / nfd)
    assert os.listdir(drive) == [nfd]
    before = (drive / nfd).stat().st_mtime_ns
    r = mirror.reconcile()
    assert [o["op"] for o in r["ops"]] == ["keep"]
    assert os.listdir(drive) == [nfd] and (drive / nfd).stat().st_mtime_ns == before
    config.meeting_file(base, ".txt").write_text(_txt(base, "Alex Rivera: v2."))
    assert mirror.reconcile()["update"] == 1
    assert len(os.listdir(drive)) == 1 and "v2." in (drive / nfd).read_text()


def test_a_deleted_meeting_loses_its_file(drive):
    import shutil
    _meeting(WEEKLY)
    _meeting(BUDGET)
    mirror.reconcile()
    shutil.rmtree(config.meeting_dir(WEEKLY))
    r = mirror.reconcile([WEEKLY])
    assert r["remove"] == 1
    assert _names(drive) == [f"{BUDGET}.txt"]


def test_a_copy_removed_by_hand_while_the_pass_runs_is_no_error(drive, monkeypatch):
    import shutil
    _meeting(WEEKLY)
    mirror.reconcile()
    shutil.rmtree(config.meeting_dir(WEEKLY))
    real = mirror._plan

    def plan_then_gone(ctx, st, bases=None):
        ops = real(ctx, st, bases)
        (drive / f"{WEEKLY}.txt").unlink()
        return ops
    monkeypatch.setattr(mirror, "_plan", plan_then_gone)
    r = mirror.reconcile()
    assert r["ok"] and r["problem"] is None and r["remove"] == 0
    assert mirror._load(mirror.context())["files"] == {}


def test_an_archived_meeting_loses_its_file_and_a_restore_brings_it_back(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    arc = config.archive_dir()
    arc.mkdir()
    config.meeting_dir(WEEKLY).rename(arc / WEEKLY)
    assert mirror.reconcile()["remove"] == 1
    assert _names(drive) == []
    (arc / WEEKLY).rename(config.meeting_dir(WEEKLY))
    assert mirror.reconcile()["write"] == 1
    assert _names(drive) == [f"{WEEKLY}.txt"]


# ---------- strict ----------

def test_a_strict_meeting_is_never_mirrored(drive):
    _meeting(WEEKLY, strict=True)
    r = mirror.reconcile()
    assert r["write"] == 0 and _names(drive) == []


def test_a_meeting_that_becomes_strict_loses_its_file(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    _set_json(WEEKLY, strict=True)
    assert mirror.reconcile([WEEKLY])["remove"] == 1
    assert _names(drive) == []


def test_a_strict_header_line_is_enough(drive):
    """The pipeline writes the .txt before the json: a strict header in the
    new .txt keeps it home before the json says so."""
    _meeting(WEEKLY, txt=_txt(WEEKLY, strict=True))
    assert mirror.reconcile()["write"] == 0 and _names(drive) == []


def test_an_unreadable_json_counts_as_strict(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    config.meeting_file(WEEKLY, ".json").write_text("{not json")
    assert mirror.reconcile()["remove"] == 1 and _names(drive) == []


def test_a_meeting_that_turns_strict_between_plan_and_write_is_not_written(drive, monkeypatch):
    _meeting(WEEKLY)
    real = mirror._plan

    def plan_then_strict(ctx, st, bases=None):
        ops = real(ctx, st, bases)
        _set_json(WEEKLY, strict=True)
        return ops
    monkeypatch.setattr(mirror, "_plan", plan_then_strict)
    r = mirror.reconcile()
    assert r["write"] == 0 and _names(drive) == []
    assert mirror._load(mirror.context())["files"] == {}


# ---------- files the mirror did not write ----------

def test_a_file_put_there_by_hand_stays(drive):
    (drive / "Notes from the retreat.txt").write_text("my own notes")
    _meeting(WEEKLY)
    mirror.reconcile()
    mirror.reconcile()
    assert _names(drive) == [f"{WEEKLY}.txt", "Notes from the retreat.txt"]
    assert (drive / "Notes from the retreat.txt").read_text() == "my own notes"


def test_a_foreign_file_with_a_meetings_name_blocks_it_and_is_never_overwritten(drive):
    (drive / f"{WEEKLY}.txt").write_text("typed by hand")
    _meeting(WEEKLY)
    r = mirror.reconcile()
    assert [o["op"] for o in r["ops"]] == ["blocked"] and r["write"] == 0
    assert (drive / f"{WEEKLY}.txt").read_text() == "typed by hand"
    # and a delete of the meeting leaves it too
    import shutil
    shutil.rmtree(config.meeting_dir(WEEKLY))
    mirror.reconcile()
    assert (drive / f"{WEEKLY}.txt").read_text() == "typed by hand"


def test_a_file_that_changed_after_the_mirror_wrote_it_is_left_alone(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    (drive / f"{WEEKLY}.txt").write_text("edited in Drive")
    config.meeting_file(WEEKLY, ".txt").write_text(_txt(WEEKLY, "Alex Rivera: v2."))
    r = mirror.reconcile()
    assert [o["op"] for o in r["ops"]] == ["changed"] and r["update"] == 0
    assert (drive / f"{WEEKLY}.txt").read_text() == "edited in Drive"
    # gone from the library: still not removed
    _set_json(WEEKLY, strict=True)
    assert mirror.reconcile()["remove"] == 0
    assert (drive / f"{WEEKLY}.txt").read_text() == "edited in Drive"


def test_a_deleted_copy_is_written_again(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    (drive / f"{WEEKLY}.txt").unlink()
    assert mirror.reconcile()["write"] == 1
    assert (drive / f"{WEEKLY}.txt").exists()


def test_the_same_bytes_with_a_new_time_are_still_its_own(drive):
    """The sync program may set a file's time again: the hash decides."""
    _meeting(WEEKLY)
    mirror.reconcile()
    p = drive / f"{WEEKLY}.txt"
    os.utime(p, (1_000_000_000, 1_000_000_000))
    config.meeting_file(WEEKLY, ".txt").write_text(_txt(WEEKLY, "Alex Rivera: v2."))
    assert mirror.reconcile()["update"] == 1
    assert "v2." in p.read_text()


def test_a_write_that_crashed_before_the_record_is_still_its_own(drive, monkeypatch):
    _meeting(WEEKLY)
    calls = {"n": 0}
    real = mirror._save

    def save_once(ctx, st):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(ctx, st)        # the pending mark
        raise SystemExit("killed")      # the process died before the final save
    monkeypatch.setattr(mirror, "_save", save_once)
    with pytest.raises(SystemExit):
        mirror.reconcile()
    assert _names(drive) == [f"{WEEKLY}.txt"]
    monkeypatch.setattr(mirror, "_save", real)
    config.meeting_file(WEEKLY, ".txt").write_text(_txt(WEEKLY, "Alex Rivera: v2."))
    r = mirror.reconcile()
    assert r["update"] == 1 and "v2." in (drive / f"{WEEKLY}.txt").read_text()


def test_a_stale_temp_file_of_its_own_is_swept(drive):
    (drive / f"{mirror.TMP_PREFIX}abc.tmp").write_text("half")
    (drive / ".hidden by hand.tmp").write_text("mine")
    mirror.reconcile()
    assert _names(drive) == [".hidden by hand.tmp"]


# ---------- a folder that is not there ----------

def test_a_missing_folder_is_one_problem_and_a_retry(sandbox, tmp_path_factory):
    d = tmp_path_factory.mktemp("gone") / "Meeting Transcripts"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    _meeting(WEEKLY)
    r = mirror.reconcile()
    assert not r["ok"] and "missing" in r["problem"]
    assert not d.exists()                    # never created in its place
    assert "the next run retries" in mirror.summary_line(r)
    d.mkdir()
    assert mirror.reconcile()["write"] == 1


def test_a_folder_that_is_not_writable_is_a_problem(drive):
    _meeting(WEEKLY)
    os.chmod(drive, 0o500)
    try:
        r = mirror.reconcile()
        assert not r["ok"] and "not writable" in r["problem"]
    finally:
        os.chmod(drive, 0o700)


def test_a_write_that_fails_ends_the_pass_with_a_problem(drive, monkeypatch):
    import errno
    _meeting(WEEKLY)

    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(mirror, "_write", full)
    r = mirror.reconcile()
    assert not r["ok"] and "No space left on device" in r["problem"]


def test_a_held_lock_is_a_problem_not_a_wait_forever(drive, monkeypatch):
    import fcntl
    _meeting(WEEKLY)
    monkeypatch.setattr(mirror, "LOCK_WAIT_SEC", 0.2)
    with open(mirror.context().home / mirror.LOCK_NAME, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = mirror.reconcile()
    assert not r["ok"] and "lock" in r["problem"]


# ---------- the background trigger ----------

def test_touch_mirrors_in_the_background(drive):
    _meeting(WEEKLY)
    mirror.touch(WEEKLY)
    assert mirror.wait(10)
    assert _names(drive) == [f"{WEEKLY}.txt"]


def test_touch_keeps_the_folder_it_was_asked_for(drive, sandbox, tmp_path_factory):
    """Work that runs later follows the setting as it was when asked."""
    _meeting(WEEKLY)
    with mirror._cv:                       # hold the worker back
        mirror.touch(WEEKLY)
        other = tmp_path_factory.mktemp("other")
        (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={other}\n")
    assert mirror.wait(10)
    assert _names(drive) == [f"{WEEKLY}.txt"] and _names(other) == []


def test_touch_never_raises_and_says_a_problem_once(drive, monkeypatch, capsys):
    _meeting(WEEKLY)

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(mirror, "reconcile", boom)
    monkeypatch.setattr(mirror, "_said", {"msg": None})
    for _ in range(3):
        mirror.touch(WEEKLY)
        assert mirror.wait(10)
    err = capsys.readouterr().err
    assert err.count("mirror: not updated now (RuntimeError)") == 1


def test_a_missing_folder_in_the_background_is_one_line(sandbox, tmp_path_factory,
                                                        monkeypatch, capsys):
    d = tmp_path_factory.mktemp("away") / "Drive"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    monkeypatch.setattr(mirror, "_said", {"msg": None})
    _meeting(WEEKLY)
    for _ in range(3):
        mirror.touch(WEEKLY)
        assert mirror.wait(10)
    assert capsys.readouterr().err.count("is missing or not a folder") == 1


def test_the_summary_line(drive):
    assert mirror.summary_line({"off": True}) is None
    _meeting(WEEKLY)
    r = mirror.reconcile()
    assert mirror.summary_line(r) == "mirror: 1 written, 0 updated, 0 removed"
    assert mirror.summary_line(mirror.reconcile()) is None


# ---------- the doctor ----------

def _mirror_findings():
    from stt import doctor
    return [f for f in doctor.check(registry=False)
            if f.kind.startswith("mirror_") or f.subject == "STT_MIRROR_DIR"]


def test_the_doctor_says_nothing_while_the_mirror_is_off(sandbox):
    _meeting(WEEKLY)
    assert _mirror_findings() == []


def test_the_doctor_reports_a_set_folder_that_is_missing(sandbox, tmp_path_factory):
    d = tmp_path_factory.mktemp("away") / "Meeting Transcripts"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    (f,) = _mirror_findings()
    assert (f.kind, f.subject, f.fixable) == ("mirror_unavailable", "STT_MIRROR_DIR", False)
    assert "is missing or not a folder" in f.detail


def test_the_doctor_reports_a_folder_that_is_not_writable(drive):
    os.chmod(drive, 0o500)
    try:
        (f,) = _mirror_findings()
    finally:
        os.chmod(drive, 0o700)
    assert f.kind == "mirror_unavailable" and "not writable" in f.detail


def test_the_doctor_reports_a_bad_value_as_a_bad_setting(sandbox):
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={sandbox / 'meetings'}\n")
    (f,) = _mirror_findings()
    assert f.kind == "bad_setting" and "inside the meetings library" in f.detail


def test_the_doctor_reports_changed_and_blocked_files_and_writes_nothing(drive, sandbox):
    _meeting(WEEKLY)
    _meeting(BUDGET)
    (drive / f"{BUDGET}.txt").write_text("typed by hand")
    mirror.reconcile()
    (drive / f"{WEEKLY}.txt").write_text("edited in Drive")
    config.meeting_file(WEEKLY, ".txt").write_text(_txt(WEEKLY, "Alex Rivera: v2."))
    state = (sandbox / mirror.STATE_NAME).read_bytes()
    files = {n: (drive / n).read_bytes() for n in _names(drive)}
    got = {(f.kind, f.subject) for f in _mirror_findings()}
    assert got == {("mirror_changed", f"{WEEKLY}.txt"),
                   ("mirror_blocked", f"{BUDGET}.txt")}
    assert (sandbox / mirror.STATE_NAME).read_bytes() == state
    assert {n: (drive / n).read_bytes() for n in _names(drive)} == files


def test_a_clean_mirror_has_no_finding(drive):
    _meeting(WEEKLY)
    mirror.reconcile()
    assert _mirror_findings() == []


def test_the_doctor_cli_prints_the_mirror_findings(drive, capsys, monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "tools"))
    import doctor as doctor_cli
    _meeting(WEEKLY)
    (drive / f"{WEEKLY}.txt").write_text("typed by hand")
    doctor_cli.main(["check"])
    out = capsys.readouterr().out
    assert f"mirror_blocked       {WEEKLY}.txt: a file the mirror did not write" in out

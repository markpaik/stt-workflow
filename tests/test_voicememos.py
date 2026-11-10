"""Voice Memos sync (unified plan Phase 2 step 4) against a SYNTHETIC
library: the allow-list, the zero-write rule, re-sync without duplicates,
the schema stop, the doctor report, and the title chain's sidecar. Every
name and title here is synthetic."""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

import run_batch
from stt import config, doctor, provenance, voicememos

SCHEMA = ("CREATE TABLE ZCLOUDRECORDING ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, "
          "Z_OPT INTEGER, ZFLAGS INTEGER, ZFOLDER INTEGER, ZDATE TIMESTAMP, "
          "ZDURATION FLOAT, ZEVICTIONDATE TIMESTAMP, ZCUSTOMLABEL VARCHAR, "
          "ZCUSTOMLABELFORSORTING VARCHAR, ZENCRYPTEDTITLE VARCHAR, ZPATH VARCHAR, "
          "ZUNIQUEID VARCHAR )")
EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
NOW = datetime(2026, 11, 20, 12, 0, 0).astimezone().timestamp()


def _lib(rows, schema=SCHEMA):
    c = voicememos.container_dir()
    c.mkdir(parents=True, exist_ok=True)
    db = c / voicememos.DB_NAME
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(schema)
    for i, r in enumerate(rows):
        start = datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S").astimezone()
        path = r.get("path") or (start.strftime("%Y%m%d %H%M%S") + f"-{i:08X}.m4a")
        cols = "ZUNIQUEID, ZPATH, ZDATE, ZDURATION, ZENCRYPTEDTITLE"
        if "ZENCRYPTEDTITLE" not in schema:
            cols = "ZUNIQUEID, ZPATH, ZDATE, ZDURATION"
        vals = [r.get("uuid", f"UUID-{i}"), path, start.timestamp() - EPOCH,
                r.get("dur", 1200.0)]
        if "ZENCRYPTEDTITLE" in schema:
            vals.append(r.get("title", "New Recording"))
        conn.execute(f"INSERT INTO ZCLOUDRECORDING ({cols}) VALUES "
                     f"({','.join('?' * len(vals))})", vals)
        f = c / path
        if not r.get("no_file"):
            f.write_bytes(b"synthetic memo audio " * 400 + str(i).encode())
            mt = r.get("mtime", start.timestamp() + r.get("dur", 1200.0))
            os.utime(f, (mt, mt))
    conn.commit()
    conn.close()
    return c


def _snapshot(c):
    out = {}
    for p in sorted(c.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.name] = (st.st_size, st.st_mtime,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _staged():
    d = voicememos.staging_dir()
    return sorted(p.name for p in d.glob("*.m4a")) if d.exists() else []


def test_the_allow_list_copies_only_finished_long_new_memos(sandbox):
    _lib([
        {"start": "2026-11-03 09:00:00", "title": "Budget Review"},           # copied
        {"start": "2026-11-01 09:00:00", "title": "Older Meeting"},           # before go-live
        {"start": "2026-11-04 09:00:00", "title": "Quick Note", "dur": 120},  # short
        {"start": "2026-11-20 11:50:00", "title": "Still Going", "dur": 400,  # in progress
         "mtime": NOW - 30},
        {"start": "2026-11-05 09:00:00", "title": "Gone", "no_file": True},   # not local
    ])
    r = voicememos.sync(now=NOW)
    assert r["status"] == "ok"
    assert _staged() == ["2026-11-03 Budget Review.m4a"]
    c = r["counts"]
    assert (c["copied"], c["before_go_live"], c["short"], c["in_progress"],
            c["not_local"]) == (1, 1, 1, 1, 1)


def test_a_zero_byte_file_and_a_missing_length_have_their_own_reasons(sandbox):
    """A zero-byte memo counted as "not_local" (an iCloud placeholder has a
    size and no blocks, not size 0), and a NULL ZDURATION read as 0 s and
    counted as "short". Both are held back, under a reason that says what
    is wrong, and the doctor line names them."""
    c = _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"},
              {"start": "2026-11-04 09:00:00", "title": "Empty One", "uuid": "U-E"},
              {"start": "2026-11-05 09:00:00", "title": "No Length", "uuid": "U-N"}])
    conn = sqlite3.connect(str(c / voicememos.DB_NAME))
    path = conn.execute("SELECT ZPATH FROM ZCLOUDRECORDING WHERE ZUNIQUEID='U-E'"
                        ).fetchone()[0]
    conn.execute("UPDATE ZCLOUDRECORDING SET ZDURATION=NULL WHERE ZUNIQUEID='U-N'")
    conn.commit()
    conn.close()
    f = c / path
    mt = f.stat().st_mtime
    f.write_bytes(b"")
    os.utime(f, (mt, mt))

    r = voicememos.sync(now=NOW)
    assert r["status"] == "ok"
    assert _staged() == ["2026-11-03 Budget Review.m4a"]
    assert r["counts"] == {"copied": 1, "empty": 1, "no_duration": 1}
    lines = {f.kind: f.detail for f in doctor.check(registry=False)}
    assert lines["voicememos_skipped"] == "empty 1, no_duration 1"


def test_no_marker_is_required_and_a_default_title_is_no_label(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "New Recording 4"}])
    voicememos.sync(now=NOW)
    assert _staged() == ["2026-11-03 Voice Memo.m4a"]
    side = json.loads((voicememos.staging_dir()
                       / "2026-11-03 Voice Memo.vm.json").read_text())
    assert side["title_typed"] is False and side["location_note"] is None
    assert side["recorded_at"].startswith("2026-11-03T09:00:00")


def test_a_default_address_title_becomes_a_location_note(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "1200 Example Ave"},
          {"start": "2026-11-04 09:00:00", "title": "1200 Example Ave 2"}])
    voicememos.sync(now=NOW)
    sides = sorted(voicememos.staging_dir().glob("*.vm.json"))
    notes = [json.loads(p.read_text())["location_note"] for p in sides]
    assert notes == ["1200 Example Ave", "1200 Example Ave"]
    assert all(n.startswith("2026-11-0") and " Voice Memo" in n for n in _staged())


def test_a_listed_place_name_is_a_default(sandbox, monkeypatch):
    monkeypatch.setenv("STT_VOICEMEMOS_PLACES", "Sample Cafe, Test Library")
    assert voicememos.classify_title("Sample Cafe") == (False, "Sample Cafe")
    assert voicememos.classify_title("Sample Cafe 3") == (False, "Sample Cafe")
    assert voicememos.classify_title("Budget Review") == (True, None)
    assert voicememos.classify_title("New Recording 2", ["New Recording"]) == (False, None)
    assert voicememos.classify_title("2026-11-03 09:00") == (False, None)


def test_the_library_sees_zero_writes(sandbox):
    c = _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"},
              {"start": "2026-11-04 09:00:00", "title": "Staff Huddle"}])
    before = _snapshot(c)
    voicememos.sync(now=NOW)
    voicememos.sync(now=NOW)
    assert _snapshot(c) == before


def test_a_resync_makes_no_duplicate_even_after_the_batch_removed_the_copy(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    voicememos.sync(now=NOW)
    for p in voicememos.staging_dir().iterdir():
        p.unlink()                       # the batch stored it and deleted the copy
    r = voicememos.sync(now=NOW)
    assert r["counts"].get("already") == 1 and _staged() == []


def test_a_blocked_memo_never_comes_back(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review", "uuid": "U-LEAK"}])
    voicememos.block("U-LEAK")
    r = voicememos.sync(now=NOW)
    assert r["counts"] == {"blocked": 1} and _staged() == []


def test_a_schema_change_stops_ingest_and_the_doctor_reports_it(sandbox):
    no_title = SCHEMA.replace("ZENCRYPTEDTITLE VARCHAR, ", "")
    _lib([{"start": "2026-11-03 09:00:00"}], schema=no_title)
    r = voicememos.sync(now=NOW)
    assert r["status"] == "schema" and "ZENCRYPTEDTITLE" in r["detail"]
    assert _staged() == []
    kinds = {f.kind for f in doctor.check(registry=False)}
    assert "voicememos_schema" in kinds


def test_a_hidden_database_stops_ingest(sandbox):
    c = _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    (c / voicememos.DB_NAME).unlink()
    r = voicememos.sync(now=NOW)
    assert r["status"] == "missing" and _staged() == []


def test_a_denied_read_is_reported(sandbox, monkeypatch):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])

    def deny(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(voicememos, "read_rows", deny)
    assert voicememos.sync(now=NOW)["status"] == "denied"
    assert "voicememos_denied" in {f.kind for f in doctor.check(registry=False)}


def test_sync_can_be_turned_off(sandbox, monkeypatch):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    monkeypatch.setenv("STT_VOICEMEMOS_SYNC", "0")
    assert voicememos.sync(now=NOW)["status"] == "disabled" and _staged() == []


def test_a_duplicated_memo_takes_its_stored_date_as_uncertain(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review",
           "path": "Budget Review copy.m4a"}])
    voicememos.sync(now=NOW)
    side = json.loads((voicememos.staging_dir()
                       / "2026-11-03 Budget Review.vm.json").read_text())
    assert "recorded_at" not in side
    assert (side["date"], side["date_source"]) == ("2026-11-03", "mtime")
    src = voicememos.staging_dir() / "2026-11-03 Budget Review.m4a"
    assert provenance.fresh(src, probe=False) == ("mtime", "2026-11-03", None)


def test_the_batch_sweeps_the_staging_folder(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    voicememos.sync(now=NOW)
    names = [p.name for p in run_batch.iter_sources(config.source_dir())]
    assert "2026-11-03 Budget Review.m4a" in names
    # the library itself is never a source
    assert not any("-0000" in n for n in names)


def test_staged_files_are_private(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    voicememos.sync(now=NOW)
    d = voicememos.staging_dir()
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    for p in d.iterdir():
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct(voicememos.state_path().stat().st_mode & 0o777) == "0o600"


def test_a_numbered_typed_title_stays_typed(sandbox):
    # people number their own series: a repeat is a default only when its
    # base is an address, a listed place, or a 'New Recording' name
    assert voicememos.classify_title("Team Sync 2", ["Team Sync"]) == (True, None)
    assert voicememos.classify_title("Cohort 3", ["Cohort"]) == (True, None)
    assert voicememos.classify_title("1200 Example Ave 2", ["1200 Example Ave"]) == \
        (False, "1200 Example Ave")


def test_a_typed_date_in_the_title_is_not_doubled(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Study Session 11032026"},
          {"start": "2026-11-03 10:00:00", "title": "Board 10152026"},
          {"start": "2026-11-03 11:00:00", "title": "Board 10152026 Notes"}])
    voicememos.sync(now=NOW)
    assert _staged() == ["2026-11-03 Board Notes.m4a", "2026-11-03 Board.m4a",
                         "2026-11-03 Study Session.m4a"]
    from stt import dates
    for n in _staged():
        assert dates.meeting_date(n) == "2026-11-03"


def test_a_typed_date_restamp_does_not_know_stays_in_the_title(sandbox):
    """_staged_name's comment said any date inside a title is removed. Only
    the stamps dates.restamp knows go (a leading ISO date, a trailing or
    standalone 8-digit date). An ISO date in the middle is part of what the
    person typed, so it stays, and the start still leads. A slashed date
    loses its slashes (no file name holds one) and then reads as a trailing
    8-digit stamp, so it goes."""
    _lib([{"start": "2026-11-07 09:00:00", "title": "Notes 2026-10-05 sync"},
          {"start": "2026-11-07 10:00:00", "title": "Standup 10/05/2026"},
          {"start": "2026-11-07 11:00:00", "title": "2026-10-05 Planning"}])
    voicememos.sync(now=NOW)
    assert _staged() == ["2026-11-07 Notes 2026-10-05 sync.m4a",
                         "2026-11-07 Planning.m4a",
                         "2026-11-07 Standup.m4a"]
    from stt import dates
    for n in _staged():
        assert dates.meeting_date(n) == "2026-11-07"


def test_a_denied_stat_on_the_database_is_recorded(sandbox, monkeypatch):
    """Path.exists() re-raises PermissionError: the sync must record it for
    the doctor, never raise."""
    import pathlib
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    real = pathlib.Path.exists

    def exists(self, *a, **k):
        if self.name == voicememos.DB_NAME:
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "exists", exists)
    r = voicememos.sync(now=NOW)
    assert r["status"] == "denied" and _staged() == []
    monkeypatch.setattr(pathlib.Path, "exists", real)
    assert "voicememos_denied" in {f.kind for f in doctor.check(registry=False)}


def test_a_denied_file_stat_is_recorded(sandbox, monkeypatch):
    import pathlib
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    real = pathlib.Path.stat

    def stat(self, *a, **k):
        if self.suffix == ".m4a" and self.parent == voicememos.container_dir():
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    r = voicememos.sync(now=NOW)
    monkeypatch.setattr(pathlib.Path, "stat", real)
    assert r["status"] == "denied" and _staged() == []
    assert voicememos.load_state()["last"]["status"] == "denied"


def test_a_sync_that_raises_is_recorded_by_the_batch(sandbox, monkeypatch):
    def boom(**k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(voicememos, "sync", boom)
    assert run_batch.sync_voice_memos()["status"] == "error"
    assert "voicememos_error" in {f.kind for f in doctor.check(registry=False)}


def test_a_missing_database_is_reported_when_the_app_is_here(sandbox):
    c = _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    (c / voicememos.DB_NAME).unlink()
    assert voicememos.sync(now=NOW)["status"] == "missing"
    assert "voicememos_missing" in {f.kind for f in doctor.check(registry=False)}


def test_no_voice_memos_app_means_no_missing_report(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_VOICEMEMOS_DIR", str(tmp_path / "no-app" / "Recordings"))
    assert voicememos.sync(now=NOW)["status"] == "missing"
    assert "voicememos_missing" not in {f.kind for f in doctor.check(registry=False)}


def test_sync_off_means_no_missing_report(sandbox, monkeypatch):
    c = _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    (c / voicememos.DB_NAME).unlink()
    voicememos.sync(now=NOW)
    monkeypatch.setenv("STT_VOICEMEMOS_SYNC", "0")
    assert "voicememos_missing" not in {f.kind for f in doctor.check(registry=False)}


def test_every_reader_of_the_source_folders_sees_the_voice_memos_staging(sandbox):
    """The batch transcribes the Voice Memos staging copies. The panel queue,
    the menu bar, the doctor's stale-hold check and the archive delete note
    used to read only the iCloud and recorder folders, so a staged memo was
    invisible to them. They all read config.source_dirs now."""
    import json

    import run_batch
    from conftest import mfile
    from gui import menubar, server
    from stt import archive, config, doctor, holds

    stage = voicememos.staging_dir()
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "Staged Memo.m4a").write_bytes(b"\x00" * 64)
    assert stage in config.source_dirs()
    assert config.source_dirs() == list(server._watched_dirs())
    assert "Staged Memo.m4a" in [p.name for p in run_batch.iter_sources(config.source_dir())]
    assert "Staged Memo.m4a" in menubar.gather()["queued"]
    holds.hold("Staged Memo.m4a")
    assert "Staged Memo.m4a" not in doctor._stale_holds()
    mfile("Staged Memo 05012026", ".json").write_text(json.dumps(
        {"source_name": "Staged Memo.m4a", "source_file": "Staged Memo 05012026.m4a",
         "segments": [], "speakers": [], "words": []}))
    r = archive.delete_meeting("Staged Memo 05012026")
    assert r["ok"] and "re-transcribed" in (r["note"] or "")


def test_memo_in_recently_deleted_is_never_eligible(tmp_path):
    from stt import voicememos as vm
    row = {"uuid": "u1", "path": "20261105 100000-ABCDEF12.m4a", "zdate": 0,
           "duration": 600.0, "title": "x", "deleted": True}
    ok, why, _, _ = vm.eligible(row, 2e9, "2000-01-01", tmp_path)
    assert not ok and why == "deleted"


@pytest.mark.parametrize("kind", ["absolute", "traversal"])
def test_a_path_outside_the_container_is_never_eligible(tmp_path, kind):
    """ZPATH comes from the Voice Memos database. A tampered row with an
    absolute or ../ path must not make the sync copy a file from outside
    the container."""
    from stt import voicememos as vm
    container = tmp_path / "Recordings"
    container.mkdir()
    outside = tmp_path / "secret.m4a"
    outside.write_bytes(b"\x01" * 4096)
    os.utime(outside, (1e9, 1e9))
    path = str(outside) if kind == "absolute" else "../secret.m4a"
    row = {"uuid": "u1", "path": path, "zdate": 0, "duration": 600.0,
           "title": "x", "deleted": False}
    ok, why, _, _ = vm.eligible(row, 2e9, "2000-01-01", container)
    assert not ok and why == "outside_library"
    # the same file inside the container is fine
    inside = container / "20261105 100000-ABCDEF12.m4a"
    inside.write_bytes(b"\x01" * 4096)
    os.utime(inside, (1e9, 1e9))
    ok, why, _, _ = vm.eligible(dict(row, path=inside.name), 2e9, "2000-01-01", container)
    assert ok, why

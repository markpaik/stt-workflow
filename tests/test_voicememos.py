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
    assert _staged() == ["Budget Review 11032026.m4a"]
    c = r["counts"]
    assert (c["copied"], c["before_go_live"], c["short"], c["in_progress"],
            c["not_local"]) == (1, 1, 1, 1, 1)


def test_no_marker_is_required_and_a_default_title_is_no_label(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "New Recording 4"}])
    voicememos.sync(now=NOW)
    assert _staged() == ["Voice Memo 11032026.m4a"]
    side = json.loads((voicememos.staging_dir()
                       / "Voice Memo 11032026.vm.json").read_text())
    assert side["title_typed"] is False and side["location_note"] is None
    assert side["recorded_at"].startswith("2026-11-03T09:00:00")


def test_a_default_address_title_becomes_a_location_note(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "1200 Example Ave"},
          {"start": "2026-11-04 09:00:00", "title": "1200 Example Ave 2"}])
    voicememos.sync(now=NOW)
    sides = sorted(voicememos.staging_dir().glob("*.vm.json"))
    notes = [json.loads(p.read_text())["location_note"] for p in sides]
    assert notes == ["1200 Example Ave", "1200 Example Ave"]
    assert all(n.startswith("Voice Memo") for n in _staged())


def test_a_listed_place_name_is_a_default(sandbox, monkeypatch):
    monkeypatch.setenv("STT_VOICEMEMOS_PLACES", "Sample Cafe, Test Library")
    assert voicememos.classify_title("Sample Cafe") == (False, "Sample Cafe")
    assert voicememos.classify_title("Sample Cafe 3") == (False, "Sample Cafe")
    assert voicememos.classify_title("Budget Review") == (True, None)
    assert voicememos.classify_title("Budget Review 2", ["Budget Review"]) == (False, None)
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
                       / "Budget Review 11032026.vm.json").read_text())
    assert "recorded_at" not in side
    assert (side["date"], side["date_source"]) == ("2026-11-03", "mtime")
    src = voicememos.staging_dir() / "Budget Review 11032026.m4a"
    assert provenance.fresh(src, probe=False) == ("mtime", "2026-11-03", None)


def test_the_batch_sweeps_the_staging_folder(sandbox):
    _lib([{"start": "2026-11-03 09:00:00", "title": "Budget Review"}])
    voicememos.sync(now=NOW)
    names = [p.name for p in run_batch.iter_sources(config.source_dir())]
    assert "Budget Review 11032026.m4a" in names
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

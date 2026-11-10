"""Voice Memos delete after ingest against a SYNTHETIC library and a FAKE
`shortcuts` binary: the setting, every candidate rule, the title checks, the
Shortcut's outcomes, the check afterwards, the stop, the doctor, the dry
run, and the batch's end-of-run step. No test runs the real `shortcuts`.
Every name and title here is synthetic."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run_batch
from stt import config, doctor, manifest, voicememos

REAL_STORED_DURATION = voicememos._stored_duration

SCHEMA = ("CREATE TABLE ZCLOUDRECORDING ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, "
          "Z_OPT INTEGER, ZFLAGS INTEGER, ZFOLDER INTEGER, ZDATE TIMESTAMP, "
          "ZDURATION FLOAT, ZEVICTIONDATE TIMESTAMP, ZCUSTOMLABEL VARCHAR, "
          "ZCUSTOMLABELFORSORTING VARCHAR, ZENCRYPTEDTITLE VARCHAR, ZPATH VARCHAR, "
          "ZUNIQUEID VARCHAR )")
EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
EVICTED = 815000000.0          # any Core Data time: the memo is in Recently Deleted

FAKE = r'''#!{python}
import json, os, sqlite3, sys, time
here = os.path.dirname(os.path.abspath(__file__))
sc = json.load(open(os.path.join(here, "scenario.json")))
args = sys.argv[1:]
def log(d):
    with open(os.path.join(here, "calls.jsonl"), "a") as f:
        f.write(json.dumps(d) + "\n")
if args[:1] == ["list"]:
    log({{"cmd": "list"}})
    print("\n".join(sc.get("shortcuts", [])))
    sys.exit(sc.get("list_rc", 0))
if args[:1] != ["run"]:
    sys.exit(64)
name = args[1]
title = open(args[args.index("--input-path") + 1], encoding="utf-8").read()
outp = args[args.index("--output-path") + 1]
log({{"cmd": "run", "name": name, "title": title, "argv": args}})
if sc.get("sleep"):
    time.sleep(sc["sleep"])
db = sqlite3.connect(sc["db"])
# what the owner's Shortcut does: Find Recordings where Name is the input,
# count them, delete only when there is exactly one, output the count
live = [r[0] for r in db.execute(
    "SELECT ZUNIQUEID FROM ZCLOUDRECORDING WHERE ZENCRYPTEDTITLE = ? "
    "AND ZEVICTIONDATE IS NULL", (title,))]
if len(live) == 1 and not sc.get("no_evict"):
    db.execute("UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE = ? WHERE ZUNIQUEID = ?",
               ({evicted}, live[0]))
for u in sc.get("evict_also", []):
    db.execute("UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE = ? WHERE ZUNIQUEID = ?",
               ({evicted}, u))
for u in sc.get("erase", []):
    db.execute("DELETE FROM ZCLOUDRECORDING WHERE ZUNIQUEID = ?", (u,))
db.commit()
db.close()
out = sc.get("output", str(len(live)))
if out is not None:
    open(outp, "w").write(out)
sys.exit(sc.get("rc", 0))
'''


class Fake:
    def __init__(self, d: Path):
        self.dir = d
        d.mkdir(parents=True, exist_ok=True)
        self.bin = d / "shortcuts"
        self.bin.write_text(FAKE.format(python=sys.executable, evicted=EVICTED))
        self.bin.chmod(0o755)
        self.sc = {"shortcuts": [voicememos.SHORTCUT_DEFAULT, "Some Other Shortcut"],
                   "db": str(voicememos.container_dir() / voicememos.DB_NAME)}
        self.save()

    def save(self):
        (self.dir / "scenario.json").write_text(json.dumps(self.sc))

    def set(self, **kw):
        self.sc.update(kw)
        self.save()

    def calls(self, cmd="run"):
        p = self.dir / "calls.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
        return [r for r in rows if r["cmd"] == cmd]


@pytest.fixture
def vm(sandbox, monkeypatch, tmp_path):
    """The delete turned on, a fake shortcuts binary, quick checks, and the
    stored audio's length read from a table instead of ffprobe."""
    monkeypatch.setenv("STT_VM_DELETE_AFTER_INGEST", "1")
    monkeypatch.setattr(voicememos, "VERIFY_WAIT_SEC", 0.0)
    monkeypatch.setattr(voicememos, "VERIFY_POLLS", 2)
    fake = Fake(tmp_path / "fakebin")
    monkeypatch.setattr(voicememos, "SHORTCUTS_BIN", fake.bin)
    durs = {}
    monkeypatch.setattr(voicememos, "_stored_duration",
                        lambda p: durs.get(Path(p).parent.name, 1200.0))
    fake.durs = durs
    return fake


def _lib(rows):
    """The fake Voice Memos database. A row: uuid, title, dur, evicted."""
    c = voicememos.container_dir()
    c.mkdir(parents=True, exist_ok=True)
    db = c / voicememos.DB_NAME
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(SCHEMA)
    for i, r in enumerate(rows):
        start = datetime(2026, 11, 3, 9, 0).astimezone() + timedelta(hours=i)
        conn.execute(
            "INSERT INTO ZCLOUDRECORDING (ZUNIQUEID, ZPATH, ZDATE, ZDURATION, "
            "ZENCRYPTEDTITLE, ZEVICTIONDATE) VALUES (?,?,?,?,?,?)",
            (r["uuid"], start.strftime("%Y%m%d %H%M%S") + f"-{i:08X}.m4a",
             start.timestamp() - EPOCH, r.get("dur", 1200.0), r.get("title"),
             EVICTED if r.get("evicted") else None))
    conn.commit()
    conn.close()
    return db


def _rows():
    return {r["uuid"]: r for r in voicememos.read_rows(require_eviction=True)}


def _stored(uuid, title, note=True, manifest_rec=True, audio=True, base=None):
    """A memo the sync staged and the batch stored: the staged entry, the
    library meeting (json with the memo's id, audio), the manifest record,
    and the batch's ingest note. Returns the meeting base."""
    name = f"2026-11-03 {title or 'Voice Memo'}.m4a"
    base = base or name[:-4]
    d = voicememos.load_state()
    d["staged"][uuid] = {"name": name, "at": f"2026-11-03T10:00:{len(d['staged']):02d}"}
    voicememos._save_state(d)
    folder = config.meeting_dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    mid = config.new_meeting_id()
    j = config.meeting_file(base, ".json")
    j.write_text(json.dumps({"meeting_id": mid, "vm_uuid": uuid, "title": title,
                             "segments": [], "speakers": [], "words": []}))
    config.meeting_file(base, ".txt").write_text("synthetic")
    if audio:
        config.meeting_file(base, ".m4a").write_bytes(b"synthetic stored audio")
    config.index_set(mid, base)
    if manifest_rec:
        manifest.record(name, 1.0, [str(j), str(config.meeting_file(base, ".txt")),
                                    str(config.meeting_file(base, ".m4a"))])
    if note:
        assert voicememos.note_ingested(uuid, name, base, mid)
    return base


def _rec(uuid):
    return voicememos.load_state()["delete"].get(uuid) or {}


def _pass(**kw):
    lines = []
    r = voicememos.delete_pass(log=lines.append, warn=lines.append, **kw)
    r["lines"] = lines
    return r


# ---------- the setting ----------

def test_the_setting_is_off_by_default(vm, monkeypatch):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert not voicememos.delete_enabled()
    assert _pass()["status"] == "off"
    assert vm.calls() == [] and not _rows()["U-1"]["deleted"]


@pytest.mark.parametrize("raw", ["yes", "true", "2", "on"])
def test_an_unreadable_setting_is_off_warns_once_and_the_doctor_lists_it(
        vm, monkeypatch, capsys, raw):
    monkeypatch.setenv("STT_VM_DELETE_AFTER_INGEST", raw)
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert _pass()["status"] == "off" and vm.calls() == []
    assert not voicememos.delete_enabled()
    err = capsys.readouterr().err
    assert err.count(f"STT_VM_DELETE_AFTER_INGEST={raw!r}") == 1 and "using 0" in err
    assert [p[0] for p in config.setting_problems()] == ["STT_VM_DELETE_AFTER_INGEST"]
    found = [f for f in doctor.check(registry=False) if f.kind == "bad_setting"]
    assert [f.subject for f in found] == ["STT_VM_DELETE_AFTER_INGEST"]


def test_the_setting_is_read_from_stt_env(vm, monkeypatch, sandbox):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    (sandbox / "stt.env").write_text("STT_VM_DELETE_AFTER_INGEST=1\n")
    assert voicememos.delete_enabled() and config.setting_problems() == []
    (sandbox / "stt.env").write_text("STT_VM_DELETE_AFTER_INGEST=0\n")
    assert not voicememos.delete_enabled()


def test_sync_off_also_turns_the_delete_off(vm, monkeypatch):
    monkeypatch.setenv("STT_VOICEMEMOS_SYNC", "0")
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert _pass()["status"] == "off" and vm.calls() == []


# ---------- a clean delete ----------

def test_a_stored_memo_moves_to_recently_deleted(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-2", "title": "Staff Huddle"}])
    base = _stored("U-1", "Budget Review")
    r = _pass()
    assert r["status"] == "ok" and [d["title"] for d in r["deleted"]] == ["Budget Review"]
    runs = vm.calls()
    assert len(runs) == 1
    a = runs[0]["argv"]
    assert a[:2] == ["run", "STT Delete Voice Memo"]
    assert a[2] == "--input-path" and a[4] == "--output-path" and len(a) == 6
    assert runs[0]["title"] == "Budget Review"
    rows = _rows()
    assert rows["U-1"]["deleted"] and not rows["U-2"]["deleted"]
    rec = _rec("U-1")
    assert rec["status"] == "deleted" and rec["base"] == base and rec["at"]
    assert rec["tries"] == 1
    assert any(ln.startswith('Voice Memos: moved "Budget Review" to Recently Deleted')
               and base in ln for ln in r["lines"])
    # done for good: the next pass runs nothing
    assert _pass()["status"] == "idle" and len(vm.calls()) == 1


def test_the_shortcut_name_is_a_setting(vm, monkeypatch):
    monkeypatch.setenv("STT_VM_DELETE_SHORTCUT", "Sample Delete Memo")
    vm.set(shortcuts=["Sample Delete Memo"])
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert _pass()["deleted"]
    assert vm.calls()[0]["name"] == "Sample Delete Memo"


def test_the_input_file_holds_the_title_and_nothing_else(vm):
    _lib([{"uuid": "U-1", "title": "Café Planning – Q3"}])
    _stored("U-1", "Café Planning – Q3")
    assert _pass()["deleted"]
    assert vm.calls()[0]["title"] == "Café Planning – Q3"


def test_a_renamed_meeting_is_found_by_its_id(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    base = _stored("U-1", "Budget Review")
    new = "2026-11-03 Budget Review Final"
    old_dir, new_dir = config.meeting_dir(base), config.meeting_dir(new)
    old_dir.rename(new_dir)
    for p in list(new_dir.iterdir()):
        p.rename(new_dir / (new + p.name[len(base):]))
    r = _pass()
    assert r["deleted"] and r["deleted"][0]["base"] == new


def test_a_memo_stored_before_the_note_is_proved_by_its_manifest_record(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review", note=False)
    r = _pass()
    assert r["deleted"]
    # the proof is kept, so a later memo under the same staged name cannot hide it
    assert voicememos.load_state()["staged"]["U-1"]["ingested"]["from"] == "manifest"


def test_a_pass_moves_at_most_the_limit(vm, monkeypatch):
    monkeypatch.setattr(voicememos, "MAX_DELETES_PER_PASS", 2)
    _lib([{"uuid": f"U-{i}", "title": f"Planning Session {i}"} for i in range(4)])
    for i in range(4):
        _stored(f"U-{i}", f"Planning Session {i}")
    assert len(_pass()["deleted"]) == 2
    assert len(_pass()["deleted"]) == 2
    assert _pass()["status"] == "idle" and len(vm.calls()) == 4


# ---------- the candidate rules ----------

def test_a_memo_the_sync_never_staged_is_never_deleted(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-OTHER", "title": "Private Note"}])
    _stored("U-1", "Budget Review")
    _pass()
    rows = _rows()
    assert rows["U-1"]["deleted"] and not rows["U-OTHER"]["deleted"]
    assert [c["title"] for c in vm.calls()] == ["Budget Review"]


def test_a_memo_the_batch_has_not_stored_waits(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review", note=False, manifest_rec=False)
    r = _pass()
    assert vm.calls() == [] and not _rows()["U-1"]["deleted"]
    assert [(i["state"], i["reason"]) for i in r["plan"]] == [("waiting", "not_ingested")]


def test_a_meeting_deleted_from_the_library_keeps_its_memo(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    base = _stored("U-1", "Budget Review")
    shutil.rmtree(config.meeting_dir(base))
    r = _pass()
    assert vm.calls() == [] and not _rows()["U-1"]["deleted"]
    assert r["plan"][0]["reason"] == "meeting_gone"


def test_a_meeting_that_now_holds_another_memo_keeps_this_one(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    base = _stored("U-1", "Budget Review")
    j = config.meeting_file(base, ".json")
    d = json.loads(j.read_text())
    d["vm_uuid"] = "U-SOMEONE-ELSE"
    j.write_text(json.dumps(d))
    assert _pass()["plan"][0]["reason"] == "meeting_gone" and vm.calls() == []


def test_a_meeting_without_stored_audio_keeps_its_memo(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review", audio=False)
    assert _pass()["plan"][0]["reason"] == "no_audio" and vm.calls() == []


@pytest.mark.parametrize("stored,deletes", [(1200.0, True), (1201.9, True),
                                            (1197.5, False), (600.0, False),
                                            (None, False)])
def test_the_stored_audio_must_match_the_memo_within_two_seconds(vm, stored, deletes):
    _lib([{"uuid": "U-1", "title": "Budget Review", "dur": 1200.0}])
    base = _stored("U-1", "Budget Review")
    vm.durs[base] = stored
    r = _pass()
    assert bool(r["deleted"]) is deletes
    if not deletes:
        assert r["plan"][0]["reason"] == "duration_mismatch" and vm.calls() == []


def test_a_memo_already_in_recently_deleted_is_left_alone(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review", "evicted": True}])
    _stored("U-1", "Budget Review")
    r = _pass()
    assert vm.calls() == [] and r["deleted"] == []
    assert r["plan"][0]["state"] == "owner"
    assert _rec("U-1")["status"] == "owner"
    assert _pass()["status"] == "idle"


def test_a_memo_deleted_between_the_two_reads_is_left_alone(vm, monkeypatch):
    """The owner deletes the memo after the pass planned it: the fresh read
    right before the delete sees it in Recently Deleted, and nothing runs."""
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    real = voicememos.read_rows
    reads = []

    def rows(*a, **k):
        reads.append(1)
        if len(reads) == 2:
            _lib([{"uuid": "U-1", "title": "Budget Review", "evicted": True}])
        return real(*a, **k)
    monkeypatch.setattr(voicememos, "read_rows", rows)
    r = _pass()
    assert r["plan"][0]["state"] == "candidate" and vm.calls() == []
    assert _rec("U-1")["status"] == "owner"


def test_a_memo_no_longer_in_the_database_is_done(vm):
    _lib([{"uuid": "U-2", "title": "Staff Huddle"}])
    _stored("U-1", "Budget Review")
    _pass()
    assert vm.calls() == [] and _rec("U-1")["status"] == "gone"


def test_a_run_into_another_folder_never_deletes(vm, tmp_path):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _pass(store=other)["status"] == "not_library"
    assert run_batch.delete_voice_memos(False)["status"] == "not_library"
    assert vm.calls() == [] and not _rows()["U-1"]["deleted"]


def test_the_real_binary_never_runs_against_a_synthetic_library(vm, monkeypatch):
    """The sandbox reads a synthetic container. The real shortcuts would act
    on the real Voice Memos library, so the pass refuses before any process
    starts."""
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    monkeypatch.setattr(voicememos, "SHORTCUTS_BIN", voicememos._REAL_SHORTCUTS)

    def never(*a, **k):
        raise AssertionError("no process may start here")
    monkeypatch.setattr(voicememos.subprocess, "run", never)
    r = _pass()
    assert r["status"] == "sandbox" and not _rows()["U-1"]["deleted"]


def test_a_schema_without_the_recently_deleted_column_stops_the_delete(vm):
    c = voicememos.container_dir()
    c.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(c / voicememos.DB_NAME))
    conn.execute(SCHEMA.replace("ZEVICTIONDATE TIMESTAMP, ", ""))
    conn.execute("INSERT INTO ZCLOUDRECORDING (ZUNIQUEID, ZPATH, ZDATE, ZDURATION, "
                 "ZENCRYPTEDTITLE) VALUES ('U-1', 'x.m4a', 0, 1200.0, 'Budget Review')")
    conn.commit()
    conn.close()
    _stored("U-1", "Budget Review")
    r = _pass()
    assert r["status"] == "schema" and "ZEVICTIONDATE" in r["detail"]
    assert vm.calls() == []


# ---------- the title checks before each delete ----------

@pytest.mark.parametrize("other", ["Budget Review", "budget  review", " Budget Review "])
def test_a_duplicate_title_needs_a_manual_delete(vm, other):
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": other}])
    _stored("U-1", "Budget Review")
    r = _pass()
    assert vm.calls() == []
    rows = _rows()
    assert not rows["U-1"]["deleted"] and not rows["U-2"]["deleted"]
    rec = _rec("U-1")
    assert (rec["status"], rec["reason"]) == ("manual", "duplicate_title")
    assert any("needs a manual delete" in ln for ln in r["lines"])


def test_a_same_title_in_recently_deleted_needs_a_manual_delete(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-OLD", "title": "Budget Review", "evicted": True}])
    _stored("U-1", "Budget Review")
    _pass()
    assert vm.calls() == []
    assert _rec("U-1")["reason"] == "title_in_recently_deleted"


@pytest.mark.parametrize("title", ["", "   ", None])
def test_a_blank_title_needs_a_manual_delete(vm, title):
    _lib([{"uuid": "U-1", "title": title}])
    _stored("U-1", title)
    _pass()
    assert vm.calls() == []
    assert (_rec("U-1")["status"], _rec("U-1")["reason"]) == ("manual", "blank_title")


def test_a_title_problem_found_in_the_fresh_read_blocks_the_delete(vm, monkeypatch):
    """The pass reads once to plan, then again right before each delete. A
    twin that appears between the two reads still blocks the Shortcut."""
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    real = voicememos.read_rows
    reads = []

    def rows(*a, **k):
        reads.append(1)
        if len(reads) == 2:
            _lib([{"uuid": "U-1", "title": "Budget Review"},
                  {"uuid": "U-NEW", "title": "Budget Review"}])
        return real(*a, **k)
    monkeypatch.setattr(voicememos, "read_rows", rows)
    _pass()
    assert vm.calls() == [] and _rec("U-1")["reason"] == "duplicate_title"


def test_a_manual_delete_clears_once_the_title_is_unique_again(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    _pass()
    assert _rec("U-1")["status"] == "manual"
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Budget Review B"}])
    assert _pass()["deleted"] and _rec("U-1")["status"] == "deleted"


def test_a_manual_delete_is_dropped_when_the_meeting_leaves_the_library(vm):
    """The library copy is gone, so the memo is the only copy: no one is
    asked to delete it any more."""
    _lib([{"uuid": "U-1", "title": ""}])
    base = _stored("U-1", "")
    _pass()
    assert _rec("U-1")["status"] == "manual"
    shutil.rmtree(config.meeting_dir(base))
    _pass()
    assert _rec("U-1") == {} and vm.calls() == []
    assert "voicememos_manual_delete" not in {f.kind for f in doctor.check(registry=False)}


def test_a_manual_delete_done_by_the_owner_is_resolved(vm):
    _lib([{"uuid": "U-1", "title": ""}])
    _stored("U-1", "")
    _pass()
    assert _rec("U-1")["status"] == "manual"
    _lib([{"uuid": "U-1", "title": "", "evicted": True}])
    r = _pass()
    assert _rec("U-1")["status"] == "owner" and vm.calls() == []
    assert any("no manual delete is needed" in ln for ln in r["lines"])


# ---------- the Shortcut's outcomes ----------

@pytest.mark.parametrize("output", ["0", "2", "", "yes"])
def test_an_output_that_is_not_one_is_a_failure_with_a_backoff(vm, output):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(output=output, no_evict=True)
    t0 = 1_900_000_000.0
    r = _pass(now=t0)
    assert len(vm.calls()) == 1 and r["failed"]
    rec = _rec("U-1")
    assert rec["status"] == "failed" and rec["reason"] == "output" and rec["tries"] == 1
    assert rec["next_at"] == t0 + voicememos.BACKOFF_SEC[0] and rec["at"]
    # backed off: the next pass inside the wait runs nothing
    assert _pass(now=t0 + 60)["plan"][0]["state"] == "backoff"
    assert len(vm.calls()) == 1
    # after the wait it tries again
    vm.sc.pop("output")
    vm.set(no_evict=False)
    assert _pass(now=t0 + voicememos.BACKOFF_SEC[0] + 1)["deleted"]
    assert _rec("U-1")["tries"] == 2


def test_a_timeout_is_a_failure_and_ends_the_pass(vm, monkeypatch):
    monkeypatch.setattr(voicememos, "SHORTCUT_TIMEOUT_SEC", 0.5)
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Staff Huddle"}])
    _stored("U-1", "Budget Review")
    _stored("U-2", "Staff Huddle")
    vm.set(sleep=5, no_evict=True)
    r = _pass()
    assert len(vm.calls()) == 1          # the second memo waits for the next pass
    assert _rec("U-1")["reason"] == "timeout" and _rec("U-2") == {}
    assert any("was not moved" in ln for ln in r["lines"])


def test_a_non_zero_exit_is_a_failure(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(rc=1, no_evict=True, output=None)
    _pass()
    assert _rec("U-1")["reason"] == "exit"


def test_a_missing_shortcut_runs_nothing_and_the_doctor_says_so(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(shortcuts=["Some Other Shortcut"])
    r = _pass()
    assert r["status"] == "missing" and vm.calls() == []
    assert _rec("U-1") == {}        # not this memo's failure
    kinds = {f.kind for f in doctor.check(registry=False)}
    assert "voicememos_shortcut_missing" in kinds


def test_an_absent_shortcuts_binary_runs_nothing_and_the_doctor_says_so(vm, monkeypatch, tmp_path):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    monkeypatch.setattr(voicememos, "SHORTCUTS_BIN", tmp_path / "gone" / "shortcuts")
    r = _pass()
    assert r["status"] == "no_shortcuts" and not _rows()["U-1"]["deleted"]
    found = [f for f in doctor.check(registry=False)
             if f.kind == "voicememos_shortcut_missing"]
    assert found and "is not here" in found[0].detail


def test_no_shortcut_finding_while_the_setting_is_off(vm, monkeypatch):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    vm.set(shortcuts=[])
    assert "voicememos_shortcut_missing" not in {f.kind for f in doctor.check(registry=False)}
    assert vm.calls("list") == []


def test_a_failed_check_after_the_run_is_a_failure(vm):
    """The Shortcut said 1, but the memo is still live: no success."""
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(no_evict=True, output="1")
    r = _pass()
    assert not r["deleted"]
    rec = _rec("U-1")
    assert (rec["status"], rec["reason"]) == ("failed", "verify_not_deleted")


def test_repeated_failures_end_as_a_manual_delete(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(output="0", no_evict=True)
    t = 1_900_000_000.0
    for _ in range(voicememos.MAX_DELETE_TRIES):
        _pass(now=t)
        t += 10 * 24 * 3600
    rec = _rec("U-1")
    assert (rec["status"], rec["reason"], rec["retry"]) == ("manual", "tries", False)
    n = len(vm.calls())
    assert n == voicememos.MAX_DELETE_TRIES
    _pass(now=t)
    assert len(vm.calls()) == n       # it waits for the owner now
    assert "voicememos_manual_delete" in {f.kind for f in doctor.check(registry=False)}


def test_a_run_killed_mid_shortcut_is_confirmed_on_the_next_pass(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    base = _stored("U-1", "Budget Review")
    voicememos._set_delete("U-1", {"status": "running", "at": "2026-11-03T12:00:00",
                                   "tries": 1, "title": "Budget Review", "base": base,
                                   "next_at": 0})
    _lib([{"uuid": "U-1", "title": "Budget Review", "evicted": True}])
    r = _pass()
    assert vm.calls() == [] and r["deleted"] and _rec("U-1")["late"] is True


def test_a_run_killed_mid_shortcut_waits_out_the_first_backoff(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    t = 1_900_000_000.0
    voicememos._set_delete("U-1", {"status": "running", "at": "x", "tries": 1,
                                   "title": "Budget Review", "next_at": t + 100})
    assert _pass(now=t)["plan"][0]["state"] == "backoff" and vm.calls() == []
    assert _pass(now=t + 101)["deleted"] and _rec("U-1")["tries"] == 2


# ---------- the stop ----------

def test_another_memo_disappearing_stops_every_delete(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-2", "title": "Staff Huddle"},
          {"uuid": "U-3", "title": "Planning Session"}])
    _stored("U-1", "Budget Review")
    _stored("U-3", "Planning Session")
    vm.set(evict_also=["U-2"])
    r = _pass()
    assert r["status"] == "halted" and "other live memo" in r["detail"]
    assert any(ln.startswith("ERROR: Voice Memos deletes are STOPPED") for ln in r["lines"])
    assert len(vm.calls()) == 1                  # U-3 never ran
    st = voicememos.load_state()
    assert st["delete_halt"]["uuid"] == "U-1"
    assert _rec("U-1")["reason"] == "halt"
    # every later pass runs nothing until the owner clears the stop
    r2 = _pass()
    assert r2["status"] == "halted" and len(vm.calls()) == 1
    assert "voicememos_delete_halted" in {f.kind for f in doctor.check(registry=False)}
    assert voicememos.clear_halt()["uuid"] == "U-1"
    vm.set(evict_also=[])
    assert [d["title"] for d in _pass()["deleted"]] == ["Planning Session"]


def test_a_memo_erased_for_good_stops_every_delete(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    vm.set(no_evict=True, erase=["U-1"])
    r = _pass()
    assert r["status"] == "halted"
    assert "left the database instead of moving to Recently Deleted" in r["detail"]


def test_a_memo_in_recently_deleted_erased_by_the_run_stops_every_delete(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-OLD", "title": "Old Note", "evicted": True}])
    _stored("U-1", "Budget Review")
    vm.set(erase=["U-OLD"])
    r = _pass()
    assert r["status"] == "halted" and "other memo(s) left the database" in r["detail"]


def test_a_halt_found_after_a_bad_output_still_stops(vm):
    """Even a run that output 0 is checked for memos it took with it."""
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Staff Huddle"}])
    _stored("U-1", "Budget Review")
    vm.set(output="0", no_evict=True, evict_also=["U-2"])
    assert _pass()["status"] == "halted"


# ---------- the doctor, the panel, the dry run ----------

def test_the_doctor_counts_manual_deletes_without_titles(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Budget Review"},
          {"uuid": "U-3", "title": ""}])
    _stored("U-1", "Budget Review")
    _stored("U-3", "")
    _pass()
    found = [f for f in doctor.check(registry=False) if f.kind == "voicememos_manual_delete"]
    assert len(found) == 1
    assert found[0].detail.startswith("2 memo(s) to delete by hand")
    assert "blank_title 1" in found[0].detail and "duplicate_title 1" in found[0].detail
    assert "Budget Review" not in found[0].detail


def test_the_panel_lists_memos_removed_lately(vm):
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    _pass()
    p = voicememos.delete_panel_state()
    assert p["delete"] is True and p["manual"] == 0 and p["halted"] is False
    assert [r["title"] for r in p["removed"]] == ["Budget Review"]


def _tool(*args):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vm_delete_tool", config.CODE_DIR / "tools" / "vm_delete.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(list(args))


def test_the_dry_run_lists_what_would_happen_and_writes_nothing(vm, capsys):
    _lib([{"uuid": "U-1", "title": "Budget Review"},
          {"uuid": "U-2", "title": "Staff Huddle"}, {"uuid": "U-3", "title": "Staff Huddle"}])
    _stored("U-1", "Budget Review")
    _stored("U-2", "Staff Huddle")
    before = voicememos.state_path().read_bytes()
    assert _tool("--dry-run") == 0
    out = capsys.readouterr().out
    assert vm.calls() == []                         # the Shortcut never ran
    assert voicememos.state_path().read_bytes() == before
    assert not any(r["deleted"] for r in _rows().values())
    assert "would delete (1):" in out and "Budget Review: would move to Recently Deleted" in out
    assert "Staff Huddle: needs a manual delete: another live memo has the same title" in out
    assert 'Shortcut "STT Delete Voice Memo": installed' in out


def test_the_dry_run_works_with_the_setting_off(vm, monkeypatch, capsys):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert _tool() == 0
    out = capsys.readouterr().out
    assert "STT_VM_DELETE_AFTER_INGEST is off" in out and "would delete (1)" in out
    assert vm.calls() == []


def test_apply_runs_one_pass_even_with_the_setting_off(vm, monkeypatch, capsys):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    assert _tool("--apply") == 0
    assert _rows()["U-1"]["deleted"] and len(vm.calls()) == 1
    assert 'moved "Budget Review" to Recently Deleted' in capsys.readouterr().out


def test_apply_refuses_while_a_batch_runs(vm, monkeypatch):
    from stt import status
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    monkeypatch.setattr(status, "read", lambda: {"running": True})
    assert _tool("--apply") == 2 and vm.calls() == []


def test_clear_halt_from_the_tool(vm, capsys):
    voicememos._set_halt("synthetic stop", "U-1")
    assert _tool("--clear-halt") == 0
    assert "Cleared the stop" in capsys.readouterr().out
    assert "delete_halt" not in voicememos.load_state()


def test_a_second_pass_while_one_runs_is_skipped(vm):
    import fcntl
    _lib([{"uuid": "U-1", "title": "Budget Review"}])
    _stored("U-1", "Budget Review")
    with open(config.PROJECT_DIR / voicememos.DELETE_LOCK_NAME, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        assert _pass()["status"] == "busy"
    assert vm.calls() == []


# ---------- the stored audio's real length ----------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("memo_dur,deletes", [(3.0, True), (9.0, False)])
def test_the_length_check_reads_the_stored_file(vm, monkeypatch, memo_dur, deletes):
    monkeypatch.setattr(voicememos, "_stored_duration", REAL_STORED_DURATION)
    _lib([{"uuid": "U-1", "title": "Budget Review", "dur": memo_dur}])
    base = _stored("U-1", "Budget Review")
    a = config.meeting_file(base, ".m4a")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:a", "aac", str(a)], check=True)
    assert bool(_pass()["deleted"]) is deletes



# ---------- the batch ----------

def _batch(monkeypatch, *extra):
    import signal

    from stt import icloud, pipeline, provenance, summarize

    def fake_process_file(s, dest_dir=None, **kw):
        s = Path(s)
        base = s.stem
        config.meeting_dir(base, dest_dir).mkdir(parents=True, exist_ok=True)
        side = provenance.read_sidecar(s)
        j = config.meeting_file(base, ".json", dest_dir)
        mid = config.new_meeting_id()
        j.write_text(json.dumps({"meeting_id": mid, "vm_uuid": side.get("vm_uuid"),
                                 "segments": [], "speakers": [], "words": []}))
        config.index_set(mid, base)
        t = config.meeting_file(base, ".txt", dest_dir)
        t.write_text("synthetic")
        return {"base": base, "txt": t, "json": j, "emb": None,
                "duration_sec": 1200.0, "n_speakers": 1, "identified": []}
    monkeypatch.setattr(pipeline, "process_file", fake_process_file)
    monkeypatch.setattr(icloud, "materialize", lambda p: True)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda *a, **k: None)
    monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(run_batch, "record_review_metrics", lambda: False)
    monkeypatch.setattr(summarize, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize", "--ignore-pause",
                                      "--source", str(config.ICLOUD_DIR), *extra])
    old = signal.getsignal(signal.SIGTERM)
    try:
        return run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)


def _synced_memo():
    """A finished memo in the fake container, with its audio file, copied
    into the staging folder by the real sync."""
    c = voicememos.container_dir()
    _lib([{"uuid": "U-1", "title": "Budget Review"}, {"uuid": "U-2", "title": "Staff Huddle",
                                                     "evicted": True}])
    row = _rows()["U-1"]
    f = c / row["path"]
    f.write_bytes(b"synthetic memo audio " * 400)
    os.utime(f, (1_700_000_000, 1_700_000_000))
    now = datetime(2026, 11, 20, 12, 0).astimezone().timestamp()
    r = voicememos.sync(now=now)
    assert r["counts"].get("copied") == 1


def test_the_batch_moves_a_memo_it_stored_and_says_so(vm, monkeypatch, capsys):
    _synced_memo()
    _batch(monkeypatch)
    out = capsys.readouterr().out
    assert _rows()["U-1"]["deleted"]
    assert len(vm.calls()) == 1 and vm.calls()[0]["title"] == "Budget Review"
    ing = voicememos.load_state()["staged"]["U-1"]["ingested"]
    assert ing["base"] == "2026-11-03 Budget Review" and ing["meeting_id"]
    assert 'Voice Memos: moved "Budget Review" to Recently Deleted' in out
    # the staged copy went after the verified store, as before
    assert not list(voicememos.staging_dir().glob("*.m4a"))


def test_the_batch_leaves_memos_alone_with_the_setting_off(vm, monkeypatch):
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST")
    _synced_memo()
    _batch(monkeypatch)
    assert not _rows()["U-1"]["deleted"] and vm.calls() == []
    # the proof is still noted, so turning the setting on later needs no rerun
    assert voicememos.load_state()["staged"]["U-1"]["ingested"]


def test_a_batch_into_another_folder_never_deletes_or_notes(vm, monkeypatch, tmp_path):
    _synced_memo()
    other = tmp_path / "other_dest"
    _batch(monkeypatch, "--dest", str(other), "--keep-original")
    assert vm.calls() == [] and not _rows()["U-1"]["deleted"]
    assert "ingested" not in voicememos.load_state()["staged"]["U-1"]
    # its manifest record names a meeting outside the library: no proof
    r = _pass()
    assert [i["reason"] for i in r["plan"]] == ["not_ingested"] and vm.calls() == []


def test_a_delete_step_that_raises_never_fails_the_batch(vm, monkeypatch, capsys):
    _synced_memo()

    def boom(**k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(voicememos, "delete_pass", boom)
    assert _batch(monkeypatch) == 0
    assert "Voice Memos delete skipped" in capsys.readouterr().err


def test_a_failed_shortcut_never_fails_the_batch(vm, monkeypatch, capsys):
    _synced_memo()
    vm.set(rc=1, no_evict=True, output=None)
    assert _batch(monkeypatch) == 0
    assert "was not moved to Recently Deleted" in capsys.readouterr().err
    assert _rec("U-1")["status"] == "failed"

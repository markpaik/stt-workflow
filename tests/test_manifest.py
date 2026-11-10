"""manifest: idempotency across the states that bit us in real life —
fresh file, processed, outputs deleted (self-heal), re-recorded file."""
import os
from pathlib import Path

import pytest

from stt import manifest


def _outputs(d, base="m"):
    txt, js = d / f"{base}.txt", d / f"{base}.json"
    txt.write_text("t")
    js.write_text("{}")
    return [str(txt), str(js)]


def test_fresh_file_is_not_processed(sandbox):
    m = manifest.load()
    assert not manifest.is_processed(m, "new.m4a", 1000.0)


def test_mark_then_processed(sandbox):
    m = manifest.load()
    manifest.mark(m, "a.m4a", 1000.0, _outputs(sandbox))
    manifest.save(m)
    m2 = manifest.load()
    assert manifest.is_processed(m2, "a.m4a", 1000.0)


def test_new_mtime_means_new_file(sandbox):
    m = manifest.load()
    manifest.mark(m, "a.m4a", 1000.0, _outputs(sandbox))
    assert not manifest.is_processed(m, "a.m4a", 2000.0)  # re-recorded/replaced


def test_deleted_outputs_self_heal(sandbox):
    """User deletes transcripts to redo them -> file must count as new again."""
    m = manifest.load()
    outs = _outputs(sandbox)
    manifest.mark(m, "a.m4a", 1000.0, outs)
    assert manifest.is_processed(m, "a.m4a", 1000.0)
    (sandbox / "m.json").unlink()
    assert not manifest.is_processed(m, "a.m4a", 1000.0)


def test_corrupt_manifest_recovers(sandbox):
    from stt import config
    config.MANIFEST_PATH.write_text("{not json")
    m = manifest.load()
    assert m == {"processed": {}}


def test_save_uses_tmp_then_atomic_replace(sandbox, monkeypatch):
    """save() must go through write-tmp-then-os.replace, not a direct write —
    a plain write_text() can be interrupted mid-write, truncating the file a
    concurrent reader (another process) sees; os.replace is atomic at the
    filesystem level. Spy on os.replace to prove the mechanism is actually
    used, not just documented in a comment."""
    from stt import config
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (calls.append((src, dst)), real_replace(src, dst)))
    manifest.save({"processed": {"a.m4a": {"mtime": 1.0, "outputs": []}}})
    assert len(calls) == 1
    src, dst = calls[0]
    assert str(src).endswith(".tmp") and dst == config.MANIFEST_PATH
    assert not Path(src).exists()  # renamed away, nothing left behind
    assert manifest.load() == {"processed": {"a.m4a": {"mtime": 1.0, "outputs": []}}}


def test_crash_while_writing_tmp_leaves_the_real_file_untouched(sandbox):
    """The realistic crash window is DURING the tmp-file write (before the
    atomic rename ever happens) — that must never corrupt the real file,
    since os.replace is never reached."""
    from stt import config
    manifest.save({"processed": {"a.m4a": {"mtime": 1.0, "outputs": []}}})
    good_before = config.MANIFEST_PATH.read_text()
    tmp = config.MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text('{"processed": {"b.m4')  # torn write, crash before replace
    assert config.MANIFEST_PATH.read_text() == good_before
    assert manifest.load() == {"processed": {"a.m4a": {"mtime": 1.0, "outputs": []}}}
    tmp.unlink()


def test_a_run_snapshot_can_no_longer_clobber_a_concurrent_record(sandbox):
    """W8: load and save took no file lock. The batch loads the manifest once
    at the start of a run and holds that snapshot for hours; saving it back
    silently discarded every record the GUI (or a second run) wrote in between
    -- dropping a just-finished file's processed record or a retarget's path
    fix. record() is the locked read-modify-write that replaces that pattern."""
    manifest.save({"processed": {}})
    snapshot = manifest.load()                       # what a long run holds

    # the GUI records a file while our run is still transcoding
    manifest.record("gui.m4a", 2.0, _outputs(sandbox, "g"))

    # our run finishes its own file
    rec = manifest.record("run.m4a", 1.0, _outputs(sandbox, "r"))
    snapshot["processed"]["run.m4a"] = rec            # keep the snapshot current

    on_disk = manifest.load()["processed"]
    assert sorted(on_disk) == ["gui.m4a", "run.m4a"], \
        "a finished file's record must not erase one written meanwhile"


def test_the_manifest_lock_spans_a_whole_read_modify_write(sandbox):
    """W8: the lock has to cover the READ as well as the write. A second writer
    that loads mid-cycle would base its save on a stale copy and the later save
    wins."""
    import threading

    manifest.save({"processed": {}})
    started, finished = threading.Event(), threading.Event()

    def other_writer():
        started.set()
        manifest.record("b.m4a", 2.0, [])
        finished.set()

    t = threading.Thread(target=other_writer)
    with manifest.locked():                  # our read-modify-write, in progress
        t.start()
        assert started.wait(5)
        assert not finished.wait(0.4), "another writer got inside our cycle"
        m = manifest.load()
        manifest.mark(m, "a.m4a", 1.0, [])
        manifest.save(m)
    t.join(5)
    assert sorted(manifest.load()["processed"]) == ["a.m4a", "b.m4a"]


def test_load_and_save_each_take_the_lock_on_their_own(sandbox):
    """W8, the part neither test above exercises: a caller that reaches
    load() or save() directly -- not through update()/record() -- must still
    wait on a lock someone else is holding. update()/record() make the whole
    read-modify-write safe; this only proves load() and save() do not read or
    write straight through a lock that is already held."""
    import threading

    manifest.save({"processed": {}})
    entered = threading.Event()
    loaded = []

    def loader():
        entered.set()
        loaded.append(manifest.load())

    with manifest.locked():
        t = threading.Thread(target=loader)
        t.start()
        assert entered.wait(5)
        t.join(0.3)
        assert t.is_alive(), "a bare load() must wait on a held lock"
    t.join(5)
    assert loaded == [{"processed": {}}]

    saved = threading.Event()

    def saver():
        manifest.save({"processed": {"x": 1}})
        saved.set()

    with manifest.locked():
        t2 = threading.Thread(target=saver)
        t2.start()
        assert not saved.wait(0.3), "a bare save() must wait on a held lock too"
    t2.join(5)
    assert manifest.load()["processed"] == {"x": 1}


# ---------- a source that keeps failing is set aside ----------

def _src(d, name="bad.m4a", data=b"x" * 64):
    p = d / name
    p.write_bytes(data)
    return p


def test_three_failures_of_an_unchanged_file_park_it(sandbox):
    p = _src(sandbox)
    for n in (1, 2):
        rec = manifest.record_failure(p.name, p, "boom")
        assert rec["count"] == n and not rec["parked"]
        assert not manifest.is_parked(manifest.load(), p.name, p)
    rec = manifest.record_failure(p.name, p, "boom")
    assert rec["count"] == manifest.FAIL_PARK_AFTER and rec["parked"]
    assert manifest.is_parked(manifest.load(), p.name, p)


def test_an_unreadable_file_is_parked_at_the_first_failure(sandbox):
    p = _src(sandbox)
    manifest.record_failure(p.name, p, "ffmpeg could not decode bad.m4a: x",
                            unreadable=True)
    assert manifest.is_parked(manifest.load(), p.name, p)


def test_a_changed_file_is_no_longer_parked(sandbox):
    """A copy that was still being written fails, then completes: the new
    size and mtime make it a new file, and the count starts again."""
    p = _src(sandbox)
    manifest.record_failure(p.name, p, "x", unreadable=True)
    p.write_bytes(b"y" * 128)
    os.utime(p, (5000.0, 5000.0))
    assert not manifest.is_parked(manifest.load(), p.name, p)
    assert manifest.record_failure(p.name, p, "x")["count"] == 1


def test_a_downloaded_placeholder_is_no_longer_parked(sandbox, monkeypatch):
    """An iCloud placeholder that failed to download keeps its size and
    mtime once it downloads. Its blocks change, so it is not parked."""
    from stt import icloud
    p = _src(sandbox)
    monkeypatch.setattr(icloud, "_fully_present", lambda path: False)
    for _ in range(manifest.FAIL_PARK_AFTER):
        manifest.record_failure(p.name, p, "iCloud file did not fully materialize")
    assert manifest.is_parked(manifest.load(), p.name, p)
    monkeypatch.setattr(icloud, "_fully_present", lambda path: True)
    assert not manifest.is_parked(manifest.load(), p.name, p)


def test_a_half_downloaded_placeholder_is_unparked_once_it_completes(
        sandbox, monkeypatch):
    """A download that timed out halfway has blocks on disk, so it is not
    "dataless". The old signature recorded dataless=False, the finished
    download read the same, and the file stayed parked for good."""
    from stt import icloud
    p = _src(sandbox)
    monkeypatch.setattr(icloud, "is_dataless", lambda path: False)  # has blocks
    monkeypatch.setattr(icloud, "_fully_present", lambda path: False)
    for _ in range(manifest.FAIL_PARK_AFTER):
        manifest.record_failure(p.name, p, "iCloud file did not fully materialize")
    assert manifest.is_parked(manifest.load(), p.name, p)
    monkeypatch.setattr(icloud, "_fully_present", lambda path: True)  # all bytes
    assert not manifest.is_parked(manifest.load(), p.name, p)


def test_an_older_failure_record_still_reads(sandbox):
    """A record written before the change holds "dataless", not "partial"."""
    p = _src(sandbox)
    st = p.stat()
    with manifest.update() as m:
        m.setdefault("failed", {})[p.name] = {
            "mtime": round(st.st_mtime, 3), "size": st.st_size,
            "dataless": False, "count": 3, "parked": True}
    assert manifest.is_parked(manifest.load(), p.name, p)


def test_a_success_clears_the_failure_record(sandbox):
    p = _src(sandbox)
    manifest.record_failure(p.name, p, "x", unreadable=True)
    manifest.record(p.name, p.stat().st_mtime, _outputs(sandbox))
    m = manifest.load()
    assert p.name not in m.get("failed", {})
    assert not manifest.is_parked(m, p.name, p)


def test_a_missing_file_is_never_parked(sandbox):
    p = _src(sandbox)
    manifest.record_failure(p.name, p, "x", unreadable=True)
    p.unlink()
    assert not manifest.is_parked(manifest.load(), p.name, p)
    assert manifest.record_failure(p.name, p, "x") == {}


# ---------- a damaged manifest is kept aside, never written over ----------

def _kept(config):
    return sorted(p for p in config.MANIFEST_PATH.parent.iterdir()
                  if p.name.startswith("manifest.json.bad-"))


def test_a_damaged_manifest_is_copied_aside_before_the_next_write(sandbox, capsys, monkeypatch):
    """load() read a truncated manifest as empty, and the next update() saved
    that empty manifest over it: every processed record was gone, with no
    warning and no copy, and a keep-original setup re-transcribed the whole
    library."""
    from stt import config
    monkeypatch.setitem(manifest._warned, "damaged", False)
    manifest.save({"processed": {"a.m4a": {"mtime": 1.0, "outputs": []}}})
    whole = config.MANIFEST_PATH.read_bytes()
    config.MANIFEST_PATH.write_bytes(whole[:30])
    assert manifest.load() == {"processed": {}}
    assert "manifest.json is not readable" in capsys.readouterr().err
    with manifest.update() as m:
        manifest.mark(m, "b", 2.0, [])
    kept = _kept(config)
    assert len(kept) == 1 and kept[0].read_bytes() == whole[:30]
    assert "kept a copy as " + kept[0].name in capsys.readouterr().err
    assert list(manifest.load()["processed"]) == ["b"]
    # the new file is readable, so the next write keeps no second copy
    manifest.record("c", 3.0, [])
    assert _kept(config) == kept
    assert sorted(manifest.load()["processed"]) == ["b", "c"]


def test_bytes_that_are_not_text_are_a_damaged_manifest(sandbox, monkeypatch):
    from stt import config
    monkeypatch.setitem(manifest._warned, "damaged", False)
    config.MANIFEST_PATH.write_bytes(b'{"processed": {"\xff\xfe": 1}}')
    assert manifest.load() == {"processed": {}}
    manifest.save({"processed": {}})
    assert [p.read_bytes() for p in _kept(config)] == [b'{"processed": {"\xff\xfe": 1}}']


def test_a_readable_manifest_is_never_copied_aside(sandbox):
    from stt import config
    manifest.save({"processed": {}})
    manifest.record("a", 1.0, [])
    assert _kept(config) == []


@pytest.mark.parametrize("text", ["null", "[]", "42", '"abc"', '{"processed": null}',
                                  '{"processed": []}', '{"processed": "x"}'])
def test_json_of_the_wrong_shape_reads_empty_and_is_kept_aside(sandbox, text, monkeypatch):
    """Only bad JSON was caught: 'null', '[]' or '42' made load() raise
    AttributeError on setdefault, and {"processed": null} made
    is_processed() raise. Either stopped the whole batch run."""
    from stt import config
    monkeypatch.setitem(manifest._warned, "damaged", False)
    config.MANIFEST_PATH.write_text(text)
    m = manifest.load()
    assert m == {"processed": {}}
    assert manifest.is_processed(m, "a.m4a", 1.0) is False
    manifest.record("b.m4a", 2.0, [])
    assert list(manifest.load()["processed"]) == ["b.m4a"]
    assert [p.read_text() for p in _kept(config)] == [text]


@pytest.mark.parametrize("rec", [{"mtime": None}, {"mtime": "soon"}, {"mtime": [1]},
                                 {"mtime": float("nan")},
                                 {"mtime": 1.0, "outputs": None},
                                 {"mtime": 1.0, "outputs": "a.json"},
                                 {"mtime": 1.0, "outputs": [None, 5, {"x": 1}]}])
def test_a_record_of_the_wrong_shape_never_raises(sandbox, rec):
    """A record with mtime null or text raised TypeError in is_processed,
    and outputs null raised it too."""
    m = {"processed": {"a.m4a": rec}}
    got = manifest.is_processed(m, "a.m4a", 1.0)
    # an unusable mtime is no record; outputs with no transcript path keep
    # the old reading of a record with no outputs
    want = isinstance(rec.get("mtime"), float) and rec["mtime"] == 1.0
    assert got is want
    # a retarget leaves outputs it cannot read as they are: a string was
    # walked one character at a time and saved back as a list of letters
    import json
    from stt import config
    config.MANIFEST_PATH.write_text(json.dumps(m))
    manifest.retarget("/nowhere/old", "/nowhere/new")
    assert manifest.load()["processed"]["a.m4a"].get("outputs") == rec.get("outputs")


def test_records_that_are_not_objects_are_skipped_and_kept(sandbox, tmp_path):
    """is_processed, retarget, dupes, provenance and the archive scrub call
    rec.get() on every record: a bare 1 or null there raised in each of
    them. Such a record stays on disk for a person to repair
    (tools/doctor.py reports it as manifest_bad_record)."""
    import json
    from stt import archive, config, dupes, provenance
    m = {"processed": {"a.m4a": 1, "b.m4a": None, "c.m4a": {"mtime": 3.0, "outputs": []}}}
    config.MANIFEST_PATH.write_text(json.dumps(m))
    got = manifest.load()
    assert got == m
    assert manifest.is_processed(got, "a.m4a", 1.0) is False
    assert manifest.is_processed(got, "b.m4a", 1.0) is False
    assert manifest.retarget("/nowhere/old", "/nowhere/new") is False
    assert dupes.source_duplicates([], [], man=got) == {}
    assert provenance.manifest_names(got) == {}
    manifest.record("d.m4a", 4.0, [])
    assert sorted(manifest.load()["processed"]) == ["a.m4a", "b.m4a", "c.m4a", "d.m4a"]
    assert _kept(config) == []          # a whole, readable file: no copy

    # a delete still scrubs its own record: the bad ones made the scrub
    # raise, and its except skipped it, so the source read as new again
    from conftest import mfile
    base = "Weekly 05012026"
    mfile(base, ".json").write_text(json.dumps({"date": "2026-05-01", "speakers": [],
                                                "segments": [], "words": []}))
    mfile(base, ".txt").write_text("stub")
    manifest.record("weekly.m4a", 5.0, [config.meeting_file(base, ".json")])
    assert archive.delete_meeting(base)["ok"]
    assert sorted(manifest.load()["processed"]) == ["a.m4a", "b.m4a", "c.m4a", "d.m4a"]


def test_a_failure_list_that_is_not_an_object_reads_as_none(sandbox, capsys, monkeypatch,
                                                            tmp_path):
    """record_failure called .get() on m["failed"] and is_parked did too:
    a list there raised in both."""
    from stt import config
    monkeypatch.setitem(manifest._warned, "damaged", False)
    text = '{"processed": {"c.m4a": {"mtime": 3.0}}, "failed": []}'
    config.MANIFEST_PATH.write_text(text)
    m = manifest.load()
    assert m == {"processed": {"c.m4a": {"mtime": 3.0}}, "failed": {}}
    assert "failure list that is not an object" in capsys.readouterr().err
    src = tmp_path / "d.m4a"
    src.write_bytes(b"x" * 16)
    assert manifest.is_parked(m, "d.m4a", src) is False
    assert manifest.record_failure("d.m4a", src, "boom")["count"] == 1
    assert [p.read_text() for p in _kept(config)] == [text]
    assert manifest.load()["processed"] == {"c.m4a": {"mtime": 3.0}}

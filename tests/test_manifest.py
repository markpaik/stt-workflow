"""manifest: idempotency across the states that bit us in real life —
fresh file, processed, outputs deleted (self-heal), re-recorded file."""
import os
from pathlib import Path

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

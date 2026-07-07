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

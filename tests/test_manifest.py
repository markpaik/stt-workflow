"""manifest: idempotency across the states that bit us in real life —
fresh file, processed, outputs deleted (self-heal), re-recorded file."""
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

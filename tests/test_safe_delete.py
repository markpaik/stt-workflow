"""The batch deletes a source only when the meeting holds the same bytes
(unified plan Phase 2 step 2). Every name here is synthetic."""
import json
from pathlib import Path

import run_batch
from stt import config, icloud, pipeline, provenance


def _fake_process_file(s, dest_dir=None, **kw):
    base = Path(s).stem
    config.meeting_dir(base, dest_dir).mkdir(parents=True, exist_ok=True)
    j = config.meeting_file(base, ".json", dest_dir)
    j.write_text("{}")
    t = config.meeting_file(base, ".txt", dest_dir)
    t.write_text("x")
    return {"base": base, "txt": t, "json": j, "emb": None,
            "duration_sec": 1.0, "n_speakers": 1, "identified": []}


OPTS = {"do_diarize": False, "strict": False, "verify": False,
        "track_unknowns": True, "allowed": None, "do_move": True}


def _setup(monkeypatch, name="Sync 05012026.m4a", body=b"REAL AUDIO BYTES" * 100):
    src_dir = config.source_dir()
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / name
    src.write_bytes(body)
    monkeypatch.setattr(pipeline, "process_file", _fake_process_file)
    monkeypatch.setattr(icloud, "materialize", lambda p: True)
    return src


def test_a_verified_copy_deletes_the_source_and_its_sidecar(sandbox, monkeypatch):
    src = _setup(monkeypatch)
    side = provenance.sidecar_path(src)
    side.write_text(json.dumps({"recorded_at": "2026-05-01T10:00:00-04:00"}))
    res = run_batch.process_one(str(src), str(config.MEETINGS_DIR), OPTS)
    assert res["ok"]
    assert not src.exists() and not side.exists()
    assert config.meeting_file("Sync 05012026", ".m4a").read_bytes() == b"REAL AUDIO BYTES" * 100


def test_a_copy_with_a_wrong_hash_leaves_the_source(sandbox, monkeypatch):
    src = _setup(monkeypatch)
    real_copy = run_batch.shutil.copy2

    def bad_copy(a, b, *r, **k):
        real_copy(a, b, *r, **k)
        data = bytearray(Path(b).read_bytes())
        data[5] ^= 0xFF                      # same size, different bytes
        Path(b).write_bytes(bytes(data))
    monkeypatch.setattr(run_batch.shutil, "copy2", bad_copy)
    run_batch.process_one(str(src), str(config.MEETINGS_DIR), OPTS)
    assert src.exists()


def test_a_short_copy_leaves_the_source(sandbox, monkeypatch):
    src = _setup(monkeypatch)
    real_copy = run_batch.shutil.copy2

    def short_copy(a, b, *r, **k):
        real_copy(a, b, *r, **k)
        Path(b).write_bytes(Path(b).read_bytes()[:-10])
    monkeypatch.setattr(run_batch.shutil, "copy2", short_copy)
    run_batch.process_one(str(src), str(config.MEETINGS_DIR), OPTS)
    assert src.exists()


def test_a_source_changed_during_processing_is_kept(sandbox, monkeypatch, tmp_path):
    src = _setup(monkeypatch)
    dest = tmp_path / "copy.m4a"
    dest.write_bytes(src.read_bytes())
    from stt import dupes
    fp, size = dupes.fingerprint(src), src.stat().st_size
    src.write_bytes(b"NEW BYTES" * 300)
    ok, why = run_batch.safe_to_delete(src, dest, fp, size)
    assert not ok and "changed" in why


def test_an_identical_copy_passes(sandbox, tmp_path):
    src = tmp_path / "a.m4a"
    src.write_bytes(b"x" * 5000)
    dest = tmp_path / "b.m4a"
    dest.write_bytes(b"x" * 5000)
    from stt import dupes
    assert run_batch.safe_to_delete(src, dest, dupes.fingerprint(src), 5000) == (True, "ok")

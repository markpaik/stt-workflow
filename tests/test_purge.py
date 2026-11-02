"""tools/purge.py (unified plan Phase 2 step 3): a synthetic leaked memo
leaves the store, the manifest, search.db, both voice registries, the
centroid index, the review stores, and the watched folders. Every name is
synthetic."""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

from conftest import mfile
from stt import config, holds, identify, search, status, unknowns, voicememos

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import purge  # noqa: E402

LEAK = "Personal Errand 11032026"
KEEP = "Budget Review 11042026"


def _meeting(base, mid, text, **extra):
    d = {"meeting_id": mid, "source_file": f"{base}.m4a", "date": "2026-11-03",
         "speakers": [{"id": "SPEAKER_00", "display": "Speaker 1"}],
         "segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": text}],
         "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text(text)
    mfile(base, ".m4a").write_bytes(b"audio " + base.encode())
    return d


def _world():
    _meeting(LEAK, "mid-leak", "zebracakes groceries pharmacy",
             source_name="Personal Errand.m4a", vm_uuid="VM-LEAK")
    _meeting(KEEP, "mid-keep", "budget lines for spring")
    config.MANIFEST_PATH.write_text(json.dumps({"processed": {
        "Personal Errand.m4a": {"mtime": 1.0, "outputs": [
            str(config.meeting_file(LEAK, ".json")), str(config.meeting_file(LEAK, ".txt"))]},
        "Budget Review.m4a": {"mtime": 2.0, "outputs": [
            str(config.meeting_file(KEEP, ".json"))]}}}))
    search.sync()
    vp = config.VOICEPRINTS_DIR
    np.save(vp / "Avery.npy", np.ones((2, 4)))
    np.save(vp / "Blake.npy", np.ones((1, 4)))
    identify.save_registry({
        "Avery": {"file": "Avery.npy", "n_samples": 2, "sources": [LEAK, KEEP],
                  "source_ids": ["mid-leak", "mid-keep"]},
        "Blake": {"file": "Blake.npy", "n_samples": 1, "sources": [LEAK],
                  "source_ids": ["mid-leak"]}})
    np.save(vp / "U001.npy", np.ones((1, 4)))
    np.save(vp / "U002.npy", np.ones((2, 4)))
    unknowns.save({"next": 3, "speakers": {
        "U001": {"file": "U001.npy", "meetings": [LEAK], "meeting_ids": ["mid-leak"],
                 "links": [{"meeting": LEAK, "meeting_id": "mid-leak", "cluster": "S0"}]},
        "U002": {"file": "U002.npy", "meetings": [LEAK, KEEP],
                 "meeting_ids": ["mid-leak", "mid-keep"],
                 "suggested": [{"uid": "U001", "score": 0.7}],
                 "links": [{"meeting": LEAK, "meeting_id": "mid-leak", "cluster": "S1"},
                           {"meeting": KEEP, "meeting_id": "mid-keep", "cluster": "S0"}]}}})
    src = config.source_dir() / "Personal Errand.m4a"
    src.write_bytes(b"leaked source")
    holds.hold("Personal Errand.m4a")
    Path(status.HISTORY_LOG).write_text(
        json.dumps({"name": "Personal Errand.m4a", "ok": True}) + "\n"
        + json.dumps({"name": "Budget Review.m4a", "ok": True}) + "\n")
    return src


def test_the_dry_run_reports_and_writes_nothing(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _world()
    before = {p: p.read_bytes() for p in config.PROJECT_DIR.rglob("*")
              if p.is_file() and "search.db" not in p.name}
    p = purge.plan(LEAK)
    assert p["ok"] and p["manifest_keys"] == ["Personal Errand.m4a"]
    assert p["enrolled"] == {"Avery": {"rows": [0], "total": 2},
                             "Blake": {"rows": [0], "total": 1}}
    assert p["unknowns"] == {"only": ["U001"], "shared": ["U002"]}
    assert p["search_rows"] >= 2 and len(p["sources"]) == 1
    after = {p: p.read_bytes() for p in config.PROJECT_DIR.rglob("*")
             if p.is_file() and "search.db" not in p.name}
    assert after == before


def test_a_leaked_memo_leaves_every_store(sandbox, monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text(f"see [[{LEAK}]]")
    (wiki / "other.md").write_text("nothing here")
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(wiki))
    src = _world()
    r = purge.apply(LEAK)
    assert r["done"]
    assert r["outside"] == [str(wiki / "page.md")]
    # the store, the manifest, and the watched folder
    assert LEAK not in config.meeting_bases()
    assert not config.meeting_dir(LEAK).exists()
    m = json.loads(config.MANIFEST_PATH.read_text())["processed"]
    assert list(m) == ["Budget Review.m4a"]
    assert not src.exists()
    # search.db: no row, and no copy of the text anywhere in the file
    assert search.meetings("zebracakes") == []
    raw = b"".join(p.read_bytes() for p in config.PROJECT_DIR.glob("search.db*"))
    assert b"zebracakes" not in raw
    conn = sqlite3.connect(str(search.db_path()))
    assert conn.execute("SELECT count(*) FROM files WHERE base = ?", (LEAK,)).fetchone()[0] == 0
    conn.close()
    assert search.meetings("budget")
    # enrolled voices: Avery keeps the other sample, Blake had only this one
    reg = identify.load_registry()
    assert list(reg) == ["Avery"]
    assert reg["Avery"]["sources"] == [KEEP] and reg["Avery"]["n_samples"] == 1
    assert np.load(config.VOICEPRINTS_DIR / "Avery.npy").shape == (1, 4)
    assert not (config.VOICEPRINTS_DIR / "Blake.npy").exists()
    bak = config.VOICEPRINTS_DIR / "registry.json.bak"
    assert LEAK not in bak.read_text()
    # unknown voices: U001 heard only here is gone with its embedding
    ureg = unknowns.load()["speakers"]
    assert "U001" not in ureg and not (config.VOICEPRINTS_DIR / "U001.npy").exists()
    assert ureg["U002"]["meetings"] == [KEEP]
    assert [ln["meeting"] for ln in ureg["U002"]["links"]] == [KEEP]
    assert ureg["U002"]["suggested"] == []
    # holds, history, and the sync block
    assert "Personal Errand.m4a" not in holds.items()
    assert "Personal Errand" not in Path(status.HISTORY_LOG).read_text()
    assert "VM-LEAK" in voicememos.load_state()["blocked"]
    # the other meeting is untouched
    assert json.loads(config.meeting_file(KEEP, ".json").read_text())["meeting_id"] == "mid-keep"


def test_purge_clears_threads_and_overrides_when_they_exist(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _world()
    t = config.PROJECT_DIR / "ask_threads"
    t.mkdir()
    (t / "a.json").write_text(json.dumps({"citations": [{"meeting_id": "mid-leak"}]}))
    (t / "b.json").write_text(json.dumps({"citations": [{"meeting_id": "mid-keep"}]}))
    (config.PROJECT_DIR / "group_overrides.json").write_text(
        json.dumps({"mid-leak": {"not_in": "g1"}, "mid-keep": {"not_in": "g2"}}))
    purge.apply(LEAK)
    assert [p.name for p in t.iterdir()] == ["b.json"]
    assert list(json.loads((config.PROJECT_DIR / "group_overrides.json").read_text())) == [
        "mid-keep"]


def test_an_unknown_meeting_is_refused(sandbox):
    assert purge.plan("No Such Meeting")["ok"] is False
    assert purge.main(["No Such Meeting"]) == 1


def test_an_active_meeting_is_refused(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _world()
    monkeypatch.setattr(status, "meeting_active", lambda b: b == LEAK)
    r = purge.apply(LEAK)
    assert r["ok"] is False and config.meeting_dir(LEAK).exists()


def test_a_legacy_meeting_without_source_name_finds_its_original(sandbox, monkeypatch, tmp_path):
    """A meeting from before source_name existed, renamed since: its JSON
    names neither the arrival name nor the file in the watched folder. The
    manifest key still does, so the purge finds and removes the original,
    and the next batch has nothing to transcribe again."""
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _meeting(LEAK, "mid-leak", "zebracakes groceries pharmacy")
    config.MANIFEST_PATH.write_text(json.dumps({"processed": {
        "Old Arrival.m4a": {"mtime": 1.0, "outputs": [
            str(config.meeting_file(LEAK, ".json"))]}}}))
    src = config.source_dir() / "Old Arrival.m4a"
    src.write_bytes(b"legacy source")
    p = purge.plan(LEAK)
    assert p["sources"] == [str(src)]

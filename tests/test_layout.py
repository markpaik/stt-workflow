"""Per-meeting folder layout: the flat->folder migration, folder-aware rename,
and the stored/editable meeting date."""
import json

from stt import config, summarize
from conftest import mfile


def _flat_meeting(base, extra_exts=(), date_in_json=None):
    """Write a meeting in the OLD flat layout, directly in the meetings dir."""
    d = {"source_file": f"{base}.m4a", "duration_sec": 60.0, "strict": False,
         "speakers": [], "segments": [], "words": []}
    if date_in_json:
        d["date"] = date_in_json
    (config.MEETINGS_DIR / f"{base}.json").write_text(json.dumps(d))
    (config.MEETINGS_DIR / f"{base}.txt").write_text("stub")
    for ext in extra_exts:
        (config.MEETINGS_DIR / f"{base}{ext}").write_text("x")


def test_migration_moves_every_artifact_into_folders(sandbox):
    _flat_meeting("LT Meeting 05212026",
                  extra_exts=(".m4a", ".diar.npz", ".emb.npz",
                              ".reviews.json", ".verify.json"))
    _flat_meeting("Board Prep")
    n = config.migrate_flat_meetings()
    assert n == 9  # 7 + 2 files moved
    for base in ("LT Meeting 05212026", "Board Prep"):
        assert config.meeting_dir(base).is_dir()
        assert config.meeting_file(base, ".json").exists()
        assert not (config.MEETINGS_DIR / f"{base}.json").exists()
    # sidecars travelled WITH their meeting, no bogus "X.reviews" folder
    assert config.meeting_file("LT Meeting 05212026", ".reviews.json").exists()
    assert not (config.MEETINGS_DIR / "LT Meeting 05212026.reviews").exists()
    assert sorted(config.meeting_bases()) == ["Board Prep", "LT Meeting 05212026"]


def test_migration_is_idempotent_and_backfills_date(sandbox):
    _flat_meeting("LT Meeting 05212026")
    config.migrate_flat_meetings()
    assert config.migrate_flat_meetings() == 0  # second run: nothing to do
    d = json.loads(config.meeting_file("LT Meeting 05212026", ".json").read_text())
    assert d["date"] == "2026-05-21"  # filename convention, not mtime


def test_migration_never_overwrites_an_existing_date(sandbox):
    _flat_meeting("LT Meeting 05212026", date_in_json="2026-05-01")
    config.migrate_flat_meetings()
    d = json.loads(config.meeting_file("LT Meeting 05212026", ".json").read_text())
    assert d["date"] == "2026-05-01"  # a human's correction is never clobbered


def test_rename_renames_folder_and_every_file(sandbox):
    for suffix in (".json", ".txt", ".m4a", ".diar.npz", ".reviews.json"):
        p = mfile("Old Name 05212026", suffix)
        if suffix == ".json":
            p.write_text(json.dumps({"source_file": "Old Name 05212026.m4a",
                                     "segments": [], "speakers": [], "words": []}))
        else:
            p.write_text("x")
    r = summarize.rename_meeting("Old Name 05212026", "LT Meeting 05212026")
    assert r["ok"] and len(r["renamed"]) == 5
    assert not config.meeting_dir("Old Name 05212026").exists()
    new_dir = config.meeting_dir("LT Meeting 05212026")
    assert new_dir.is_dir()
    names = sorted(p.name for p in new_dir.iterdir())
    assert names == ["LT Meeting 05212026.diar.npz", "LT Meeting 05212026.json",
                     "LT Meeting 05212026.m4a", "LT Meeting 05212026.reviews.json",
                     "LT Meeting 05212026.txt"]
    d = json.loads(config.meeting_file("LT Meeting 05212026", ".json").read_text())
    assert d["source_file"] == "LT Meeting 05212026.m4a"
    assert d["renamed_from"] == "Old Name 05212026"


def test_rename_refuses_collision(sandbox):
    mfile("A", ".json").write_text("{}")
    mfile("B", ".json").write_text("{}")
    r = summarize.rename_meeting("A", "B")
    assert not r["ok"] and "exists" in r["error"]
    assert config.meeting_dir("A").is_dir()  # nothing was touched


def test_set_meeting_date(sandbox):
    mfile("Mtg", ".json").write_text(json.dumps(
        {"source_file": "Mtg.m4a", "date": "2026-05-30",
         "segments": [], "speakers": [], "words": []}))
    r = summarize.set_meeting_date("Mtg", "2026-04-21")
    assert r == {"ok": True, "date": "2026-04-21"}
    assert json.loads(config.meeting_file("Mtg", ".json").read_text())["date"] == "2026-04-21"
    assert not summarize.set_meeting_date("Mtg", "yesterday")["ok"]
    assert not summarize.set_meeting_date("Nope", "2026-04-21")["ok"]


def _fake_asr():
    import types
    return types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": "fake-asr", "text": "hello from the pipeline",
        "words": [{"start": 0.2, "end": 0.6, "word": "hello"},
                  {"start": 0.7, "end": 1.0, "word": "from"},
                  {"start": 1.1, "end": 1.4, "word": "the"},
                  {"start": 1.5, "end": 1.9, "word": "pipeline"}]})


def test_processed_at_stamped_by_a_run_and_preserved_across_edits(sandbox, monkeypatch, tmp_path):
    """processed_at records when transcription last ran (new or redo). A review
    save re-writes the json but must NOT touch it — it answers 'when was this
    last transcribed', separate from generated_at which changes on every save."""
    import subprocess

    from stt import pipeline, review
    from stt.audio import FFMPEG
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)

    src = tmp_path / "Sync 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=3", "-ac", "1",
                    "-c:a", "aac", str(src)], check=True, capture_output=True)
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    d0 = json.loads(res["json"].read_text())
    assert d0.get("processed_at")  # stamped by the transcription run
    proc = d0["processed_at"]

    # a review save (any edit) must preserve processed_at while re-stamping generated_at
    _, data = review._load("Sync 05012026")
    review._rewrite(res["json"], data)
    d1 = json.loads(res["json"].read_text())
    assert d1["processed_at"] == proc                 # transcription time preserved
    assert "generated_at" in d1                        # (the save clock, re-stamped)


def test_reprocess_after_rename_and_date_change(sandbox, monkeypatch, tmp_path):
    """The user's flow: process a meeting, rename it AND correct its date in
    the panel, then Redo. The reprocess must land everything in the RENAMED
    folder (no resurrection of the old name anywhere), key the manifest by
    the new audio, and keep the human's corrected date rather than
    re-deriving one from the new filename."""
    import subprocess

    from stt import manifest, pipeline, summarize
    from stt.audio import FFMPEG

    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)

    # 1. original processing from a watched-folder file
    src = tmp_path / "Team Sync 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=3", "-ac", "1",
                    "-c:a", "aac", str(src)], check=True, capture_output=True)
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    assert res["json"].exists()
    m = manifest.load()
    manifest.mark(m, src.name, src.stat().st_mtime, [str(res["json"])])
    manifest.save(m)
    import shutil
    shutil.copy2(src, config.meeting_file("Team Sync 05012026", ".m4a"))
    d0 = json.loads(config.meeting_file("Team Sync 05012026", ".json").read_text())
    assert d0["date"] == "2026-05-01"  # stamped from the filename convention

    # 2. rename + human date correction, exactly as the panel does
    r = summarize.rename_meeting("Team Sync 05012026", "Focus Group 06012026")
    assert r["ok"]
    r = summarize.set_meeting_date("Focus Group 06012026", "2026-06-15")
    assert r["ok"]

    # 3. Redo: the panel passes the STORED (renamed) audio path
    stored = config.meeting_audio("Focus Group 06012026")
    assert stored is not None and "Focus Group" in stored.name
    res2 = pipeline.process_file(stored, dest_dir=config.MEETINGS_DIR,
                                 do_diarize=False, do_verify=False)

    # everything lives under the NEW name only
    assert res2["json"] == config.meeting_file("Focus Group 06012026", ".json")
    assert config.meeting_bases() == ["Focus Group 06012026"]
    assert not config.meeting_dir("Team Sync 05012026").exists()

    d = json.loads(res2["json"].read_text())
    assert d["source_file"] == "Focus Group 06012026.m4a"
    assert "hello from the pipeline" in " ".join(
        s["text"] for s in d["segments"])
    # the human's corrected date survives the reprocess — NOT re-derived from
    # the new filename (which would say 2026-06-01)
    assert d["date"] == "2026-06-15"

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

"""Work/Personal categories and archive / restore / delete.

The traps these lock down, in order of how much they'd hurt:
  - an archived meeting getting silently RE-TRANSCRIBED by the next batch run
    (manifest still pointed at the moved outputs -> is_processed self-heals to
    False -> resurrection in the main view)
  - archiving or deleting a meeting the batch is writing right now
  - a restore landing on a name a live meeting has since taken
  - a category silently cleared by a Redo (meta is rebuilt from scratch)
  - an archived meeting still answering on the live endpoints (search/Ask/export)
"""
import json

import pytest

from stt import archive, config, manifest, summarize, unknowns
from conftest import mfile


def _meeting(base, date="2026-05-01", **extra):
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "strict": False,
         "date": date, "speakers": [], "segments": [], "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    return d


# ---------- category ----------

def test_category_set_change_and_clear(sandbox):
    _meeting("Sync 05012026")
    assert summarize.set_meeting_category("Sync 05012026", "work") == {
        "ok": True, "category": "work"}
    d = json.loads(config.meeting_file("Sync 05012026", ".json").read_text())
    assert d["category"] == "work"
    summarize.set_meeting_category("Sync 05012026", "personal")
    assert json.loads(config.meeting_file("Sync 05012026", ".json").read_text()
                      )["category"] == "personal"
    # "" clears the flag entirely rather than storing an empty string
    assert summarize.set_meeting_category("Sync 05012026", "")["category"] is None
    assert "category" not in json.loads(
        config.meeting_file("Sync 05012026", ".json").read_text())


def test_category_rejects_junk_and_missing_meeting(sandbox):
    _meeting("Sync 05012026")
    r = summarize.set_meeting_category("Sync 05012026", "urgent")
    assert not r["ok"] and "work" in r["error"]
    assert not summarize.set_meeting_category("Nope", "work")["ok"]


def test_category_survives_a_redo(sandbox, monkeypatch, tmp_path):
    """process_file rebuilds the meta from scratch, so a human-set flag must be
    carried forward explicitly — exactly like the corrected date."""
    import subprocess

    from stt import pipeline
    from stt.audio import FFMPEG
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)

    src = tmp_path / "Redo Cat 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-ac", "1", "-c:a", "aac",
                    str(src)], check=True, capture_output=True)
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                          do_verify=False)
    summarize.set_meeting_category("Redo Cat 05012026", "personal")
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                          do_verify=False)  # the Redo
    assert json.loads(config.meeting_file("Redo Cat 05012026", ".json").read_text()
                      )["category"] == "personal"


# ---------- archive / restore ----------

def test_archive_hides_the_meeting_from_every_live_surface(sandbox):
    """The whole point of the dot-prefixed folder: meeting_bases() stops listing
    it, and every endpoint gated on that membership (search, Ask, export, voice
    clips, relabel --all) therefore excludes it with no per-endpoint work."""
    _meeting("Private Call 05012026")
    assert archive.archive_meeting("Private Call 05012026") == {
        "ok": True, "base": "Private Call 05012026"}
    assert config.meeting_bases() == []                       # gone from the list
    assert config.archived_bases() == ["Private Call 05012026"]
    assert not config.meeting_dir("Private Call 05012026").exists()
    # the folder moved wholesale — nothing inside was touched
    d = config.archive_dir() / "Private Call 05012026"
    assert (d / "Private Call 05012026.json").exists()
    assert (d / "Private Call 05012026.m4a").exists()


def test_restore_brings_it_back_intact(sandbox):
    _meeting("Private Call 05012026", category="personal")
    archive.archive_meeting("Private Call 05012026")
    assert archive.restore_meeting("Private Call 05012026") == {
        "ok": True, "base": "Private Call 05012026"}
    assert config.meeting_bases() == ["Private Call 05012026"]
    assert config.archived_bases() == []
    d = json.loads(config.meeting_file("Private Call 05012026", ".json").read_text())
    assert d["category"] == "personal"  # byte-identical: nothing was rewritten


def test_restore_onto_a_taken_name_uniquifies_and_follows_the_registry(sandbox):
    """A pre-feature archived name can collide with a live meeting. Restore must
    not clobber it — and the speaker registries reference meetings BY NAME, so
    the refs have to follow or the ▶ voice clips point at a dead meeting."""
    _meeting("Weekly 05012026")
    archive.archive_meeting("Weekly 05012026")
    _meeting("Weekly 05012026")  # a NEW meeting takes the freed name
    reg = unknowns.load()
    reg["speakers"]["U001"] = {"file": "u001.npy", "meetings": ["Weekly 05012026"]}
    unknowns.save(reg)

    r = archive.restore_meeting("Weekly 05012026")
    assert r["ok"] and r["base"] == "Weekly 05012026 (2)"
    assert sorted(config.meeting_bases()) == ["Weekly 05012026", "Weekly 05012026 (2)"]
    # the inner files were renamed with the folder, keeping <base>/<base>.* intact
    assert config.meeting_file("Weekly 05012026 (2)", ".json").exists()
    assert config.meeting_file("Weekly 05012026 (2)", ".m4a").exists()
    assert unknowns.load()["speakers"]["U001"]["meetings"] == ["Weekly 05012026 (2)"]


def test_a_new_name_can_never_collide_with_an_archived_one(sandbox):
    """Rename uniquifies against the archive too, so the ambiguity the test above
    has to clean up can't be created in the first place."""
    _meeting("Standup 05012026")
    archive.archive_meeting("Standup 05012026")
    _meeting("Other 05012026")
    r = summarize.rename_meeting("Other 05012026", "Standup 05012026")
    assert r["ok"] and r["base"] == "Standup 05012026 (2)"


def test_archive_refused_while_the_meeting_is_being_processed(sandbox):
    """Same hazard as rename: process_file resolved its output paths at the start
    and writes them at the end, so moving the folder mid-run would recreate it at
    the old location — a duplicate."""
    from stt import status
    _meeting("Busy 05012026")
    status.set_stage("Busy 05012026.m4a", "transcribing")
    r = archive.archive_meeting("Busy 05012026")
    assert not r["ok"] and "processed" in r["error"]
    assert config.meeting_bases() == ["Busy 05012026"]  # untouched
    assert not archive.delete_meeting("Busy 05012026")["ok"]


def test_archiving_retargets_the_manifest_so_it_is_not_re_transcribed(sandbox):
    """The resurrection bug. With the original audio still in a watched folder
    (keep-original), the manifest's outputs point into the meeting folder. Archive
    moves that folder; is_processed() self-heals on missing outputs, so the file
    would read as NEW and the next run would silently re-transcribe the meeting
    the user just archived."""
    _meeting("Kept 05012026")
    src = config.source_dir() / "Kept 05012026.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio")
    m = manifest.load()
    manifest.mark(m, src.name, src.stat().st_mtime,
                  [config.meeting_file("Kept 05012026", ".txt"),
                   config.meeting_file("Kept 05012026", ".json")])
    manifest.save(m)
    assert manifest.is_processed(manifest.load(), src.name, src.stat().st_mtime)

    archive.archive_meeting("Kept 05012026")
    # STILL processed: the manifest now points at the archived copies
    assert manifest.is_processed(manifest.load(), src.name, src.stat().st_mtime)
    archive.restore_meeting("Kept 05012026")
    assert manifest.is_processed(manifest.load(), src.name, src.stat().st_mtime)


# ---------- delete ----------

def test_delete_removes_everything_and_scrubs_speaker_refs(sandbox):
    _meeting("Gone 05012026")
    reg = unknowns.load()
    reg["speakers"]["U001"] = {"file": "u001.npy",
                               "meetings": ["Gone 05012026", "Other 05012026"]}
    unknowns.save(reg)
    assert archive.delete_meeting("Gone 05012026")["ok"]
    assert config.meeting_bases() == [] and config.archived_bases() == []
    assert not config.meeting_dir("Gone 05012026").exists()
    # the dead meeting no longer drives a ▶ button, but the unknown itself
    # survives — its embedding still identifies that voice in future meetings
    u = unknowns.load()["speakers"]["U001"]
    assert u["meetings"] == ["Other 05012026"]


def test_delete_works_on_an_archived_meeting_too(sandbox):
    _meeting("Gone 05012026")
    archive.archive_meeting("Gone 05012026")
    assert archive.delete_meeting("Gone 05012026")["ok"]
    assert config.archived_bases() == []


def test_delete_warns_when_the_original_would_be_re_transcribed(sandbox):
    """Deleting the transcript while the source audio still sits in a watched
    folder means the next run brings it straight back. Say so, don't let the
    meeting silently reappear."""
    _meeting("Reappears 05012026")
    src = config.source_dir() / "Reappears 05012026.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio")
    r = archive.delete_meeting("Reappears 05012026")
    assert r["ok"] and "re-transcribed" in (r["note"] or "")
    # and with no source lying around, no scary note
    _meeting("Clean 05012026")
    assert archive.delete_meeting("Clean 05012026")["note"] is None


def test_unknown_bases_are_refused_everywhere(sandbox):
    for fn in (archive.archive_meeting, archive.restore_meeting, archive.delete_meeting):
        r = fn("../../etc/passwd")
        assert not r["ok"] and "no " in r["error"]
    assert not archive.restore_meeting("Never Archived")["ok"]

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


def test_rename_collision_uniquifies_instead_of_refusing(sandbox):
    """Two recordings can legitimately end up wanting the same name (a
    recurring meeting recorded twice in one day) — the rename lands as
    'B (2)' rather than bouncing with an error, matching the recorder's own
    naming."""
    mfile("A", ".json").write_text("{}")
    mfile("B", ".json").write_text("{}")
    r = summarize.rename_meeting("A", "B")
    assert r["ok"] and r["base"] == "B (2)"
    assert not config.meeting_dir("A").exists()
    assert config.meeting_dir("B (2)").is_dir()
    assert config.meeting_dir("B").is_dir()  # the existing meeting untouched


def test_rename_appends_meeting_date_for_recurring_names(sandbox):
    """Typing just 'Weekly Check-in' must not collide across weeks: the
    meeting's own date is appended to the FILES (MMDDYYYY, so dates.py parses
    it), while the panel shows the clean name via its title field."""
    mfile("Voice Memo 042", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 042.m4a", "date": "2026-07-03",
         "segments": [], "speakers": [], "words": []}))
    r = summarize.rename_meeting("Voice Memo 042", "Weekly Check-in")
    assert r["ok"] and r["base"] == "Weekly Check-in 07032026"
    assert config.meeting_dir("Weekly Check-in 07032026").is_dir()
    # a name the user dated explicitly is left exactly as typed
    mfile("Voice Memo 043", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 043.m4a", "date": "2026-07-10",
         "segments": [], "speakers": [], "words": []}))
    r2 = summarize.rename_meeting("Voice Memo 043", "Special Review 06152026")
    assert r2["ok"] and r2["base"] == "Special Review 06152026"
    # same clean name, SAME date (recorded twice that day) -> ' (2)'
    mfile("Voice Memo 044", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 044.m4a", "date": "2026-07-03",
         "segments": [], "speakers": [], "words": []}))
    r3 = summarize.rename_meeting("Voice Memo 044", "Weekly Check-in")
    assert r3["ok"] and r3["base"] == "Weekly Check-in 07032026 (2)"


def test_display_title_strips_only_a_trailing_date_stamp(sandbox):
    from gui import server as srv
    assert srv._display_title("Weekly Check-in 07032026") == "Weekly Check-in"
    assert srv._display_title("LT Meeting 05212026") == "LT Meeting"
    # an 8-digit run that is NOT a valid date is part of the name
    assert srv._display_title("Case 99999999") == "Case 99999999"
    # a date in the middle is not a trailing stamp
    assert srv._display_title("Recording 07032026 1430") == "Recording 07032026 1430"
    # a bare date never strips to nothing
    assert srv._display_title("07032026") == "07032026"


def test_set_meeting_date_restamps_the_folder(sandbox):
    """Correcting a date RE-STAMPS the folder. The date lives in the folder name
    to keep recurring meetings unique, so if a date edit left the old stamp in
    place the name and the stored date would silently drift apart (they did)."""
    mfile("Mtg", ".json").write_text(json.dumps(
        {"source_file": "Mtg.m4a", "date": "2026-05-30",
         "segments": [], "speakers": [], "words": []}))
    r = summarize.set_meeting_date("Mtg", "2026-04-21")
    assert r["ok"] and r["date"] == "2026-04-21" and r["base"] == "Mtg 04212026"
    assert config.meeting_dir("Mtg 04212026").is_dir()
    assert not config.meeting_dir("Mtg").exists()
    d = json.loads(config.meeting_file("Mtg 04212026", ".json").read_text())
    assert d["date"] == "2026-04-21"
    assert d["source_file"] == "Mtg 04212026.m4a"
    # correcting it AGAIN replaces the stamp, never appends a second one
    r2 = summarize.set_meeting_date("Mtg 04212026", "2026-04-22")
    assert r2["ok"] and r2["base"] == "Mtg 04222026"
    assert not config.meeting_dir("Mtg 04212026").exists()
    assert not summarize.set_meeting_date("Mtg 04222026", "yesterday")["ok"]
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

    # 2. rename + human date correction, exactly as the panel does. Correcting
    #    the date RE-STAMPS the folder, so the meeting ends up under the date the
    #    human actually chose — the name and the stored date stay in lockstep.
    r = summarize.rename_meeting("Team Sync 05012026", "Focus Group 06012026")
    assert r["ok"] and r["base"] == "Focus Group 06012026"
    r = summarize.set_meeting_date("Focus Group 06012026", "2026-06-15")
    assert r["ok"] and r["base"] == "Focus Group 06152026"
    final = r["base"]

    # 3. Redo: the panel passes the STORED (renamed) audio path
    stored = config.meeting_audio(final)
    assert stored is not None and "Focus Group" in stored.name
    res2 = pipeline.process_file(stored, dest_dir=config.MEETINGS_DIR,
                                 do_diarize=False, do_verify=False)

    # everything lives under the FINAL name only — no resurrection of either
    # the original name or the pre-correction one
    assert res2["json"] == config.meeting_file(final, ".json")
    assert config.meeting_bases() == [final]
    assert not config.meeting_dir("Team Sync 05012026").exists()
    assert not config.meeting_dir("Focus Group 06012026").exists()

    d = json.loads(res2["json"].read_text())
    assert d["source_file"] == f"{final}.m4a"
    assert "hello from the pipeline" in " ".join(
        s["text"] for s in d["segments"])
    # the human's corrected date survives the reprocess (it is NOT re-derived
    # from the filename — which now happens to agree, and must keep agreeing)
    assert d["date"] == "2026-06-15"


def test_rename_refused_while_meeting_is_being_processed(sandbox):
    """A Redo computes the old-name paths at its start and writes them at the
    end; renaming the folder mid-run would leave the run recreating the old
    folder (a duplicate meeting). rename_meeting refuses while the base is in
    the live status active-set, exactly as relabel_one skips."""
    from stt import status
    mfile("Being Processed 05012026", ".json").write_text(json.dumps(
        {"source_file": "Being Processed 05012026.m4a", "date": "2026-05-01",
         "segments": [], "speakers": [], "words": []}))
    status.set_stage("Being Processed 05012026.m4a", "transcribing")
    r = summarize.rename_meeting("Being Processed 05012026", "New Name 05012026")
    assert not r["ok"] and "processed" in r["error"]
    assert config.meeting_dir("Being Processed 05012026").is_dir()  # untouched
    assert not config.meeting_dir("New Name 05012026").exists()


def test_rename_sanitizes_leading_dots_and_control_chars(sandbox):
    """A rename can never resolve the meeting folder to the parent dir or a
    hidden name: leading dots and control characters are stripped, matching
    recorder.final_name. A name that reduces to nothing is refused."""
    mfile("Src 05012026", ".json").write_text(json.dumps(
        {"source_file": "Src 05012026.m4a", "date": "2026-05-01",
         "segments": [], "speakers": [], "words": []}))
    # ".." -> class strip leaves "..", lstrip(".") empties it -> refused
    r = summarize.rename_meeting("Src 05012026", "..")
    assert not r["ok"] and r["error"] == "empty name"
    assert config.meeting_dir("Src 05012026").is_dir()  # not moved
    # a leading dot on a real name is dropped; the date is still appended
    mfile("Src2 05012026", ".json").write_text(json.dumps(
        {"source_file": "Src2 05012026.m4a", "date": "2026-05-01",
         "segments": [], "speakers": [], "words": []}))
    r2 = summarize.rename_meeting("Src2 05012026", ".Hidden\x07 Meeting")
    assert r2["ok"] and r2["base"] == "Hidden Meeting 05012026"
    assert config.meeting_dir("Hidden Meeting 05012026").is_dir()


def test_process_file_writes_under_the_meeting_lock(sandbox, monkeypatch, tmp_path):
    """The write phase (date resolution + every output/cache write) holds the
    same per-meeting lock the panel's edits and relabel take, so a Redo can't
    interleave with a concurrent set_date/review/rename on the same base."""
    import contextlib
    import subprocess

    from stt import pipeline, review
    from stt.audio import FFMPEG
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)

    entered = []
    real = review.lock_meeting

    @contextlib.contextmanager
    def spy(base):
        entered.append(base)
        with real(base):
            yield
    monkeypatch.setattr(review, "lock_meeting", spy)

    src = tmp_path / "Locked 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-ac", "1",
                    "-c:a", "aac", str(src)], check=True, capture_output=True)
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                          do_diarize=False, do_verify=False)
    assert "Locked 05012026" in entered

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
    new_dir = config.meeting_dir("2026-05-21 LT Meeting")
    assert new_dir.is_dir()
    names = sorted(p.name for p in new_dir.iterdir())
    assert names == ["2026-05-21 LT Meeting.diar.npz", "2026-05-21 LT Meeting.json",
                     "2026-05-21 LT Meeting.m4a", "2026-05-21 LT Meeting.reviews.json",
                     "2026-05-21 LT Meeting.txt"]
    d = json.loads(config.meeting_file("2026-05-21 LT Meeting", ".json").read_text())
    assert d["source_file"] == "2026-05-21 LT Meeting.m4a"
    assert d["title"] == "LT Meeting" and d["date"] == "2026-05-21"
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
    meeting's own date goes in front of the FILES (ISO, so dates.py parses
    it), while the panel shows the clean name via its title field."""
    mfile("Voice Memo 042", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 042.m4a", "date": "2026-07-03",
         "segments": [], "speakers": [], "words": []}))
    r = summarize.rename_meeting("Voice Memo 042", "Weekly Check-in")
    assert r["ok"] and r["base"] == "2026-07-03 Weekly Check-in"
    assert config.meeting_dir("2026-07-03 Weekly Check-in").is_dir()
    # a name the user dated explicitly gives the date, and takes the ISO form
    mfile("Voice Memo 043", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 043.m4a", "date": "2026-07-10",
         "segments": [], "speakers": [], "words": []}))
    r2 = summarize.rename_meeting("Voice Memo 043", "Special Review 06152026")
    assert r2["ok"] and r2["base"] == "2026-06-15 Special Review"
    # same clean name, SAME date (recorded twice that day) -> ' (2)'
    mfile("Voice Memo 044", ".json").write_text(json.dumps(
        {"source_file": "Voice Memo 044.m4a", "date": "2026-07-03",
         "segments": [], "speakers": [], "words": []}))
    r3 = summarize.rename_meeting("Voice Memo 044", "Weekly Check-in")
    assert r3["ok"] and r3["base"] == "2026-07-03 Weekly Check-in (2)"


def test_display_title_strips_only_a_trailing_date_stamp(sandbox):
    from gui import server as srv
    assert srv._display_title("2026-07-03 Weekly Check-in") == "Weekly Check-in"
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
    assert r["ok"] and r["date"] == "2026-04-21" and r["base"] == "2026-04-21 Mtg"
    assert config.meeting_dir("2026-04-21 Mtg").is_dir()
    assert not config.meeting_dir("Mtg").exists()
    d = json.loads(config.meeting_file("2026-04-21 Mtg", ".json").read_text())
    assert d["date"] == "2026-04-21"
    assert d["source_file"] == "2026-04-21 Mtg.m4a"
    # correcting it AGAIN replaces the stamp, never appends a second one
    r2 = summarize.set_meeting_date("2026-04-21 Mtg", "2026-04-22")
    assert r2["ok"] and r2["base"] == "2026-04-22 Mtg"
    assert not config.meeting_dir("2026-04-21 Mtg").exists()
    assert not summarize.set_meeting_date("2026-04-22 Mtg", "yesterday")["ok"]
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
    _, data = review._load("2026-05-01 Sync")
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
    shutil.copy2(src, config.meeting_file("2026-05-01 Team Sync", ".m4a"))
    d0 = json.loads(config.meeting_file("2026-05-01 Team Sync", ".json").read_text())
    assert d0["date"] == "2026-05-01"  # stamped from the filename convention
    meeting_id = d0["meeting_id"]      # minted once, by this first run
    assert meeting_id and config.base_of(meeting_id) == "2026-05-01 Team Sync"

    # 2. rename + human date correction, exactly as the panel does. Correcting
    #    the date RE-STAMPS the folder, so the meeting ends up under the date the
    #    human actually chose — the name and the stored date stay in lockstep.
    r = summarize.rename_meeting("2026-05-01 Team Sync", "Focus Group 06012026")
    assert r["ok"] and r["base"] == "2026-06-01 Focus Group"
    r = summarize.set_meeting_date("2026-06-01 Focus Group", "2026-06-15")
    assert r["ok"] and r["base"] == "2026-06-15 Focus Group"
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
    assert not config.meeting_dir("2026-05-01 Team Sync").exists()
    assert not config.meeting_dir("2026-06-01 Focus Group").exists()

    d = json.loads(res2["json"].read_text())
    assert d["source_file"] == f"{final}.m4a"
    assert "hello from the pipeline" in " ".join(
        s["text"] for s in d["segments"])
    # the human's corrected date survives the reprocess (it is NOT re-derived
    # from the filename — which now happens to agree, and must keep agreeing)
    assert d["date"] == "2026-06-15"
    # and so does the meeting's identity: a Redo re-transcribes the SAME
    # meeting, so it keeps its id. Minting a new one here would orphan every
    # registry reference, lock and queued job that names this meeting.
    assert d["meeting_id"] == meeting_id
    assert config.base_of(meeting_id) == final


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
    # a leading dot on a real name is dropped; the date still goes in front
    mfile("Src2 05012026", ".json").write_text(json.dumps(
        {"source_file": "Src2 05012026.m4a", "date": "2026-05-01",
         "segments": [], "speakers": [], "words": []}))
    r2 = summarize.rename_meeting("Src2 05012026", ".Hidden\x07 Meeting")
    assert r2["ok"] and r2["base"] == "2026-05-01 Hidden Meeting"
    assert config.meeting_dir("2026-05-01 Hidden Meeting").is_dir()


def test_process_file_writes_under_the_meeting_lock(sandbox, monkeypatch, tmp_path):
    """The write phase (date resolution + every output/cache write) holds the
    same per-meeting lock the panel's edits and relabel take, so a Redo can't
    interleave with a concurrent set_date/review/rename on the same base."""
    import contextlib
    import subprocess

    from stt import pipeline, review
    from stt.audio import FFMPEG
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())

    entered = []
    real = review.lock_meeting

    @contextlib.contextmanager
    def spy(base, key=None):
        entered.append(base)
        with real(base, key=key):
            yield
    monkeypatch.setattr(review, "lock_meeting", spy)

    src = tmp_path / "Locked 05012026.m4a"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-ac", "1",
                    "-c:a", "aac", str(src)], check=True, capture_output=True)
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                          do_diarize=False, do_verify=False)
    assert "2026-05-01 Locked" in entered


def _sine(path, secs=2):
    import subprocess

    from stt.audio import FFMPEG
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=300:duration={secs}", "-ac", "1",
                    "-c:a", "aac", str(path)], check=True, capture_output=True)


def test_a_new_meeting_leaves_no_base_named_lock(sandbox, monkeypatch, tmp_path):
    """The pipeline took lock_meeting(base) before the json existed, so
    lock_key fell back to the base: every newly processed meeting left an
    empty "<base>.lock" in .locks that the registry doctor then reported as
    orphan_lock. The id is decided first, and the lock is named for it."""
    from stt import pipeline, registry_doctor
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())

    src = tmp_path / "Fresh Mtg 05012026.m4a"
    _sine(src)
    out = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    base = out["base"]
    mid = config.id_of(base)
    assert mid, "the new meeting carries an id"
    locks = config.meetings_dir() / ".locks"
    assert sorted(p.name for p in locks.iterdir()) == [f"{mid}.lock"], \
        "the one lock file is named for the id the json carries"
    assert config.orphan_lock_files() == []
    assert [f for f in registry_doctor.check() if f.kind == "orphan_lock"] == []

    # a Redo keeps its id, and so its lock, exactly as before
    pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                          do_diarize=False, do_verify=False)
    assert config.id_of(base) == mid
    assert sorted(p.name for p in locks.iterdir()) == [f"{mid}.lock"]


def test_two_first_runs_of_one_new_meeting_queue_on_one_lock(sandbox, monkeypatch, tmp_path):
    """Two first runs of the SAME new recording (a file listed twice in one
    batch, or two batches on one file) each found no json, so each minted its
    own id and took a lock named for it. Both were inside the write at once,
    and whichever write landed last silently won. A new meeting's id is now
    decided under one shared lock that is held through the write, so the
    second run waits, finds the id the first one wrote, and queues on it."""
    import threading
    import time
    from pathlib import Path

    from stt import output, pipeline
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    # no ffmpeg: both runs share one scratch wav, and the race under test is
    # the write lock, not the conversion
    monkeypatch.setattr(pipeline.audio, "to_wav16k", lambda s, w: Path(w).touch())
    monkeypatch.setattr(pipeline.audio, "duration_sec", lambda p: 2.0)

    # a run that mints an id waits (briefly) for the other run to reach the
    # same point, so both decide before either writes when nothing stops them
    gate = threading.Barrier(2)
    real_mint = config.new_meeting_id

    def mint():
        try:
            gate.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        return real_mint()
    monkeypatch.setattr(config, "new_meeting_id", mint)

    inside, peak, count = [0], [0], threading.Lock()
    real_write = output.write_json

    def write_json(*a, **k):
        with count:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        try:
            time.sleep(0.3)
            return real_write(*a, **k)
        finally:
            with count:
                inside[0] -= 1
    monkeypatch.setattr(output, "write_json", write_json)

    src = tmp_path / "Twice Mtg 05012026.m4a"
    src.write_bytes(b"not audio: conversion and probing are stubbed")
    out, errs = [], []

    def run():
        try:
            out.append(pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                             do_diarize=False, do_verify=False))
        except Exception as e:     # surfaced below, not lost in the thread
            errs.append(e)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errs, f"the two writes collided: {errs!r}"
    assert len(out) == 2
    assert {o["base"] for o in out} == {"2026-05-01 Twice Mtg"}
    assert peak[0] == 1, "the two writes of one meeting never overlap"
    mid = config.id_of("2026-05-01 Twice Mtg")
    locks = config.meetings_dir() / ".locks"
    assert sorted(p.name for p in locks.iterdir()) == [f"{mid}.lock"], \
        "both runs queued on the one lock named for the id the json carries"
    assert config.orphan_lock_files() == []


def test_migration_leaves_unrelated_files_in_a_picked_folder_alone(sandbox, tmp_path):
    """Choosing a meetings folder runs the migration on whatever is there. A
    folder of other work must not be reorganised: only a real flat-layout
    transcript (a meeting json with its .txt) moves."""
    d = tmp_path / "Documents"
    d.mkdir()
    for name in ("package.json", "report.txt", "2025-01-02 Notes.txt",
                 "2025-01-02 Notes.json", "2025-01-02 Notes.wav", "data.json",
                 "data.txt"):
        (d / name).write_text("{}" if name.endswith(".json") else "x")
    (d / "data.json").write_text(json.dumps({"rows": [1, 2]}))
    real = {"source_file": "Board Prep.m4a", "duration_sec": 60.0,
            "speakers": [], "segments": [], "words": []}
    (d / "Board Prep.json").write_text(json.dumps(real))
    (d / "Board Prep.txt").write_text("stub")
    (d / "Board Prep.m4a").write_text("x")

    config.migrate_flat_meetings(d)
    for name in ("package.json", "report.txt", "2025-01-02 Notes.txt",
                 "2025-01-02 Notes.json", "2025-01-02 Notes.wav", "data.json",
                 "data.txt"):
        assert (d / name).is_file(), name
    assert not (d / "package").exists() and not (d / "data").exists()
    assert not (d / "2025-01-02 Notes").exists()
    for ext in (".json", ".txt", ".m4a"):
        assert (d / "Board Prep" / f"Board Prep{ext}").is_file()


def test_migration_leaves_another_tools_segments_export_alone(sandbox, tmp_path):
    """A json with a segments list and a paired .txt is not enough: another
    tool's export has that shape too. Only a json the pipeline wrote (with
    speakers and source_file) moves, dated name or not."""
    d = tmp_path / "Work"
    d.mkdir()
    (d / "sub.json").write_text(json.dumps({"segments": [{"start": 0, "text": "x"}]}))
    (d / "sub.txt").write_text("x")
    (d / "odd.json").write_text(json.dumps({"segments": ["x"], "speakers": [],
                                            "source_file": "odd.m4a"}))
    (d / "odd.txt").write_text("x")
    config.migrate_flat_meetings(d)
    for name in ("sub.json", "sub.txt", "odd.json", "odd.txt"):
        assert (d / name).is_file(), name
    assert not (d / "sub").exists() and not (d / "odd").exists()


def test_the_adopt_warning_names_other_files_and_flat_meetings(sandbox, tmp_path):
    d = tmp_path / "Pick"
    d.mkdir()
    assert config.adopt_folder_warning(d) is None
    (d / "Kept").mkdir()
    (d / "Kept" / "Kept.json").write_text("{}")
    (d / ".DS_Store").write_text("x")
    assert config.adopt_folder_warning(d) is None   # only meeting folders
    (d / "package.json").write_text("{}")
    (d / "notes").mkdir()
    w = config.adopt_folder_warning(d)
    assert "2 item(s) that are not meetings" in w and "flat-layout" not in w
    real = {"source_file": "Board Prep.m4a", "speakers": [], "segments": []}
    (d / "Board Prep.json").write_text(json.dumps(real))
    (d / "Board Prep.txt").write_text("stub")
    (d / "Board Prep.m4a").write_text("x")
    w = config.adopt_folder_warning(d)
    assert "2 item(s)" in w and "1 older flat-layout meeting(s) will move" in w
    assert config.flat_meeting_bases(d) == ["Board Prep"]


def test_every_ingested_type_is_found_as_stored_audio(sandbox):
    """A non-video source is stored under its own suffix (<base>.flac). The
    stored-audio lookup listed only six suffixes, so a .flac, .ogg or .caf
    meeting had no audio for Redo, playback or the shell sweep."""
    from stt import config
    assert set(config.AUDIO_SUFFIXES) == config.AUDIO_EXTS
    assert config.AUDIO_SUFFIXES[0] == ".m4a"          # the preferred copy first
    for ext in (".flac", ".ogg", ".caf", ".opus", ".aac", ".wma", ".m4b"):
        base = f"2026-01-05 Mtg {ext[1:]}"
        p = config.meeting_file(base, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        assert config.meeting_audio(base) == p


def test_the_shell_sweep_keeps_a_folder_holding_flac(sandbox):
    import run_batch
    from stt import config
    d = config.meeting_dir("2026-01-05 Mtg")
    d.mkdir(parents=True)
    (d / "2026-01-05 Mtg.flac").write_bytes(b"x")
    assert run_batch._sweep_shells() == 0
    assert d.exists()

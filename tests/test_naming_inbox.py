"""Folder naming (title + date, always) and the naming inbox.

The traps these pin down:
  - two recordings that SHARE a filename ('LT Weekly Meeting.m4a' twice) used to
    resolve to the same folder and one transcript overwrote the other
  - a Redo re-stamping (and so duplicating) an existing meeting's folder
  - the 44 meetings processed before the inbox existed retroactively flooding it
  - a rename leaving the manifest pointing at the old folder, so a kept original
    silently re-transcribes into a duplicate
"""
import json
import subprocess

from stt import archive, config, dates, manifest, pipeline, summarize
from stt.audio import FFMPEG
from conftest import mfile


def _audio(path, secs=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=300:duration={secs}", "-ac", "1",
                    "-c:a", "aac", str(path)], check=True, capture_output=True)
    return path


def _meeting(base, date="2026-05-01", **extra):
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": date,
         "speakers": [], "segments": [], "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    return d


# ---------- the shared date-stamp rule ----------

def test_stamp_helpers_round_trip():
    assert dates.strip_stamp("Weekly Check-in 07102026") == "Weekly Check-in"
    assert dates.strip_stamp("Case 99999999") == "Case 99999999"   # not a date
    assert dates.strip_stamp("07102026") == "07102026"             # never empties
    assert dates.stamp("Weekly", "2026-07-10") == "Weekly 07102026"
    # restamp REPLACES an existing stamp rather than appending a second one
    assert dates.restamp("Weekly 07102026", "2026-07-03") == "Weekly 07032026"
    assert dates.meeting_date(dates.stamp("X", "2026-07-10")) == "2026-07-10"


# ---------- the pipeline stamps NEW meetings ----------

def test_dateless_source_gets_the_date_stamped_into_its_folder(sandbox, monkeypatch, tmp_path):
    """The root cause of the duplicate problem: a source named plainly used to
    become a plainly-named folder, so the next recording of the same recurring
    meeting landed on top of it."""
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    src = _audio(tmp_path / "LT Weekly Meeting.m4a")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    base = res["base"]
    assert dates.meeting_date(base) is not None          # the folder carries a date
    assert base.startswith("LT Weekly Meeting ")
    assert config.meeting_dir(base).is_dir()


def test_a_source_that_already_has_a_date_is_left_alone(sandbox, monkeypatch, tmp_path):
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    src = _audio(tmp_path / "LT Weekly Meeting 05212026.m4a")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    assert res["base"] == "LT Weekly Meeting 05212026"   # no second stamp


def test_a_redo_never_renames_an_existing_meetings_folder(sandbox, monkeypatch, tmp_path):
    """resolve_base must resolve a reprocess to the folder the meeting ALREADY
    lives in — otherwise a Redo of one of the legacy dateless meetings would
    stamp a date, land in a NEW folder, and orphan the old one."""
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    _meeting("Legacy Dateless")                            # a pre-existing meeting
    stored = _audio(config.meeting_file("Legacy Dateless", ".m4a"))
    res = pipeline.process_file(stored, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    assert res["base"] == "Legacy Dateless"                # NOT re-stamped
    assert config.meeting_bases() == ["Legacy Dateless"]   # and not duplicated


def test_resolve_base_never_clobbers_a_different_meeting(sandbox, tmp_path):
    """A stamped name already owned by a DIFFERENT recording gets a (2), so one
    transcript can never overwrite another."""
    src = tmp_path / "Sync.m4a"
    src.write_bytes(b"x")
    iso = pipeline._meeting_date(src, None)
    taken = dates.stamp("Sync", iso)
    _meeting(taken)                                   # someone else owns that name
    d = json.loads(config.meeting_file(taken, ".json").read_text())
    d["source_file"] = "a totally different file.m4a"
    config.meeting_file(taken, ".json").write_text(json.dumps(d))
    assert pipeline.resolve_base(src, config.MEETINGS_DIR) == f"{taken} (2)"


# ---------- rename / date keep the folder in lockstep ----------

def test_rename_retargets_the_manifest(sandbox):
    """Without this a kept original reads as unprocessed after a rename (its
    outputs moved) and the next run re-transcribes it into a duplicate."""
    _meeting("Old Name 05012026")
    src = config.source_dir() / "Old Name 05012026.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio")
    m = manifest.load()
    manifest.mark(m, src.name, src.stat().st_mtime,
                  [config.meeting_file("Old Name 05012026", ".txt"),
                   config.meeting_file("Old Name 05012026", ".json")])
    manifest.save(m)
    assert manifest.is_processed(manifest.load(), src.name, src.stat().st_mtime)

    r = summarize.rename_meeting("Old Name 05012026", "New Name")
    assert r["ok"] and r["base"] == "New Name 05012026"
    # STILL processed: the manifest followed the folder
    assert manifest.is_processed(manifest.load(), src.name, src.stat().st_mtime)


def test_tagging_a_category_never_moves_the_folder(sandbox):
    """Only a title or date edit re-stamps. Clicking a tag on one of the legacy
    dateless meetings must not silently rename its folder."""
    _meeting("Legacy Dateless")
    r = summarize.set_meeting_category("Legacy Dateless", "work")
    assert r == {"ok": True, "category": "work"}
    assert config.meeting_dir("Legacy Dateless").is_dir()   # not moved


# ---------- the inbox ----------

def test_new_meetings_land_unreviewed_and_a_redo_keeps_the_state(sandbox, monkeypatch, tmp_path):
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    src = _audio(tmp_path / "Fresh 05012026.m4a")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=False, do_verify=False)
    assert json.loads(res["json"].read_text())["reviewed"] is False   # -> inbox

    summarize.apply_meeting_edits("Fresh 05012026", reviewed=True)
    res2 = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                 do_diarize=False, do_verify=False)   # a Redo
    assert json.loads(res2["json"].read_text())["reviewed"] is True   # stays put


def test_meetings_predating_the_inbox_are_not_dragged_into_it(sandbox):
    """The 40+ already-processed meetings carry no `reviewed` key at all. A
    missing key must read as reviewed, or every one of them floods the inbox."""
    from gui import server as srv
    _meeting("Old Meeting 05012026")   # no reviewed key
    meta = srv._meeting_meta(config.meeting_file("Old Meeting 05012026", ".json"),
                             config.meetings_dir())
    assert meta["needs_review"] is False


def test_accept_names_dates_tags_and_releases_in_one_step(sandbox):
    _meeting("Recording 07112026 1544", date="2026-07-11", reviewed=False)
    r = summarize.apply_meeting_edits("Recording 07112026 1544", title="Board Prep",
                                      date="2026-07-09", category="work",
                                      reviewed=True)
    assert r["ok"]
    assert r["base"] == "Board Prep 07092026"   # renamed AND re-stamped to the date
    d = json.loads(config.meeting_file("Board Prep 07092026", ".json").read_text())
    assert d["date"] == "2026-07-09" and d["category"] == "work" and d["reviewed"] is True


# ---------- the review's confirmed findings ----------

def test_plain_named_source_processes_end_to_end_into_the_stamped_folder(sandbox, monkeypatch):
    """Review finding 1 (critical): run_batch derived the audio-copy folder and
    the summary key from src.stem while process_file stamped a different base —
    the copy raised FileNotFoundError, the file was marked FAILED, and every
    later run re-transcribed it from scratch, forever."""
    import run_batch
    from stt import icloud, pipeline
    from tests.test_layout import _fake_asr
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(icloud, "materialize", lambda p: True)

    src = _audio(config.source_dir() / "LT Weekly Meeting.m4a")
    opts = {"do_diarize": False, "strict": False, "verify": False,
            "track_unknowns": True, "allowed": None, "do_move": True}
    res = run_batch.process_one(str(src), str(config.MEETINGS_DIR), opts)
    assert res["ok"]
    base = res["base"]
    assert base.startswith("LT Weekly Meeting ") and dates.meeting_date(base)
    assert config.meeting_audio(base) is not None   # audio landed in the STAMPED folder
    assert not src.exists()                          # move-after-success consumed it


def test_resolve_base_same_named_different_recording_never_clobbers(sandbox, tmp_path):
    """Findings 2 + 4: name (even name+date) is not identity. A different file
    that happens to share the name gets ' (2)'; the SAME file reprocessed (same
    mtime, e.g. a Redo or force) resolves back to its own meeting."""
    import shutil

    from stt import pipeline
    src = _audio(tmp_path / "Sync 05012026.m4a")
    d = _meeting("Sync 05012026", date="2026-05-01",
                 source_mtime=round(src.stat().st_mtime, 3))
    # the same recording, reprocessed -> its own folder
    assert pipeline.resolve_base(src, config.MEETINGS_DIR) == "Sync 05012026"
    # a DIFFERENT file under the same name (same-day second session, re-export)
    src2 = tmp_path / "other" / "Sync 05012026.m4a"
    src2.parent.mkdir()
    shutil.copy(src, src2)                       # copy (not copy2): new mtime
    import os as _os
    _os.utime(src2, (src.stat().st_atime, src.stat().st_mtime + 3600))
    assert pipeline.resolve_base(src2, config.MEETINGS_DIR) == "Sync 05012026 (2)"


def test_resolve_base_reuses_its_own_suffixed_slot(sandbox, tmp_path):
    """Finding 8: a source whose meeting already lives at '<stamped> (2)' must
    resolve back to that folder on reprocess, not mint '(3)'."""
    from stt import pipeline
    src = _audio(tmp_path / "Sync 05012026.m4a")
    _meeting("Sync 05012026",                        # someone else's meeting
             source_file="A Different Recording.m4a")
    _meeting("Sync 05012026 (2)",                    # THIS source's meeting: it
             source_file="Sync 05012026.m4a",        # records the SOURCE name,
             source_mtime=round(src.stat().st_mtime, 3))  # not the folder name
    assert pipeline.resolve_base(src, config.MEETINGS_DIR) == "Sync 05012026 (2)"


def test_resolve_base_never_reuses_an_archived_name(sandbox, tmp_path):
    """Finding 7: registries key meetings by name — a live meeting squatting on
    an archived name would corrupt that meeting's restore."""
    from stt import pipeline
    _meeting("Sync 05012026")
    archive.archive_meeting("Sync 05012026")
    src = _audio(tmp_path / "Sync 05012026.m4a")
    assert pipeline.resolve_base(src, config.MEETINGS_DIR) == "Sync 05012026 (2)"


def test_active_guard_recognizes_the_stamped_base(sandbox):
    """Finding 6: active entries are keyed by SOURCE name; the stamped folder
    name differs, so the mid-run rename/archive refusal went dead for new
    meetings. The run announces its resolved base; the guard must honor it."""
    from stt import status
    _meeting("LT Weekly Meeting 06042026")
    status.set_stage("LT Weekly Meeting.m4a", "transcribing",
                     base="LT Weekly Meeting 06042026")
    r = summarize.rename_meeting("LT Weekly Meeting 06042026", "New Name")
    assert not r["ok"] and "processed" in r["error"]
    assert not archive.archive_meeting("LT Weekly Meeting 06042026")["ok"]


def test_explicit_date_beats_a_mid_title_digit_run(sandbox):
    """Finding 5: accepting the recorder default 'Recording 07112026 1032' with
    a corrected date silently discarded the correction — the mid-name digit run
    won. Only a TRAILING stamp counts, and the picker always wins."""
    _meeting("Recording 07112026 1032", date="2026-07-11", reviewed=False)
    r = summarize.apply_meeting_edits("Recording 07112026 1032",
                                      title="Recording 07112026 1032",
                                      date="2026-07-15", reviewed=True)
    assert r["ok"]
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["date"] == "2026-07-15"          # the picker won


def test_a_future_trailing_date_is_kept_in_the_name_but_not_stored(sandbox):
    """Finding 15: 'Planning Retreat 12312026' names an EVENT; storing it as the
    meeting date would sort the meeting above every real one for months."""
    _meeting("Mtg 05012026", date="2026-05-01")
    r = summarize.rename_meeting("Mtg 05012026", "Planning Retreat 12312026")
    assert r["ok"] and r["base"] == "Planning Retreat 12312026"  # name as typed
    d = json.loads(config.meeting_file(r["base"], ".json").read_text())
    assert d["date"] == "2026-05-01"          # stored date untouched


def test_date_restamp_replaces_not_appends_on_a_twin(sandbox):
    """Finding 9: 'Weekly 07032026 (2)' + a corrected date must not become
    'Weekly 07032026 (2) 07102026'."""
    _meeting("Weekly 07032026 (2)", date="2026-07-03")
    r = summarize.set_meeting_date("Weekly 07032026 (2)", "2026-07-10")
    assert r["ok"] and r["base"] == "Weekly 07102026"


def test_case_only_retitle_does_not_mint_a_twin(sandbox):
    """Finding 10: on case-insensitive APFS, a capitalization fix saw its own
    folder as taken and appended ' (2)'."""
    _meeting("board prep 07092026", date="2026-07-09")
    r = summarize.rename_meeting("board prep 07092026", "Board Prep 07092026")
    assert r["ok"] and r["base"] == "Board Prep 07092026"


def test_restore_stamps_a_legacy_plain_name(sandbox):
    """Finding 11: a meeting archived before the convention restores under its
    plain name — which would shadow its recurring series all over again."""
    _meeting("LT Weekly Meeting", date="2026-06-04")
    archive.archive_meeting("LT Weekly Meeting")
    r = archive.restore_meeting("LT Weekly Meeting")
    assert r["ok"] and r["base"] == "LT Weekly Meeting 06042026"
    assert config.meeting_bases() == ["LT Weekly Meeting 06042026"]


def test_stale_recorder_refuses_start_and_pause(sandbox, monkeypatch, tmp_path):
    """Finding 3: an old binary has no SIGUSR1 handler — the default disposition
    would TERMINATE it mid-meeting. A binary older than its source refuses."""
    from stt import recorder, status
    binary = tmp_path / "stt-recorder"
    swift = tmp_path / "recorder.swift"
    binary.write_bytes(b"x")
    import os as _os, time as _time
    swift.write_text("//")
    _os.utime(binary, (1, 1))                  # binary predates the source
    monkeypatch.setattr(recorder, "BINARY", binary)
    monkeypatch.setattr(recorder, "SWIFT_SRC", swift)
    monkeypatch.setattr(recorder.os, "access", lambda p, m: True)
    r = recorder.start()
    assert not r["ok"] and "rebuild" in r["error"]
    status.set_recording({"pid": 4242, "caf": "/x/.rec-a.caf"})
    monkeypatch.setattr(recorder, "_recorder_running", lambda pid: True)
    r = recorder.pause()
    assert not r["ok"] and "rebuild" in r["error"]


# ---------- bulk-only ops ----------

def test_drop_audio_keeps_the_transcript(sandbox):
    _meeting("Done 05012026")
    assert config.meeting_audio("Done 05012026") is not None
    r = archive.drop_audio("Done 05012026")
    assert r["ok"] and r["freed_mb"] == 0.0        # tiny stub file
    assert config.meeting_audio("Done 05012026") is None
    assert config.meeting_file("Done 05012026", ".json").exists()   # transcript kept
    assert config.meeting_bases() == ["Done 05012026"]
    assert not archive.drop_audio("Nope")["ok"]

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

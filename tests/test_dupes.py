"""Duplicate detection, both halves.

PREVENTION: a source file that was already processed, back in the watched folder
under a new name (or a new mtime) — the manifest keys on name + mtime and cannot
see it, so without a content check the pipeline transcribes it a second time.

CURE: two transcripts of the same recording already sitting in the library.

The rules these pin are the ones that keep the feature honest: identical bytes
is proof and may be swept in bulk, a shared filename is only a suggestion and is
deleted one file at a time, and no transcript is ever deleted automatically.
"""
import json

from gui import server as srv
from stt import config, dupes, manifest
from conftest import mfile


# ------------------------------------------------------------ fingerprints --

def test_fingerprint_matches_a_copy_and_splits_on_a_changed_tail(sandbox):
    a = sandbox / "a.m4a"
    a.write_bytes(b"HEAD" + b"\x01" * 5000 + b"TAIL")
    b = sandbox / "b.m4a"                     # a plain copy under another name
    b.write_bytes(a.read_bytes())
    c = sandbox / "c.m4a"                     # same size, different content
    c.write_bytes(b"HEAD" + b"\x01" * 5000 + b"XXXX")
    assert dupes.fingerprint(a) == dupes.fingerprint(b)
    assert dupes.fingerprint(a) != dupes.fingerprint(c)
    assert dupes.fingerprint(sandbox / "missing.m4a") is None


def test_fingerprint_never_reads_a_dataless_icloud_file(sandbox, monkeypatch):
    """Reading one would trigger a multi-gigabyte download from a 2s status
    poll: the exact surprise this feature exists to prevent. No fingerprint is
    the honest answer, and the name check still covers the file."""
    from stt import icloud
    p = sandbox / "cloud.m4a"
    p.write_bytes(b"x" * 4096)
    monkeypatch.setattr(icloud, "_fully_present", lambda _p: False)
    dupes._fp_cache.clear()
    assert dupes.fingerprint(p) is None


# ------------------------------------------------------- source duplicates --

def _meeting(base, source_file, audio=b"", text="", dur=600.0):
    mfile(base, ".json").write_text(json.dumps(
        {"source_file": source_file, "duration_sec": dur,
         "speakers": [], "segments": [{"text": text}], "words": []}))
    mfile(base, ".txt").write_text("stub")
    if audio:
        mfile(base, ".m4a").write_bytes(audio)
    return {"base": base, "source_file": source_file, "title": base}


def test_a_renamed_copy_is_caught_by_its_bytes(sandbox):
    audio = b"RIFF" + b"\x07" * 9000
    metas = [_meeting("Board Prep 05012026", "board prep.m4a", audio)]
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    copy = src / "board prep 2.m4a"           # same recording, new name
    copy.write_bytes(audio)
    other = src / "brand new.m4a"
    other.write_bytes(b"RIFF" + b"\x09" * 9000)

    out = dupes.source_duplicates([copy, other], metas, {}, config.meetings_dir())
    assert out == {"board prep 2.m4a": {"base": "Board Prep 05012026",
                                        "reason": "identical"}}


def test_a_shared_filename_is_flagged_but_only_as_a_name(sandbox):
    """A recurring export honestly reuses one name for genuinely different
    meetings, so this signal must stay weaker than a byte match: it earns a
    chip and the ordinary one-file delete, never the bulk sweep."""
    metas = [_meeting("Weekly Sync 05012026", "Weekly Sync.m4a", b"OLD" + b"\x01" * 900)]
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    again = src / "Weekly Sync.m4a"           # next week's meeting, same name
    again.write_bytes(b"NEW" + b"\x02" * 4000)

    out = dupes.source_duplicates([again], metas, {}, config.meetings_dir())
    assert out == {"Weekly Sync.m4a": {"base": "Weekly Sync 05012026",
                                       "reason": "name"}}


def test_a_video_source_is_caught_by_the_manifest_fingerprint(sandbox):
    """A video's stored meeting audio is an EXTRACTED .m4a that shares no bytes
    with the .mp4 it came from, so the audio-on-disk check cannot see a repeat
    copy of that video. The fingerprint the manifest recorded for the original
    source is the only signal that survives, which is why run_batch takes it
    before the pipeline touches the file."""
    video = b"ftyp" + b"\x05" * 9000
    _meeting("All Staff 05012026", "all staff.mp4", b"extracted-audio-bytes")
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    original = src / "all staff.mp4"
    original.write_bytes(video)
    fp = dupes.fingerprint(original)
    m = {"processed": {"all staff.mp4": {
        "mtime": 1.0, "fp": fp, "size": len(video),
        "outputs": [str(mfile("All Staff 05012026", ".json"))]}}}
    original.rename(src / "all staff copy.mp4")
    again = src / "all staff copy.mp4"

    out = dupes.source_duplicates([again], [], m, config.meetings_dir())
    assert out == {"all staff copy.mp4": {"base": "All Staff 05012026",
                                          "reason": "identical"}}


def test_manifest_records_stay_readable_without_a_fingerprint(sandbox):
    """The fp is additive: every record written before it existed carries none,
    and both is_processed and the duplicate check must go on working."""
    m = manifest.load()
    manifest.mark(m, "old.m4a", 10.0, [str(mfile("Old 05012026", ".json"))])
    manifest.save(m)
    rec = manifest.load()["processed"]["old.m4a"]
    assert "fp" not in rec and "size" not in rec
    manifest.mark(m, "new.m4a", 11.0, [], fp="abc", size=7)
    assert m["processed"]["new.m4a"]["fp"] == "abc"
    assert m["processed"]["new.m4a"]["size"] == 7


# --------------------------------------------------- duplicate transcripts --

def _words(n, salt=""):
    return " ".join(f"{salt}word{i}" for i in range(n))


def test_the_same_meeting_twice_is_found_and_different_ones_are_not(sandbox):
    body = _words(400)
    _meeting("Cabinet 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("Cabinet Redo 05012026", "b.m4a",
             text=body + " one closing remark that only this pass caught",
             dur=605.0)
    _meeting("Budget 05022026", "c.m4a", text=_words(400, "z"), dur=600.0)

    pairs = dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert [(p["a"], p["b"]) for p in pairs] == \
        [("Cabinet 05012026", "Cabinet Redo 05012026")]
    assert pairs[0]["score"] > 0.9


def test_a_short_clip_inside_a_long_meeting_is_not_a_duplicate(sandbox):
    """Same words, wildly different lengths: an excerpt is not a duplicate of
    the meeting it came from, and the duration gate is what says so."""
    body = _words(400)
    _meeting("Long Session 05012026", "a.m4a", text=body, dur=3600.0)
    _meeting("Clip 05012026", "b.m4a", text=body, dur=120.0)
    assert dupes.similar_meetings(dest_dir=config.meetings_dir()) == []


def test_keeping_both_stops_a_pair_from_being_offered_again(sandbox):
    body = _words(400)
    _meeting("Standup 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("Standup 05022026", "b.m4a", text=body, dur=600.0)
    assert len(dupes.similar_meetings(dest_dir=config.meetings_dir())) == 1

    assert dupes.ignore_pair("Standup 05022026", "Standup 05012026") is True
    # order-independent: the key is the sorted pair, not the click order
    assert dupes.ignored_pairs() == {("Standup 05012026", "Standup 05022026")}
    assert dupes.similar_meetings(dest_dir=config.meetings_dir()) == []
    assert dupes.cached_pairs(config.meetings_dir()) == []
    # nothing was deleted or hidden: both meetings are still in the library
    assert len(config.meeting_bases()) == 2


def test_keep_both_hides_the_pair_from_a_stale_cache_immediately(sandbox):
    """/api/dupe_ignore never triggers a rescan (ignoring a pair doesn't change
    library_signature, so the background worker has nothing to wake it up for)
    -- cached_pairs() is what the poll reads, and it must drop an ignored pair
    on its OWN, straight out of a cache that still has the old answer sitting
    in it. Without this, "Keep both" would look like it worked and the pair
    would reappear on the very next 2s poll, until some unrelated meeting
    change happened to force a real recompute."""
    body = _words(400)
    _meeting("Standup 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("Standup 05022026", "b.m4a", text=body, dur=600.0)
    dupes.similar_meetings(dest_dir=config.meetings_dir())   # populates the cache
    assert len(dupes.cached_pairs(config.meetings_dir())) == 1

    assert dupes.ignore_pair("Standup 05012026", "Standup 05022026") is True
    # the cache on disk was NOT recomputed -- this is the read path alone
    assert dupes.cached_pairs(config.meetings_dir()) == []


def test_the_cache_follows_an_edited_transcript(sandbox):
    body = _words(400)
    _meeting("A Mtg 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("B Mtg 05012026", "b.m4a", text=body, dur=600.0)
    assert len(dupes.similar_meetings(dest_dir=config.meetings_dir())) == 1
    assert dupes.cache_is_current(config.meetings_dir()) is True

    # one of them is rewritten into something else entirely (a Redo with a
    # different engine, a bulk edit): the stale sketch must not survive it
    j = mfile("B Mtg 05012026", ".json")
    d = json.loads(j.read_text())
    d["segments"] = [{"text": _words(400, "q")}]
    j.write_text(json.dumps(d))
    import os
    os.utime(j, (j.stat().st_atime + 10, j.stat().st_mtime + 10))
    assert dupes.cache_is_current(config.meetings_dir()) is False
    assert dupes.similar_meetings(dest_dir=config.meetings_dir()) == []


def test_similarity_is_an_honest_jaccard_estimate():
    a = dupes.sketch(_words(600))
    b = dupes.sketch(_words(600))
    c = dupes.sketch(_words(600, "z"))
    assert a and len(a) <= dupes.SKETCH_K
    assert dupes.similarity(a, b) == 1.0
    assert dupes.similarity(a, c) == 0.0
    assert dupes.similarity(a, []) == 0.0
    # a text too short to shingle produces no sketch rather than a bad one
    assert dupes.sketch("only three words") == []


# ------------------------------------------------------ the panel surfaces --

def test_the_waiting_row_says_it_was_already_processed(sandbox):
    audio = b"RIFF" + b"\x07" * 9000
    _meeting("Board Prep 05012026", "board prep.m4a", audio, text=_words(50))
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "board prep 2.m4a").write_bytes(audio)

    st = srv.gather_state()
    q = [f for f in st["queue"] if f["name"] == "board prep 2.m4a"][0]
    assert q["dup_of"] == "Board Prep 05012026"
    assert q["dup_reason"] == "identical"
    assert q["dup_title"]
    row = [r for r in st["timeline"] if r["id"] == "src:board prep 2.m4a"][0]
    assert row["state"] == "waiting"        # still a queue row, just flagged
    assert row["dup_of"] == "Board Prep 05012026"
    tray = [t for t in st["tray"] if t["kind"] == "dupe_files"]
    assert len(tray) == 1 and tray[0]["count"] == 1 and tray[0]["exact"] == 1


def test_the_tray_offers_the_duplicate_transcript_review(sandbox):
    body = _words(400)
    _meeting("Cabinet 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("Cabinet Redo 05012026", "b.m4a", text=body, dur=600.0)
    srv._dupe_refresh()                     # what the background worker runs
    tray = [t for t in srv.gather_state()["tray"] if t["kind"] == "dupe_meetings"]
    assert len(tray) == 1 and tray[0]["count"] == 1
    rows = srv._dupe_pair_rows()
    assert len(rows) == 1
    assert {rows[0]["a"]["base"], rows[0]["b"]["base"]} == \
        {"Cabinet 05012026", "Cabinet Redo 05012026"}
    # both sides carry what a human needs to tell them apart
    for side in (rows[0]["a"], rows[0]["b"]):
        assert side["title"] and side["date"] and "minutes" in side


def test_an_empty_library_never_scans_or_writes_a_cache(sandbox):
    srv.gather_state()
    assert not dupes.cache_path().exists(), \
        "a poll with nothing to compare must not start a scan"

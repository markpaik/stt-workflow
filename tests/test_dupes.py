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


# ------------------------------------------------- review-fix regressions --

def test_fingerprint_hashes_the_tail_of_a_mid_size_file(sandbox):
    """Files between one and two chunks used to have everything past the first
    chunk EXCLUDED from the hash: two same-size files sharing their first
    megabyte read as byte-identical, and "identical" is the signal that gates
    the no-undo bulk delete."""
    pad = dupes.CHUNK // 2
    a = sandbox / "a.m4a"
    a.write_bytes(b"\x01" * dupes.CHUNK + b"A" * pad)
    b = sandbox / "b.m4a"
    b.write_bytes(b"\x01" * dupes.CHUNK + b"B" * pad)
    dupes._fp_cache.clear()
    assert a.stat().st_size == b.stat().st_size
    assert dupes.fingerprint(a) != dupes.fingerprint(b)


def test_a_deleted_meetings_record_matches_nothing(sandbox):
    """The user deleted that transcript, and delete_meeting promises the source
    will be re-transcribed -- so its manifest record must not flag the file,
    and above all must not hand the bulk sweep the only remaining copy. A base
    that still exists (live or archived) keeps matching: an archived meeting is
    restorable, so re-transcribing it is exactly the waste the gate prevents."""
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    f = src / "cabinet.m4a"
    f.write_bytes(b"RIFF" + b"\x02" * 9000)
    fp = dupes.fingerprint(f)
    m = {"processed": {"cabinet.m4a": {
        "mtime": 1.0, "fp": fp, "size": f.stat().st_size,
        "outputs": [str(mfile("Cabinet 05012026", ".json"))]}}}
    # the base is dead: neither signal (fp, name) may fire
    assert dupes.source_duplicates([f], [], m, config.meetings_dir(),
                                   live=set()) == {}
    # the same record with the base alive flags normally
    out = dupes.source_duplicates([f], [], m, config.meetings_dir(),
                                  live={"Cabinet 05012026"})
    assert out["cabinet.m4a"]["reason"] == "identical"


def test_delete_meeting_scrubs_its_manifest_record(sandbox):
    """The record IS the meeting's processing history; with the meeting gone it
    must go too, or its stored fingerprint keeps flagging (and the sweep keeps
    offering to delete) a source the user is entitled to re-process."""
    from stt import archive
    _meeting("Standup 05012026", "standup.m4a", b"AUD" * 100)
    m = manifest.load()
    manifest.mark(m, "standup.m4a", 5.0,
                  [str(mfile("Standup 05012026", ".json"))],
                  fp="deadbeef", size=300)
    manifest.save(m)
    assert archive.delete_meeting("Standup 05012026")["ok"]
    assert "standup.m4a" not in manifest.load()["processed"]


def test_a_truncated_scan_admits_it_and_checks_closest_lengths_first(sandbox, monkeypatch):
    """MAX_PAIRS exhausted used to stop comparing silently and stamp the cache
    as the authoritative answer. Now the budget is spent on the closest-length
    pairs first (same recording means same length), the cache says truncated --
    and the sig is STILL stamped, or the idle poll would re-kick the identical
    truncated scan forever."""
    body = _words(400)
    _meeting("A Mtg 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("B Mtg 05012026", "b.m4a", text=body, dur=601.0)   # closest pair
    _meeting("C Mtg 05012026", "c.m4a", text=_words(400, "c"), dur=680.0)
    monkeypatch.setattr(dupes, "MAX_PAIRS", 1)
    pairs = dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert dupes.cache_truncated() is True
    assert dupes.cache_is_current(config.meetings_dir()) is True
    assert {(p["a"], p["b"]) for p in pairs} == \
        {("A Mtg 05012026", "B Mtg 05012026")}, \
        "the one comparison in the budget went to the closest-length pair"
    # a full re-run clears the flag
    monkeypatch.setattr(dupes, "MAX_PAIRS", 20000)
    dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert dupes.cache_truncated() is False


def test_cache_writes_use_unique_ignored_tmp_names(sandbox, monkeypatch):
    """Two concurrent writers used to share ONE fixed .json.tmp path, so one
    could promote the other's half-written file. The unique names must keep the
    .json.tmp suffix: .gitignore matches *.json.tmp, and these files carry
    meeting names."""
    import fnmatch
    import os as _os
    from pathlib import Path as _P
    seen = []
    real = _os.replace

    def spy(a, b):
        seen.append(str(a))
        return real(a, b)
    monkeypatch.setattr(dupes.os, "replace", spy)
    dupes._save_cache({"sketches": {}, "pairs": [], "sig": "s"})
    dupes.ignore_pair("A 05012026", "B 05012026")
    assert len(seen) == 2
    for tmp in seen:
        assert fnmatch.fnmatch(_P(tmp).name, "*.json.tmp")
        assert _P(tmp).name not in ("dupes_cache.json.tmp",
                                    "dupes_ignored.json.tmp"), \
            "the tmp name must be unique per writer, never one shared path"
    assert not list(config.PROJECT_DIR.glob("*.json.tmp")), "nothing left behind"


def test_concurrent_keep_both_clicks_lose_nothing(sandbox):
    """ignore_pair is a read-modify-write; two in-flight "Keep both" clicks
    used to race it and silently drop one decision. The lock makes both land."""
    import threading as _t
    pairs = [("A 05012026", "B 05012026"), ("C 05012026", "D 05012026")]
    barrier = _t.Barrier(2)

    def go(p):
        barrier.wait()
        dupes.ignore_pair(*p)
    ts = [_t.Thread(target=go, args=(p,)) for p in pairs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert {tuple(sorted(p)) for p in pairs} <= dupes.ignored_pairs()


def test_unknown_durations_never_outrank_a_real_close_pair(sandbox, monkeypatch):
    """A meeting with no recorded duration scored 0.0 closeness, the best
    possible, so zero-duration noise pairs could eat the whole comparison
    budget ahead of the genuinely closest pair. Unknown now sorts last."""
    body = _words(400)
    _meeting("A Mtg 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("B Mtg 05012026", "b.m4a", text=body, dur=601.0)     # the real pair
    _meeting("X Mtg 05012026", "x.m4a", text=_words(400, "x"), dur=0.0)
    _meeting("Y Mtg 05012026", "y.m4a", text=_words(400, "y"), dur=0.0)
    monkeypatch.setattr(dupes, "MAX_PAIRS", 1)
    pairs = dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert {(p["a"], p["b"]) for p in pairs} == \
        {("A Mtg 05012026", "B Mtg 05012026")}, \
        "the one budgeted comparison must go to the known-close pair"


# ------------------------------------------------- batch-1 regressions -----

def test_fingerprint_hashes_the_middle_as_well_as_the_head_and_tail(sandbox):
    """W2: head + tail alone left every byte between them OUT of the hash, so
    two files above 2*CHUNK that differ only in the middle read as identical --
    and this hash is the "byte-identical" proof that gates a bulk delete with
    no undo. The cut-off sat exactly at 2*CHUNK+1 bytes."""
    n = dupes.CHUNK
    a, b = sandbox / "a.m4a", sandbox / "b.m4a"
    a.write_bytes(b"H" * n + b"\x00" * n + b"T" * n)
    b.write_bytes(b"H" * n + b"\xff" * n + b"T" * n)   # same size, head, tail
    dupes._fp_cache.clear()
    assert dupes.fingerprint(a) != dupes.fingerprint(b)
    # a real copy still matches: the middle read is additive, never selective
    c = sandbox / "c.m4a"
    c.write_bytes(a.read_bytes())
    assert dupes.fingerprint(a) == dupes.fingerprint(c)


def test_a_short_clip_is_length_gated_even_with_no_duration_recorded(sandbox):
    """W28: the duration gate only fired when BOTH sides reported a positive
    duration. A transcript with no duration_sec (a known pre-migration state)
    skipped the gate entirely, and bottom-k Jaccard reads a contained subset as
    a full 100 percent match -- so a clip paired with an hour-long meeting."""
    line = "the board approved the budget for the coming fiscal year "
    _meeting("All Hands 05012026", "all hands.m4a", text=line * 200, dur=3600.0)
    # no duration_sec at all, and a tiny fraction of the words
    mfile("Clip 05012026", ".json").write_text(json.dumps(
        {"source_file": "clip.m4a", "speakers": [],
         "segments": [{"text": line * 2}], "words": []}))
    mfile("Clip 05012026", ".txt").write_text("stub")

    pairs = dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert pairs == [], "an hour of audio and a two-line clip are not one recording"


def test_a_kept_pair_follows_a_date_correction(sandbox):
    """W20: a kept pair is stored as two literal base names, and a date
    correction re-stamps one of them through the ordinary edit path. Without
    the rename hook the next scan offers the same two transcripts again as a
    fresh, unreviewed 100 percent match."""
    from stt import summarize
    body = _words(400)
    _meeting("Budget Review 05012026", "a.m4a", text=body, dur=600.0)
    _meeting("Budget Review Copy 05012026", "b.m4a", text=body, dur=600.0)

    pairs = dupes.similar_meetings(dest_dir=config.meetings_dir())
    assert len(pairs) == 1 and pairs[0]["score"] == 1.0
    assert dupes.ignore_pair(pairs[0]["a"], pairs[0]["b"])
    assert dupes.similar_meetings(dest_dir=config.meetings_dir()) == []

    r = summarize.set_meeting_date("Budget Review Copy 05012026", "2026-06-15")
    assert r["ok"] and r["base"] == "Budget Review Copy 06152026"
    assert dupes.similar_meetings(dest_dir=config.meetings_dir()) == [], \
        "the 'keep both' decision must follow the meeting's new name"


def test_a_cold_cache_is_distinguishable_from_a_scanned_empty_one(sandbox):
    """R8: a never-scanned cache and a scanned-and-empty library both read
    pairs == [], so the drawer asserted "No duplicate transcripts found" about
    a comparison that never ran. The scan is idle-only, so a long batch can
    keep it from ever running. `sig` is written by similar_meetings and by
    nothing else, so its presence IS the record of a completed scan."""
    assert dupes.cache_scanned() is False
    assert dupes.cached_pairs() == []

    # two meetings that share nothing: a real scan, a real empty answer
    mfile("A Mtg 05012026", ".txt").write_text("apples pears plums quinces")
    mfile("A Mtg 05012026", ".json").write_text(json.dumps(
        {"duration_sec": 600.0, "words": [{"w": 1}] * 40}))
    mfile("B Mtg 05022026", ".txt").write_text("bicycles trains ferries buses")
    mfile("B Mtg 05022026", ".json").write_text(json.dumps(
        {"duration_sec": 600.0, "words": [{"w": 1}] * 40}))
    assert dupes.similar_meetings() == []
    assert dupes.cache_scanned() is True, \
        "a completed scan must be distinguishable from a cold cache"


def test_the_dupes_endpoint_publishes_whether_a_scan_has_run(sandbox):
    """R8, the wire half: the drawer cannot tell the two empties apart without
    this flag, so it rides both /api/dupes and /api/dupe_scan."""
    import gui.server as _srv
    assert dupes.cache_scanned() is False
    dupes.similar_meetings()          # writes a signature, finds nothing
    assert dupes.cache_scanned() is True
    src = (_srv.Path(_srv.__file__).resolve().parent / "server.py").read_text()
    assert '"scanned": dupes.cache_scanned()' in src

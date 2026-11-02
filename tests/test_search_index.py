"""The FTS5 search index behind /api/search (stt/search.py).

What these lock down:
  - 20 known-answer searches, each answered by the index alone
  - the result shape the page reads is the same on both paths
  - search.db is mode 600 and never exists with wider bits
  - a rebuild from nothing gives the same answers as the incremental index
  - the substring scan answers when the index finds nothing or fails
  - a delete, rename, archive or restore updates the index at once, and an
    edit made behind the index's back is picked up by the next query
All data is synthetic.
"""
import json
import os
import sqlite3
import stat

import pytest

from stt import archive, config, search, summarize
from conftest import mfile


def _mk(base, texts, mtime=None, **extra):
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0,
         "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"}],
         "segments": [{"start": float(i * 10), "end": float(i * 10 + 9),
                       "speaker": "SPEAKER_00", "display": "Speaker 1",
                       "text": t, "flags": []} for i, t in enumerate(texts)],
         "words": []}
    d.update(extra)
    j = mfile(base, ".json")
    j.write_text(json.dumps(d))
    if mtime is not None:
        os.utime(j, (mtime, mtime))
    return j


FILLER = ["Thanks everyone for joining today.", "We will follow up next week."]

# (meeting, its one distinctive line, the query that must find it). The
# queries cover exact phrases, stems, a last-word prefix, case, accents and
# punctuation, so each one exercises the FTS path rather than the scan.
KNOWN = [
    ("Budget Review 01052026", "The capital reserve forecast drops in March.", "capital reserve"),
    ("Bus Routes 01062026", "Route seventeen loses two stops after the bridge closes.", "route seventeen"),
    ("Library Hours 01072026", "Weekend library hours expand in the spring term.", "library hours"),
    ("Lunch Menu 01082026", "The café adds a vegetarian option on Thursdays.", "cafe adds"),
    ("Roof Repair 01092026", "Contractors replaced the gymnasium skylights.", "gymnasium skylight"),
    ("Hiring Plan 01122026", "We are interviewing three paraprofessional candidates.", "interviewed three"),
    ("Data Retention 01132026", "Archived rosters older than seven years are shredded.", "rosters older"),
    ("Field Trip 01142026", "The planetarium visit needs forty permission slips.", "planetar"),
    ("Science Fair 01152026", "Judges score each poster on a twelve point rubric.", "twelve point rubric"),
    ("Snow Day 01162026", "Plows clear the north lot before six.", "NORTH LOT"),
    ("Chess Club 01202026", "The chess club wants a second tournament board.", "tournament boards"),
    ("Grant Review 01212026", "The literacy grant requires quarterly reporting.", "quarterly report"),
    ("Parent Night 01222026", "Translation headsets are booked for parent night.", "translation headsets"),
    ("Device Refresh 01232026", "Chromebook carts arrive in the second week.", "chromebook cart"),
    ("Safety Drill 01262026", "The lockdown drill ran four minutes long.", "lockdown drills"),
    ("Garden Project 01272026", "Raised beds go behind the cafeteria.", "raised bed"),
    ("Attendance Huddle 01282026", "Chronic absence fell after the mentor calls.", "chronic absences"),
    ("Music Program 01292026", "The orchestra needs replacement cello strings.", "cello strings"),
    ("Facilities 01302026", "The boiler inspection passed on the first try.", "boiler inspection?"),
    ("Transport Survey 02022026", "Survey responses favored the later bell time.", "later bell time"),
]


@pytest.fixture
def corpus(sandbox):
    for i, (base, line, _) in enumerate(KNOWN):
        _mk(base, FILLER[:1] + [line] + FILLER[1:], mtime=1_700_000_000 + i * 100)
    # distractors: the words of a query, but never as the phrase
    _mk("Budget Overview 12012025", ["The capital plan is due.", "Our reserve is thin."],
        mtime=1_600_000_000)
    _mk("Bus Survey 12022025", ["Which route works?", "Seventeen people replied."],
        mtime=1_600_000_100)
    return sandbox


@pytest.mark.parametrize("base,line,q", KNOWN, ids=[k[0] for k in KNOWN])
def test_known_answer_searches(corpus, monkeypatch, base, line, q):
    def no_scan(*a, **k):
        raise AssertionError("the index must answer this query, not the scan")
    monkeypatch.setattr(search, "_scan", no_scan)
    r = search.query(q)
    assert r["query"] == q.strip().lower()
    assert r["total"] == 1, r
    hit = r["hits"][0]
    assert hit == {"base": base, "index": 1, "start": 10.0, "who": "Speaker 1",
                   "snippet": line}


def test_shape_matches_the_scan(corpus):
    """For a plain substring the two paths agree hit for hit."""
    _mk("Mtg A", ["panorama survey today", "more panorama talk " + "x" * 200],
        mtime=1_800_000_000)
    _mk("Mtg B", ["the panorama results"], mtime=1_800_000_100)
    fts = search.query("panorama")
    assert fts == search._scan("panorama")
    assert [h["base"] for h in fts["hits"]] == ["Mtg B", "Mtg A", "Mtg A"]  # newest first
    assert fts["hits"][2]["snippet"].endswith("…")


def test_limit_caps_hits_not_total(corpus):
    _mk("Long Mtg", ["recurring phrase here"] * 60)
    r = search.query("recurring phrase")
    assert r["total"] == 60 and len(r["hits"]) == 40


def test_search_db_is_mode_600(corpus):
    old = os.umask(0o022)
    try:
        search.query("capital reserve")
    finally:
        os.umask(old)
    p = config.SEARCH_DB
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    for f in search._companions(p):
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_wider_mode_is_tightened(corpus):
    search.sync()
    os.chmod(config.SEARCH_DB, 0o644)
    search.query("capital reserve")
    assert stat.S_IMODE(config.SEARCH_DB.stat().st_mode) == 0o600


def test_rebuild_from_scratch_matches_incremental(corpus):
    before = {q: search.query(q) for _, _, q in KNOWN}
    config.SEARCH_DB.write_bytes(b"not a database")
    r = search.rebuild()
    assert r["meetings"] == len(KNOWN) + 2
    assert r["lines"] == 3 * len(KNOWN) + 4
    assert stat.S_IMODE(config.SEARCH_DB.stat().st_mode) == 0o600
    assert {q: search.query(q) for _, _, q in KNOWN} == before


def test_cli_rebuild(corpus, capsys):
    assert search.main(["--rebuild"]) == 0
    assert f"indexed {len(KNOWN) + 2} meetings" in capsys.readouterr().out


# ---------- the fallback scan ----------

def test_mid_word_falls_back_to_scan(corpus):
    r = search.query("tarium")          # inside "planetarium": no token starts there
    assert r["total"] == 1 and r["hits"][0]["base"] == "Field Trip 01142026"


def test_no_word_characters_falls_back(sandbox):
    _mk("Mtg A", ["costs went up ??? again"])
    r = search.query("???")
    assert r["total"] == 1 and r["hits"][0]["snippet"] == "costs went up ??? again"


def test_sqlite_failure_falls_back(corpus, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("no such module: fts5")
    monkeypatch.setattr(search, "_open", broken)
    r = search.query("capital reserve")
    assert r["total"] == 1 and r["hits"][0]["base"] == "Budget Review 01052026"


def test_corrupt_index_falls_back_then_rebuilds(corpus):
    config.SEARCH_DB.write_bytes(b"x" * 4096)
    r = search.query("capital reserve")
    assert r["total"] == 1                      # the scan answered
    assert not config.SEARCH_DB.exists()        # the bad file is gone
    search.query("capital reserve")             # the next query builds it again
    assert _indexed() == {b for b, _, _ in KNOWN} | {
        "Budget Overview 12012025", "Bus Survey 12022025"}


def test_short_query_touches_nothing(sandbox):
    assert search.query("ab") == {"query": "ab", "hits": [], "total": 0}
    assert not config.SEARCH_DB.exists()


# ---------- keeping the index right ----------

def _indexed():
    """The bases the index holds, read straight from the file, so the read
    itself cannot sync."""
    c = sqlite3.connect(str(config.SEARCH_DB))
    try:
        return {b for (b,) in c.execute("SELECT base FROM files")}
    finally:
        c.close()


def test_delete_drops_rows_at_once(corpus):
    search.sync()
    assert archive.delete_meeting("Snow Day 01162026")["ok"]
    assert "Snow Day 01162026" not in _indexed()
    assert search.query("north lot")["total"] == 0


def test_rename_moves_rows_at_once(corpus):
    search.sync()
    r = summarize.rename_meeting("Chess Club 01202026", "Strategy Games")
    assert r["ok"]
    new = r["base"]
    assert new != "Chess Club 01202026"
    held = _indexed()
    assert new in held and "Chess Club 01202026" not in held
    assert search.query("tournament board")["hits"][0]["base"] == new


def test_archive_and_restore(corpus):
    search.sync()
    assert archive.archive_meeting("Garden Project 01272026")["ok"]
    assert "Garden Project 01272026" not in _indexed()
    assert search.query("raised beds")["total"] == 0
    assert archive.restore_meeting("Garden Project 01272026")["ok"]
    assert "Garden Project 01272026" in _indexed()
    assert search.query("raised beds")["total"] == 1


def test_changes_behind_the_index_are_picked_up(corpus):
    search.sync()
    # an outside edit (a relabel, a review, another process)
    _mk("Music Program 01292026", ["The band needs new drum heads."], mtime=1_900_000_000)
    assert search.query("cello strings")["total"] == 0
    assert search.query("drum heads")["hits"][0]["base"] == "Music Program 01292026"
    # an outside removal, with no hook to call
    import shutil
    shutil.rmtree(config.meeting_dir("Facilities 01302026"))
    assert search.query("boiler inspection")["total"] == 0
    assert "Facilities 01302026" not in _indexed()


def test_touch_never_creates_the_index(corpus):
    search.touch("Budget Review 01052026")
    assert not config.SEARCH_DB.exists()
    assert archive.delete_meeting("Snow Day 01162026")["ok"]
    assert not config.SEARCH_DB.exists()


def test_a_moved_store_empties_the_index(corpus, tmp_path, monkeypatch):
    search.sync()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(config, "MEETINGS_DIR", other)
    assert search.query("capital reserve")["total"] == 0
    assert _indexed() == set()


def test_unreadable_json_is_retried(corpus):
    j = config.meeting_file("Lunch Menu 01082026", ".json")
    good = j.read_text()
    j.write_text("{ half written")
    search.sync()
    assert "Lunch Menu 01082026" not in _indexed()
    j.write_text(good)
    assert search.query("cafe adds")["hits"][0]["base"] == "Lunch Menu 01082026"


# ---------- titles and summaries ----------

def test_meetings_matches_title_and_summary(sandbox):
    _mk("Ops Sync 03032026", ["nothing relevant"], title="Ops Sync",
        ai_summary="The team agreed on a new substitute pool.",
        ai_next_steps=["Draft the onboarding checklist"])
    _mk("Other 03042026", ["also nothing"], title="Other")
    assert search.meetings("substitute pool") == [
        {"base": "Ops Sync 03032026", "title": "Ops Sync"}]
    assert search.meetings("onboarding checklists")[0]["base"] == "Ops Sync 03032026"
    assert search.meetings("ops sync")[0]["base"] == "Ops Sync 03032026"
    # a summary match is not a transcript line, so /api/search does not list it
    assert search.query("substitute pool")["total"] == 0


# ---------- type-ahead, malformed jsons, short last words ----------

def test_part_typed_word_finds_the_whole_word(sandbox, monkeypatch):
    """Porter stems a part-typed word, so "attendan" alone would only find
    words that keep those letters after stemming. The unstemmed table answers
    the prefix, so both lines come back while the user is still typing."""
    _mk("Mtg A 03052026", ["Daily attendance is up this month."], mtime=1_800_000_000)
    _mk("Mtg B 03062026", ["The attendant opens the lot at seven."], mtime=1_800_000_100)
    _mk("Mtg C 03092026", ["Meeting notes go out on Friday."], mtime=1_800_000_200)
    _mk("Mtg D 03102026", ["The old meetinghouse is for sale."], mtime=1_800_000_300)

    def no_scan(*a, **k):
        raise AssertionError("the index must answer this query, not the scan")
    monkeypatch.setattr(search, "_scan", no_scan)
    r = search.query("attendan")
    assert r["total"] == 2
    assert [h["base"] for h in r["hits"]] == ["Mtg B 03062026", "Mtg A 03052026"]
    r = search.query("meetin")
    assert {h["base"] for h in r["hits"]} == {"Mtg C 03092026", "Mtg D 03102026"}
    # a whole word still finds its other forms through the stemmed table
    assert "Mtg C 03092026" in {h["base"] for h in search.query("meetings")["hits"]}


def test_short_last_word_is_not_a_prefix(sandbox):
    _mk("Mtg A 03052026", ["The air handler failed again."], mtime=1_800_000_000)
    _mk("Mtg B 03062026", ["We tried the AI tutor pilot."], mtime=1_800_000_100)
    _mk("Mtg C 03092026", ["Most code here is c++ and some is cobol."],
        mtime=1_800_000_200)
    r = search.query("ai?")
    assert [h["base"] for h in r["hits"]] == ["Mtg B 03062026"]
    r = search.query("c++")
    assert [h["base"] for h in r["hits"]] == ["Mtg C 03092026"]
    assert r["total"] == 1
    # three letters or more still match as a prefix
    assert search.query("han")["total"] == 1


@pytest.mark.parametrize("bad", [
    {"segments": [{"start": 0.0, "speaker": "SPEAKER_00", "text": 12345}]},
    {"speakers": "not a list"},
    {"segments": [{"start": 0.0, "speaker": ["SPEAKER_00"], "text": "hello there"}]},
    ["not", "an", "object"],
], ids=["int-text", "str-speakers", "list-speaker", "list-root"])
def test_malformed_json_does_not_disable_the_index(sandbox, bad):
    _mk("Good 03052026", ["The capital reserve forecast drops."], mtime=1_800_000_000)
    j = mfile("Bad 03062026", ".json")
    if isinstance(bad, dict):
        d = {"speakers": [], "segments": []}
        d.update(bad)
        bad = d
    j.write_text(json.dumps(bad))
    r = search.rebuild()
    assert r["meetings"] == 2                       # both recorded, the bad one empty
    assert _indexed() == {"Good 03052026", "Bad 03062026"}
    q = search.query("capital reserve")
    assert q["total"] == 1 and q["hits"][0]["base"] == "Good 03052026"
    # the bad file is not read again until it changes
    calls = []
    real = search._read
    search._read = lambda *a: calls.append(a) or real(*a)
    try:
        search.query("capital reserve")
    finally:
        search._read = real
    assert calls == []

"""Needs you (unified plan, Phase 1 step 8, rows 10, 11 and 12).

One short list, computed on read: meetings that arrived in the last 14
days by their STORED processed_at, at most 5 rows, no older-count line, and
one voice row ranked by unnamed talk in non-public meetings. Flags never
appear. Every name is synthetic."""
import json
from datetime import datetime, timedelta

from gui import server as srv
from stt import config, queue_order
from conftest import mfile

NOW = datetime.now()


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _m(base, *, days_ago=1, speakers=None, segments=None, **extra):
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0,
         "date": (NOW - timedelta(days=days_ago)).date().isoformat(),
         "processed_at": _iso(days_ago),
         "speakers": speakers or [{"id": "SPEAKER_00", "display": "Avery Sample",
                                   "name": "Avery Sample"}],
         "segments": segments if segments is not None else [
             {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "hello"}],
         "words": []}
    d.update(extra)
    if d.get("processed_at") is None:
        d.pop("processed_at")
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    return d


def _voice(base, display, secs, **extra):
    return _m(base, speakers=[{"id": "SPEAKER_00", "display": display}],
              segments=[{"start": 0.0, "end": secs, "speaker": "SPEAKER_00",
                         "text": "words"}], **extra)


def _ny():
    return srv.gather_state()["needs_you"]


def test_twenty_items_show_five(sandbox):
    for i in range(20):
        _m(f"Recording 1001202{i % 10} {1000 + i}", days_ago=1 + i % 10,
           reviewed=False, title_state="needs_title", ai_title=f"Draft {i}")
    items = _ny()
    assert len(items) == 5
    assert {x["kind"] for x in items} == {"needs_title"}
    assert not any("older" in json.dumps(x) for x in items)


def test_the_window_reads_the_arrival_not_the_recording_date(sandbox):
    # recorded 21 days ago, transferred and processed today: it appears
    _m("New Recording 1", days_ago=0, date=(NOW - timedelta(days=21)).date().isoformat(),
       reviewed=False, title_state="needs_title")
    # processed 15 days ago: outside the window, still recorded on the meeting
    _m("New Recording 2", days_ago=15, reviewed=False, title_state="needs_title")
    assert [x["target"] for x in _ny()] == ["New Recording 1"]


def test_a_meeting_without_a_stored_processed_at_never_appears(sandbox):
    # a backfill write moves the file mtime and generated_at, never this key
    _m("New Recording 3", processed_at=None, reviewed=False, title_state="needs_title",
       generated_at=NOW.isoformat(timespec="seconds"))
    assert _ny() == []


def test_a_bulk_redo_does_not_refill_the_list(sandbox):
    """A Redo re-stamps processed_at. Meetings whose titles are settled (a
    provisional or confirmed state, or reviewed before the rule) stay out
    however fresh the stamp: only a missing title can put a meeting here."""
    _m("Budget Review 09012026", days_ago=0, title_state="provisional",
       title_source="label", reviewed=False)
    _m("Team Sync 09022026", days_ago=0, title_state="confirmed", reviewed=True)
    _m("Old Planning 09032026", days_ago=0, reviewed=True)
    _m("Board Session 09042026", days_ago=0, title_state="provisional",
       title_source="public_download", route="public_download", reviewed=False)
    assert _ny() == []


def test_flags_and_short_or_public_voices_never_appear(sandbox):
    _m("Flagged 10012026", segments=[{"start": 0.0, "end": 9.0, "speaker": "SPEAKER_00",
                                      "text": "a b c d e", "flags": ["low_conf"]}])
    _voice("Short Voice 10012026", "Speaker 9", 30.0)
    _voice("Board Session 10012026", "Speaker 8", 3600.0, route="public_download")
    assert _ny() == []


def test_the_reasons_rank_silent_then_short_audio_then_title_then_voice(sandbox, monkeypatch):
    _m("Quiet Room 10012026", days_ago=3, segments=[], no_speech=True)
    _m("Cut Short 10012026", days_ago=2)
    _m("New Recording 4", days_ago=1, reviewed=False, title_state="needs_title",
       ai_title="Hiring Sync")
    _voice("Team Sync 10012026", "Speaker 20", 600.0)
    real = queue_order.duration

    def fake(p):
        return 300.0 if "Cut Short" in str(p) else real(p)
    monkeypatch.setattr(queue_order, "duration", fake)
    items = _ny()
    assert [x["kind"] for x in items] == ["no_speech", "duration_mismatch",
                                          "needs_title", "voice"]
    assert items[0]["detail"] == "No speech found in this recording"
    assert items[1]["detail"].startswith("The audio is 5 minutes shorter")
    assert items[2]["detail"] == "Draft title: Hiring Sync"
    # the rows the list holds carry their reason and place on the timeline
    st = srv.gather_state()
    row = next(r for r in st["timeline"] if r["id"] == "New Recording 4")
    assert (row["needs_you"], row["needs_you_rank"]) == ("needs_title", 2)
    other = next(r for r in st["timeline"] if r["id"] == "Team Sync 10012026")
    assert "needs_you" not in other


def test_one_voice_row_ranked_by_non_public_talk(sandbox):
    # U020 talks 5 + 8 minutes in two working meetings; U082 talks an hour in
    # a public download and 2 minutes in a working meeting; a local voice
    # talks 6 minutes in one meeting
    _voice("Team Sync 10012026", "Speaker 20", 300.0, days_ago=2)
    _voice("Hiring Sync 10022026", "Speaker 20", 480.0, days_ago=30)
    _voice("Board Session 10032026", "Speaker 82", 3600.0, route="public_download")
    _voice("Data Review 10042026", "Speaker 82", 120.0, days_ago=1)
    _voice("Vendor Call 10052026", "Voice 3", 360.0, days_ago=1)
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps({"speakers": {
        "U020": {"file": "U020.npy",
                 "meetings": ["Team Sync 10012026", "Hiring Sync 10022026"]},
        "U082": {"file": "U082.npy",
                 "meetings": ["Board Session 10032026", "Data Review 10042026"]}}}))
    items = _ny()
    voices = [x for x in items if x["kind"] == "voice"]
    assert len(voices) == 1
    v = voices[0]
    assert v["uid"] == "U020" and v["title"] == "Speaker 20"
    assert v["target"] == "Team Sync 10012026"      # the meeting inside the window
    assert v["secs"] == 780.0
    assert v["detail"].startswith("13 min of unnamed talk in 2 meetings")


def test_a_voice_heard_only_outside_the_window_is_not_offered(sandbox):
    _voice("Team Sync 09012026", "Speaker 20", 600.0, days_ago=40)
    assert _ny() == []


def test_an_active_meeting_is_left_out(sandbox, monkeypatch):
    from stt import control, status
    _m("New Recording 5", reviewed=False, title_state="needs_title")
    status.start_run(["New Recording 5.m4a"])
    status.set_stage("New Recording 5.m4a", "writing", base="New Recording 5")
    monkeypatch.setattr(control, "snapshot", lambda: {"pids": [1], "mem_mb": 0})
    assert _ny() == []

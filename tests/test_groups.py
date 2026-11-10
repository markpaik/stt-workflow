"""Computed groups and search filters (unified plan Phase 4E).

What these lock down:
  - groups form from title words, with average linkage, and never from weak
    words alone ("weekly", "check-in")
  - a meeting with a default title joins by people and hour
  - a group needs two meetings, and nothing on disk moves
  - "Not part of this group" is keyed on meeting_id, survives a rename, has
    an undo, and writes a mode-600 file
  - /api/search filters on recorded_at, date_source, and route, on both the
    index path and the scan path, and works on meetings without those keys
  - the use log carries no title and no key
All data is synthetic.
"""
import json
import os
import stat

import pytest

from stt import config, groups, search, summarize
from conftest import mfile
from test_server_security import _get, _post, running_server  # noqa: F401


def _mk(base, *, mid=None, people=(), recorded_at=None, texts=("A plain line.",),
        dur=1800.0, **extra):
    speakers = [{"id": f"SPEAKER_{i:02d}", "name": p, "display": p}
                for i, p in enumerate(people)]
    speakers.append({"id": "SPEAKER_99", "name": None, "display": "Speaker 9"})
    d = {"source_file": f"{base}.m4a", "duration_sec": dur,
         "speakers": speakers,
         "segments": [{"start": float(i * 10), "end": float(i * 10 + 8),
                       "speaker": "SPEAKER_99", "display": "Speaker 9",
                       "text": t, "flags": []} for i, t in enumerate(texts)],
         "words": []}
    if mid:
        d = {"meeting_id": mid, **d}
    if recorded_at:
        d["recorded_at"] = recorded_at
        d["date"] = recorded_at[:10]
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("\n".join(texts))


def _keys():
    return {g["key"]: g for g in groups.current()}


def _group_of(base):
    return groups.membership().get(base, (None, None))[0]


# ------------------------------------------------------------- words ---

def test_words_drop_stamps_stop_words_and_numbers():
    assert groups.words("Budget Review Meeting 10142026") == {"budget", "review"}
    assert groups.words("Team 1-on-1") == {"team", "1on1"}
    assert groups.strong({"weekly", "budget"}) == {"budget"}


# --------------------------------------------------------- formation ---

def test_spellings_of_one_series_group_and_unrelated_titles_do_not(sandbox):
    _mk("Grant Writing Sync 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    _mk("Grant Writing Sync 10152026", mid="m3")
    _mk("Bus Routes 10022026", mid="m4")
    _mk("Roof Repair 10032026", mid="m5")
    gs = groups.current()
    assert len(gs) == 1
    g = gs[0]
    assert g["count"] == 3 and g["kind"] == "title"
    assert set(g["bases"]) == {"Grant Writing Sync 10012026", "Grant Writing 10082026",
                               "Grant Writing Sync 10152026"}
    assert g["name"] == "Grant Writing Sync"
    assert "grant" in g["why"] and g["hours"] == 1.5


def test_weak_words_alone_never_join(sandbox):
    # the drift the audit warned about: two series that share only "weekly"
    _mk("Facilities Weekly 10012026")
    _mk("Literacy Weekly 10022026")
    _mk("Weekly Check-in 10032026")
    _mk("Weekly Check-in Facilities 10042026")
    keys = _keys()
    together = [g for g in keys.values()
                if {"Facilities Weekly 10012026", "Literacy Weekly 10022026"} <= set(g["bases"])]
    assert not together
    assert _group_of("Literacy Weekly 10022026") is None


def test_average_linkage_stops_a_chain(sandbox):
    # single linkage would chain A-B-C-D into one group: each neighbor pair
    # overlaps by 2 of 3 words, but A and D share nothing strong
    _mk("Alpha Beta Gamma 10012026")
    _mk("Alpha Beta Gamma 10022026")
    _mk("Beta Gamma Delta 10032026")
    _mk("Gamma Delta Epsilon 10042026")
    _mk("Gamma Delta Epsilon 10052026")
    gs = groups.current()
    assert max(g["count"] for g in gs) < 5
    a, e = _group_of("Alpha Beta Gamma 10012026"), _group_of("Gamma Delta Epsilon 10042026")
    assert a and e and a != e


def test_a_single_meeting_has_no_group_and_nothing_moves(sandbox):
    _mk("Science Fair 10012026")
    _mk("Snow Day 10022026")
    before = sorted(p.relative_to(config.meetings_dir()).as_posix()
                    for p in config.meetings_dir().rglob("*"))
    assert groups.current() == []
    after = sorted(p.relative_to(config.meetings_dir()).as_posix()
                   for p in config.meetings_dir().rglob("*"))
    assert before == after


def test_default_titles_join_by_people_and_hour(sandbox, monkeypatch):
    monkeypatch.setattr(config, "mic_speaker", lambda: "Owner Person")
    ppl = ("Owner Person", "Avery Stone")
    _mk("Caseload Planning Avery 10012026", people=ppl,
        recorded_at="2026-10-01T14:05:00-04:00")
    _mk("Caseload Planning Avery 10082026", people=ppl,
        recorded_at="2026-10-08T14:10:00-04:00")
    # a recorder default name, same people, same hour: joins that group
    _mk("Recording 10152026 1402", people=ppl, recorded_at="2026-10-15T14:02:00-04:00")
    # same people at another hour: no group
    _mk("Recording 10162026 0900", people=ppl, recorded_at="2026-10-16T09:00:00-04:00")
    # two default names with other people at one hour: a people group
    _mk("Recording 10052026 1100", people=("Owner Person", "Blake Rivera"),
        recorded_at="2026-10-05T11:00:00-04:00")
    _mk("New Recording 3", people=("Owner Person", "Blake Rivera"),
        recorded_at="2026-10-12T11:30:00-04:00")
    k = _group_of("Recording 10152026 1402")
    assert k and k == _group_of("Caseload Planning Avery 10012026")
    assert _group_of("Recording 10162026 0900") is None
    pk = _group_of("Recording 10052026 1100")
    g = _keys()[pk]
    assert g["kind"] == "people" and g["count"] == 2
    assert "11 AM" in g["why"]


def test_accented_title_words_stay_whole():
    """QA 4E #6: "Réunion" is one word, not "r" and "union"."""
    assert groups.words("Réunion Équipe Budget") == {"reunion", "equipe", "budget"}
    assert groups.words("Reunion Equipe Budget 10012026") == groups.words("Réunion Équipe Budget")


def test_the_hour_wraps_at_midnight():
    """QA 4E #7: 23:30 and 00:10 are near."""
    assert groups._near(23, 0) and groups._near(0, 23)
    assert groups._near(14, 15) and not groups._near(14, 16)
    assert not groups._near(None, 3)


def test_the_hour_falls_back_to_a_folder_or_source_stamp():
    """QA 4E #3: the live store has no recorded_at, so the people-and-hour
    rule reads the HHMM stamp in the folder name, then in source_name."""
    assert groups.row_hour({"base": "Recording 10152026 1402"}) == 14
    assert groups.row_hour({"base": "Old Name 10152026",
                            "source_name": "20261015 091500-ABCDEF12.m4a"}) == 9
    assert groups.row_hour({"base": "Recording 10152026 1402",
                            "recorded_at": "2026-10-15T08:00:00-04:00"}) == 8
    assert groups.row_hour({"base": "Budget 10152026"}) is None
    assert groups.row_hour({"base": "Recording 10152026 2675"}) is None


def test_default_titles_join_by_folder_hour_without_recorded_at(sandbox, monkeypatch):
    monkeypatch.setattr(config, "mic_speaker", lambda: "Owner Person")
    ppl = ("Owner Person", "Avery Stone")
    _mk("Recording 10012026 1405", people=ppl)
    _mk("Recording 10082026 1420", people=ppl)
    _mk("Recording 10092026 0900", people=ppl)
    k = _group_of("Recording 10012026 1405")
    assert k and k == _group_of("Recording 10082026 1420")
    assert _group_of("Recording 10092026 0900") is None
    assert "2 PM" in _keys()[k]["why"]


def test_meetings_without_amnesty_keys_still_group(sandbox):
    # no meeting_id, no recorded_at, no date_source, no route: older files
    _mk("Board Prep 09012026")
    _mk("Board Prep 09082026")
    (g,) = groups.current()
    assert g["count"] == 2


# --------------------------------------------------------- overrides ---

def test_not_part_of_this_group_with_undo(sandbox):
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    _mk("Grant Writing 10152026", mid="m3")
    key = _group_of("Grant Writing 10082026")
    r = groups.set_override("m2", key)
    assert r["ok"] and r["not_in"] == [key]
    p = groups.overrides_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text()) == {"m2": {"not_in": [key]}}
    g = _keys()[key]
    assert "Grant Writing 10082026" not in g["bases"] and g["count"] == 2
    groups.set_override("m2", key, exclude=False)
    assert json.loads(p.read_text()) == {}
    assert _keys()[key]["count"] == 3


def test_an_override_survives_a_rename_and_a_group_under_two_leaves(sandbox):
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    key = _group_of("Grant Writing 10012026")
    groups.set_override("m2", key)
    assert groups.current() == []           # one meeting left: no group
    r = summarize.rename_meeting("Grant Writing 10082026", "Grant Writing Session")
    assert r["ok"]
    # the folder moved; the override is keyed on the id, so it holds
    assert groups.current() == []


def test_purge_scope_reads_the_override_shape(sandbox):
    from tools import purge
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    key = _group_of("Grant Writing 10012026")
    groups.set_override("m2", key)
    assert purge._overrides("Grant Writing 10082026", "m2") == ["m2"]


# ---------------------------------------------------------- the API ---

def test_api_groups_and_override_and_usage(running_server):
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    _mk("Grant Writing 10152026", mid="m3")
    st, body = _get(running_server, "/api/groups")
    assert st == 200
    (g,) = body["groups"]
    assert g["count"] == 3
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": g["key"]})
    assert st == 200 and r["ok"]
    _, body = _get(running_server, "/api/groups")
    assert body["groups"][0]["count"] == 2
    st, r = _post(running_server, "/api/group_override",
                  {"base": "../../etc/passwd", "group": g["key"]})
    assert st == 400
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": g["key"], "undo": True})
    assert r["ok"] and r["not_in"] == []
    # the state rows carry the group
    _, state = _get(running_server, "/api/state")
    row = next(m for m in state["meetings"] if m["base"] == "Grant Writing 10012026")
    assert row["group"] == g["key"] and row["group_name"] == "Grant Writing"
    assert row["meeting_id"] == "m1"
    # the use count: an allow-listed event, no title, no key
    st, r = _post(running_server, "/api/usage", {"event": "group_open"})
    assert st == 200 and r["ok"]
    st, r = _post(running_server, "/api/usage", {"event": "anything else"})
    assert st == 400
    line = groups.usage_path().read_text().strip()
    assert set(json.loads(line)) == {"at", "event"}
    assert "Grant" not in line and g["key"] not in line
    assert groups.usage_counts() == {"group_open": 1, "group_ask": 0}
    assert stat.S_IMODE(groups.usage_path().stat().st_mode) == 0o600


def test_get_groups_writes_nothing(running_server):
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    before = set(os.listdir(config.PROJECT_DIR))
    _get(running_server, "/api/groups")
    after = set(os.listdir(config.PROJECT_DIR))
    assert not (after - before) - {"search.db", "search.db-wal", "search.db-shm",
                                   "search.db-journal"}


# ----------------------------------------------------------- filters ---

def _filter_world():
    _mk("Budget Talk 10012026", mid="f1", recorded_at="2026-10-01T09:00:00-04:00",
        texts=("The bond budget line moves.",), date_source="typed", route="voice_memo")
    _mk("Budget Talk 10152026", mid="f2", recorded_at="2026-10-15T09:00:00-04:00",
        texts=("The bond budget line holds.",), date_source="recorder", route="native")
    # an older meeting with none of the new keys: no recorded_at, no
    # date_source, no route; a conference export name sets its route
    _mk("GMT20260920-140000_Recording 09202026",
        texts=("The bond budget line was first proposed.",), date="2026-09-20")


@pytest.mark.parametrize("path", ["index", "scan"])
def test_search_filters_on_day_source_and_route(sandbox, monkeypatch, path):
    _filter_world()
    if path == "scan":
        monkeypatch.setattr(search, "_fts", lambda *a, **k: None)

    def bases(**f):
        r = search.query("bond budget", filters=f)
        assert "error" not in r
        return sorted({h["base"] for h in r["hits"]})
    assert len(bases()) == 3
    assert bases(after="2026-10-01") == ["Budget Talk 10012026", "Budget Talk 10152026"]
    assert bases(before="2026-10-01") == ["GMT20260920-140000_Recording 09202026"]
    assert bases(after="2026-10-02", before="2026-10-16") == ["Budget Talk 10152026"]
    assert bases(route="native") == ["Budget Talk 10152026"]
    assert bases(route="conference") == ["GMT20260920-140000_Recording 09202026"]
    assert bases(date_source="typed,recorder") == ["Budget Talk 10012026",
                                                   "Budget Talk 10152026"]
    # `none` is a meeting from before the date_source key
    assert bases(date_source="none") == ["GMT20260920-140000_Recording 09202026"]
    assert bases(**{"from": "speaker 9"}) and not bases(**{"from": "nobody"})
    r = search.query("bond budget", filters={"route": "native"})
    assert r["total"] == 1 and r["filters"] == {"route": ["native"]}


def test_inline_filters_and_bad_values(sandbox):
    _filter_world()
    r = search.query("bond budget after:2026-10-10 route:native")
    assert {h["base"] for h in r["hits"]} == {"Budget Talk 10152026"}
    assert r["query"] == "bond budget"
    r = search.query("bond budget dated:typed")
    assert {h["base"] for h in r["hits"]} == {"Budget Talk 10012026"}
    for bad in ("bond after:10/01/2026", "bond route:fax", "bond dated:guess",
                "bond before:2026-02-30"):
        r = search.query(bad)
        assert r["hits"] == [] and r["error"]


def test_group_filter_on_search(sandbox):
    _mk("Grant Writing 10012026", mid="g1", texts=("The match rate is set.",))
    _mk("Grant Writing 10082026", mid="g2", texts=("The match rate moves.",))
    _mk("Bus Routes 10022026", texts=("The match rate for buses.",))
    key = _group_of("Grant Writing 10012026")
    r = search.query("match rate", filters={"in": key})
    assert {h["base"] for h in r["hits"]} == {"Grant Writing 10012026",
                                             "Grant Writing 10082026"}
    assert search.query(f"match rate in:{key}")["total"] == 2
    assert search.query("match rate in:gnothere")["total"] == 0


def test_an_unknown_group_filter_says_so(sandbox):
    """in:<key> for no group answered total 0 with no error, while a bad
    route: or after: value said what was wrong. A typo or a stale key now
    answers with an error, as the other filters do."""
    _mk("Grant Writing 10012026", mid="g1", texts=("The match rate is set.",))
    _mk("Grant Writing 10082026", mid="g2", texts=("The match rate moves.",))
    for r in (search.query("match rate in:gnothere"),
              search.query("match rate", filters={"in": "gnothere"})):
        assert r["hits"] == [] and r["total"] == 0
        assert "gnothere" in r["error"] and "not a group" in r["error"]
    assert search.meetings("grant in:gnothere") == []
    key = _group_of("Grant Writing 10012026")
    assert "error" not in search.query(f"match rate in:{key}")


def test_api_search_passes_filters(running_server):
    _filter_world()
    st, r = _get(running_server, "/api/search?q=bond+budget&route=native")
    assert st == 200 and {h["base"] for h in r["hits"]} == {"Budget Talk 10152026"}
    st, r = _get(running_server, "/api/search?q=bond+budget&dated=none")
    assert {h["base"] for h in r["hits"]} == {"GMT20260920-140000_Recording 09202026"}
    st, r = _get(running_server, "/api/search?q=bond+budget&after=yesterday")
    assert r["error"] and r["hits"] == []
    st, r = _get(running_server, "/api/search_meetings?q=budget+talk&route=voice_memo")
    assert [m["base"] for m in r["meetings"]] == ["Budget Talk 10012026"]


def test_an_old_index_is_rebuilt_with_the_filter_columns(sandbox):
    import sqlite3
    _filter_world()
    p = search.db_path()
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE files(id INTEGER PRIMARY KEY, base TEXT UNIQUE,
                           mtime REAL, size INTEGER);""")
    conn.execute("INSERT INTO meta VALUES ('schema', '2')")
    conn.commit()
    conn.close()
    os.chmod(p, 0o600)
    r = search.query("bond budget", filters={"route": "native"})
    assert r["total"] == 1
    cols = {row[1] for row in sqlite3.connect(p).execute("PRAGMA table_info(files)")}
    assert {"day", "route", "date_source", "recorded_at", "mid"} <= cols


def test_a_grouping_failure_never_breaks_the_state_poll(running_server, monkeypatch):
    _mk("Grant Writing 10012026", mid="m1")

    def boom():
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(groups, "membership", boom)
    st, state = _get(running_server, "/api/state")
    assert st == 200
    row = next(m for m in state["meetings"] if m["base"] == "Grant Writing 10012026")
    assert "group" not in row


def test_an_override_must_name_a_real_group(running_server):
    """An invented key was stored forever: {"ok": true, "not_in": ["bogus"]},
    and it matched no group, ever. The key must be a current group's, and the
    meeting must be in it. An undo may still name the group its exclusion
    dissolved, because the stored key is proof enough."""
    _mk("Grant Writing 10012026", mid="m1")
    _mk("Grant Writing 10082026", mid="m2")
    _mk("Budget Review 10092026", mid="m3")
    (g,) = groups.current()
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": "bogus"})
    assert (st, r) == (400, {"ok": False, "error": "no such group -- refresh and try again"})
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Budget Review 10092026", "group": g["key"]})
    assert (st, r) == (400, {"ok": False, "error": "that meeting is not in this group"})
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": "bogus", "undo": True})
    assert (st, r) == (400, {"ok": False, "error": "no such group"})
    assert groups.load_overrides() == {}, "nothing invented reaches the file"
    # excluding one of two dissolves the group, and the undo still lands
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": g["key"]})
    assert st == 200 and r["ok"] and groups.current() == []
    st, r = _post(running_server, "/api/group_override",
                  {"base": "Grant Writing 10082026", "group": g["key"], "undo": True})
    assert st == 200 and r == {"ok": True, "not_in": []}
    assert [x["key"] for x in groups.current()] == [g["key"]]

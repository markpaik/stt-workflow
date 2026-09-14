"""Targeted relabel: every endpoint that changes an identity spawns the short
list of meetings the change can reach, never `relabel --all`.

Before this, naming one person walked 99 meetings at ~7s each — seven minutes,
in alphabetical order, with the meeting the user was looking at somewhere in the
middle. Each test here drives the real endpoint against a real running server
and reads the basename file the spawned command carries.
"""
import http.client
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from gui import server as srv
from stt import config, diarcache, identify, unknowns
from conftest import mfile


@pytest.fixture
def running_server(sandbox):
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    t.join(timeout=2)


@pytest.fixture
def spawned(monkeypatch):
    """Every spawned command, with the relabel subprocess itself stubbed out."""
    seen = []
    monkeypatch.setattr(srv, "_spawn", lambda cmd: seen.append([str(a) for a in cmd]))
    monkeypatch.setattr(config, "PUNCTUATE", False)
    return seen


def _post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    return r.status, body


def _unit(seed, dim=256):
    v = np.random.default_rng(seed).normal(size=dim)
    return v / np.linalg.norm(v)


def _meeting(base, cents, speakers=None, talky=True):
    """A meeting on disk with real words and a diarization cache, so relabel
    can actually rebuild it and the unknown-minting quality floor is cleared."""
    turns, words, t = [], [], 0.0
    for cid in cents:
        for _ in range(12 if talky else 1):
            turns.append({"start": t, "end": t + 3.0, "cluster": cid})
            words.append({"start": t, "end": t + 0.4, "word": "hello"})
            t += 4.0
    (mfile(base, ".json")).write_text(json.dumps({
        "source_file": f"{base}.m4a", "duration_sec": t, "strict": False,
        "speakers": speakers or [{"id": k, "name": None, "display": f"Speaker {i+1}"}
                                 for i, k in enumerate(cents)],
        "segments": [], "words": words}))
    (mfile(base, ".txt")).write_text("stub")
    diarcache.save(mfile(base, ".diar.npz"), turns, [None] * len(turns), cents)


def _bases(cmd):
    """The meeting list a spawned `relabel --bases-from FILE` carries."""
    assert "--all" not in cmd, f"a targeted pass must not spawn --all: {cmd}"
    assert cmd[:3] == [str(srv.RUN_SH), "relabel", "--bases-from"], cmd
    path = Path(cmd[3])
    assert path.parent == config.PROJECT_DIR / "logs", \
        "the target list is scratch, so it belongs under the project dir"
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


# ---------- the helper itself ----------

def test_relabel_targets_applies_the_first_meeting_here_and_spawns_the_rest(
        sandbox, spawned):
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(2)})

    res = srv._relabel_targets(vector=v, first="Bravo Mtg")
    assert res == {"applied": "Bravo Mtg", "targets": ["Alpha Mtg"]}
    assert len(spawned) == 1 and _bases(spawned[0]) == ["Alpha Mtg"]


def test_relabel_targets_spawns_the_first_meeting_when_the_lock_is_busy(
        sandbox, spawned, monkeypatch):
    """_relabel_now says False when another pass owns the relabel lock. The
    meeting must then join the spawned list, not be silently dropped."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    monkeypatch.setattr(srv, "_relabel_now", lambda base: False)

    res = srv._relabel_targets(vector=v, first="Bravo Mtg")
    assert res["applied"] is None
    assert res["targets"] == ["Bravo Mtg", "Alpha Mtg"], \
        "the meeting in the request goes FIRST in the spawned list"
    assert _bases(spawned[0]) == ["Bravo Mtg", "Alpha Mtg"]


def test_relabel_targets_spawns_nothing_when_there_is_nothing_to_do(sandbox, spawned):
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)})
    assert srv._relabel_targets(vector=_unit(99)) == {"applied": None, "targets": []}
    assert spawned == []


# ---------- one test per endpoint ----------

def test_name_applies_the_open_meeting_here_and_targets_the_others(
        running_server, spawned):
    """The whole point of the phase: the meeting being named is relabeled
    INSIDE the request, so the panel's "?" chip stops asking on the next poll,
    and only the meetings that voice appears in are spawned."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v, "SPEAKER_01": _unit(5)})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(6)})

    status, body = _post(running_server, "/api/name",
                         {"meeting": "Alpha Mtg", "speaker": "SPEAKER_00",
                          "name": "Dana Cole", "confirm": True})
    assert status == 200 and body["ok"] is True
    assert body["applied"] == "Alpha Mtg"
    assert body["targets"] == ["Bravo Mtg"]
    assert len(spawned) == 1 and _bases(spawned[0]) == ["Bravo Mtg"]
    # applied means applied: the name is in the transcript already
    names = {s.get("name") for s in json.loads(
        mfile("Alpha Mtg", ".json").read_text())["speakers"]}
    assert "Dana Cole" in names


def test_name_on_an_unknown_targets_the_meetings_that_voice_was_heard_in(
        running_server, spawned):
    v = _unit(3)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(7)})
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]

    status, body = _post(running_server, "/api/name",
                         {"uid": uid, "name": "Dana Cole", "confirm": True})
    assert status == 200 and body["ok"] is True
    assert body["applied"] is None, "a uid naming names no single meeting"
    assert body["targets"] == ["Alpha Mtg"]
    assert _bases(spawned[0]) == ["Alpha Mtg"]


def test_rename_speaker_targets_both_the_old_and_the_new_name(
        running_server, spawned):
    v = _unit(1)
    identify.enroll("Dana Cole", v, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": v},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                        "display": "Dana Cole"}])
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(2)},
             speakers=[{"id": "SPEAKER_00", "name": "D. Cole",
                        "display": "D. Cole"}])
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(3)})

    status, body = _post(running_server, "/api/rename_speaker",
                         {"name": "Dana Cole", "new": "D. Cole"})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"], \
        "the meetings carrying the OLD label and the ones already carrying the new one"
    assert _bases(spawned[0]) == ["Alpha Mtg", "Bravo Mtg"]


def test_remove_speaker_targets_only_the_meetings_carrying_that_name(
        running_server, spawned):
    identify.enroll("Dana Cole", _unit(1), source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                        "display": "Dana Cole"}])
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(3)})

    status, body = _post(running_server, "/api/remove_speaker",
                         {"name": "Dana Cole"})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg"]
    assert _bases(spawned[0]) == ["Alpha Mtg"]


def test_merge_speakers_targets_both_sides(running_server, spawned):
    """A uid merged into a name: the destination's meetings by name, and the
    retired unknown's meetings through the samples it brought with it."""
    va, vb = _unit(1), _unit(2)
    identify.enroll("Dana Cole", va, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": va},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                        "display": "Dana Cole"}])
    _meeting("Bravo Mtg", {"SPEAKER_00": vb})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(9)})
    uid = unknowns.assign({"SPEAKER_00": vb}, {"SPEAKER_00": None},
                          "Bravo Mtg")["SPEAKER_00"]

    status, body = _post(running_server, "/api/merge_speakers",
                         {"src": f"uid:{uid}", "dst": "name:Dana Cole"})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"]
    assert _bases(spawned[0]) == ["Alpha Mtg", "Bravo Mtg"]


def test_remove_sample_targets_the_remaining_profile(running_server, spawned):
    keep, drop = _unit(1), _unit(2)
    identify.enroll("Dana Cole", drop, source="Bravo Mtg")
    identify.enroll("Dana Cole", keep, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": keep})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(9)})

    status, body = _post(running_server, "/api/remove_sample",
                         {"name": "Dana Cole", "index": 0})
    assert status == 200 and body["ok"] is True
    assert body["note"] == "re-identifying all meetings in background", \
        "the existing fields survive the merge"
    assert body["targets"] == ["Alpha Mtg"]
    assert _bases(spawned[0]) == ["Alpha Mtg"]


def test_reassign_sample_targets_both_profiles(running_server, spawned):
    mine, theirs = _unit(1), _unit(2)
    identify.enroll("Dana Cole", mine, source="Alpha Mtg")
    identify.enroll("Dana Cole", theirs, source="Bravo Mtg")
    identify.enroll("Sam Reed", _unit(4), source="Bravo Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": mine})
    _meeting("Bravo Mtg", {"SPEAKER_00": theirs},
             speakers=[{"id": "SPEAKER_00", "name": "Sam Reed",
                        "display": "Sam Reed"}])
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(9)})

    status, body = _post(running_server, "/api/reassign_sample",
                         {"name": "Dana Cole", "index": 1, "to": "Sam Reed"})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"]
    assert _bases(spawned[0]) == ["Alpha Mtg", "Bravo Mtg"]


def test_api_relabel_still_walks_the_whole_library(running_server, spawned):
    """The explicit "re-identify everything" button is the one place --all is
    still right — it is the user asking for exactly that."""
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)})
    status, body = _post(running_server, "/api/relabel", {})
    assert status == 200 and body["ok"] is True
    assert spawned == [[str(srv.RUN_SH), "relabel", "--all"]]


def test_relabel_targets_falls_back_to_a_full_pass_when_the_index_breaks(
        sandbox, spawned, monkeypatch):
    """The index is a cache. A save that succeeded must never end in silence
    because the cache could not be built: the old full pass is the fallback."""
    from stt import centroids
    monkeypatch.setattr(centroids, "targets",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(srv, "_relabel_now", lambda base: True)
    res = srv._relabel_targets(vector=[1.0], first="Alpha Mtg")
    assert res == {"applied": "Alpha Mtg", "targets": ["--all"]}
    assert spawned and spawned[-1][-2:] == ["relabel", "--all"]


def test_relabel_targets_always_applies_the_requested_meeting(
        sandbox, spawned, monkeypatch):
    """Even when the index misses the requested meeting, it is relabeled in
    the request: the user is looking at it."""
    from stt import centroids
    monkeypatch.setattr(centroids, "targets", lambda **k: ["Bravo Mtg"])
    done = []
    monkeypatch.setattr(srv, "_relabel_now", lambda base: done.append(base) or True)
    res = srv._relabel_targets(vector=[1.0], first="Alpha Mtg")
    assert done == ["Alpha Mtg"]
    assert res["applied"] == "Alpha Mtg" and res["targets"] == ["Bravo Mtg"]

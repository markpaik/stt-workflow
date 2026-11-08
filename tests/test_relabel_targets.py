"""Targeted relabel: every endpoint that changes an identity hands the short
list of meetings the change can reach to the relabel queue, never `relabel
--all`.

Before this, naming one person walked 99 meetings at ~7s each — seven minutes,
in alphabetical order, with the meeting the user was looking at somewhere in the
middle. Each test here drives the real endpoint against a real running server
and reads the base list the queue was handed.

The queue (gui/jobqueue.RelabelQueue) is started by start_server, so a process
that never called it — the CLI-only panel mode, and these tests when they ask
for it — still has the temp-file spawn path it used before. Both are covered.
"""
import http.client
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from gui import jobqueue
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


class _FakeQueue:
    """The queue's submission surface, recording instead of relabeling. The
    real class is exercised in tests/test_jobqueue.py; here the question is
    only WHAT each endpoint hands it."""

    def __init__(self):
        self.submits = []     # [(bases, reason)]
        self.alls = []        # [reason]

    def submit(self, bases, reason=""):
        self.submits.append(([str(b) for b in bases], reason))
        return True

    def submit_all(self, reason=""):
        self.alls.append(reason)
        return True

    def snapshot(self):
        return jobqueue.empty_snapshot()


@pytest.fixture
def queued(monkeypatch):
    """A panel WITH a queue: nothing may be spawned."""
    q = _FakeQueue()
    monkeypatch.setattr(srv, "RELABEL_QUEUE", q)
    monkeypatch.setattr(srv, "_spawn",
                        lambda cmd: pytest.fail(f"nothing may be spawned: {cmd}"))
    monkeypatch.setattr(config, "PUNCTUATE", False)
    return q


@pytest.fixture
def spawned(monkeypatch):
    """A panel with NO queue (start_server was never called): the fallback
    spawn path. Every spawned command, with the subprocess itself stubbed."""
    seen = []
    monkeypatch.setattr(srv, "RELABEL_QUEUE", None)
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

def test_relabel_targets_applies_the_first_meeting_here_and_queues_the_rest(
        sandbox, queued):
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(2)})

    res = srv._relabel_targets(vector=v, first="Bravo Mtg")
    assert res == {"applied": "Bravo Mtg", "targets": ["Alpha Mtg"]}
    assert queued.submits == [(["Alpha Mtg"], "identity change")]
    assert queued.alls == []


def test_relabel_targets_falls_back_to_a_spawned_pass_with_no_queue(
        sandbox, spawned):
    """THE fallback: a panel process that never started a queue (the CLI-only
    `python -m gui.server` mode) still has to apply the change, so it writes
    the target list to a temp file and spawns `relabel --bases-from` exactly
    as it did before the queue existed."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(2)})

    res = srv._relabel_targets(vector=v, first="Bravo Mtg")
    assert res == {"applied": "Bravo Mtg", "targets": ["Alpha Mtg"]}
    assert len(spawned) == 1 and _bases(spawned[0]) == ["Alpha Mtg"]


def test_relabel_targets_queues_the_first_meeting_when_the_lock_is_busy(
        sandbox, queued, monkeypatch):
    """_relabel_now says False when another pass owns the relabel lock. The
    meeting must then join the queued list, not be silently dropped."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    monkeypatch.setattr(srv, "_relabel_now", lambda base: False)

    res = srv._relabel_targets(vector=v, first="Bravo Mtg")
    assert res["applied"] is None
    assert res["targets"] == ["Bravo Mtg", "Alpha Mtg"], \
        "the meeting in the request goes FIRST in the queued list"
    assert queued.submits == [(["Bravo Mtg", "Alpha Mtg"], "identity change")]


def test_relabel_targets_unions_the_bases_the_caller_already_knows(
        sandbox, queued):
    """`bases` are meetings the caller knows the change reaches (a retired
    unknown's own list). They join the score's answer, sorted. A base with no
    diarization cache, or no meeting on disk at all, is not a target: relabel
    could not rebuild it."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(2)})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(3)})
    _meeting("Echo Mtg", {"SPEAKER_00": _unit(4)})
    mfile("Echo Mtg", ".diar.npz").unlink()

    res = srv._relabel_targets(
        vector=v, first="Charlie Mtg",
        bases=["Bravo Mtg", "Charlie Mtg", "Echo Mtg", "Gone Mtg", "", None])
    assert res == {"applied": "Charlie Mtg",
                   "targets": ["Alpha Mtg", "Bravo Mtg"]}
    assert queued.submits == [(["Alpha Mtg", "Bravo Mtg"], "identity change")]


def test_relabel_targets_queues_nothing_when_there_is_nothing_to_do(sandbox, queued):
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)})
    assert srv._relabel_targets(vector=_unit(99)) == {"applied": None, "targets": []}
    assert queued.submits == [] and queued.alls == []


# ---------- one test per endpoint ----------

def test_name_applies_the_open_meeting_here_and_targets_the_others(
        running_server, queued):
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
    assert queued.submits == [(["Bravo Mtg"], "identity change")]
    # applied means applied: the name is in the transcript already
    names = {s.get("name") for s in json.loads(
        mfile("Alpha Mtg", ".json").read_text())["speakers"]}
    assert "Dana Cole" in names


def test_name_on_an_unknown_targets_the_meetings_that_voice_was_heard_in(
        running_server, queued):
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
    assert queued.submits == [(["Alpha Mtg"], "identity change")]


def _evict_early_sample(uid, later_base, later_vec):
    """The unknown was heard again in `later_base`, and only its newest rows
    survive (unknowns.MAX_SAMPLES): the early meeting stays in `meetings`
    while its own row has left the stack. One kept row stands in for the
    window here, so the early cluster scores against nothing that remains."""
    reg = unknowns.load()
    ent = reg["speakers"][uid]
    ids = unknowns.aligned_meeting_ids(ent)
    ent["meetings"] = list(ent["meetings"]) + [later_base]
    ent["meeting_ids"] = ids + [config.id_of(later_base)]
    np.save(config.VOICEPRINTS_DIR / ent["file"], np.atleast_2d(later_vec))
    unknowns.save(reg)


def test_name_on_an_unknown_reaches_a_meeting_its_kept_samples_no_longer_match(
        running_server, queued):
    """promote() retires the uid and its `meetings` list with it. The targets
    were computed AFTER that, from the kept samples alone, so a meeting whose
    row had left the MAX_SAMPLES window was found by nothing and kept its
    retired 'Speaker N' label for good. The list is read before promote."""
    early, late = _unit(3), _unit(4)
    _meeting("Alpha Mtg", {"SPEAKER_00": early})
    _meeting("Bravo Mtg", {"SPEAKER_00": late})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(7)})
    uid = unknowns.assign({"SPEAKER_00": early}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]
    _evict_early_sample(uid, "Bravo Mtg", late)
    from stt import centroids
    assert identify.score_against(early, [late]) < \
        config.NAMING_THRESHOLD - centroids.TARGET_MARGIN, \
        "the premise: the early cluster no longer scores against the stack"

    status, body = _post(running_server, "/api/name",
                         {"uid": uid, "name": "Dana Cole", "confirm": True})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"], \
        "a meeting the voice was heard in is a target whatever its score"
    assert queued.submits == [(["Alpha Mtg", "Bravo Mtg"], "identity change")]


def test_name_on_an_unknown_applies_the_open_meeting_here(
        running_server, queued):
    """A uid save sent no meeting, so the page the user had open was only
    queued while the toast said "Named here." The panel now sends the open
    page, and it relabels inside the response exactly as a cluster naming
    does."""
    v = _unit(3)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(7)})
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]
    assert unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                           "Bravo Mtg")["SPEAKER_00"] == uid

    status, body = _post(running_server, "/api/name",
                         {"uid": uid, "name": "Dana Cole", "confirm": True,
                          "meeting": "Bravo Mtg"})
    assert status == 200 and body["ok"] is True
    assert body["applied"] == "Bravo Mtg"
    assert body["targets"] == ["Alpha Mtg"]
    assert queued.submits == [(["Alpha Mtg"], "identity change")]
    names = {s.get("name") for s in json.loads(
        mfile("Bravo Mtg", ".json").read_text())["speakers"]}
    assert "Dana Cole" in names, "applied means applied"


def test_name_on_an_unknown_ignores_an_open_meeting_that_is_not_in_the_library(
        running_server, queued):
    """The meeting is context for a uid save, never its identity: a page that
    went stale (renamed in another tab) must not block naming the voice, and
    a base that is not a real meeting never reaches a file path."""
    v = _unit(3)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]

    status, body = _post(running_server, "/api/name",
                         {"uid": uid, "name": "Dana Cole", "confirm": True,
                          "meeting": "../../etc/passwd"})
    assert status == 200 and body["ok"] is True
    assert body["applied"] is None
    assert body["targets"] == ["Alpha Mtg"]


def test_name_on_an_unknown_never_calls_an_undiarized_open_meeting_applied(
        running_server, queued, monkeypatch):
    """relabel skips a meeting with no diarization cache, so relabeling it
    "here" changes nothing on the page. The response must not say applied,
    or the panel toasts "Named here." over a transcript that did not move."""
    v = _unit(3)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(8)})
    mfile("Bravo Mtg", ".diar.npz").unlink()
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]
    done = []
    monkeypatch.setattr(srv, "_relabel_now",
                        lambda base: done.append(base) or True)

    status, body = _post(running_server, "/api/name",
                         {"uid": uid, "name": "Dana Cole", "confirm": True,
                          "meeting": "Bravo Mtg"})
    assert status == 200 and body["ok"] is True
    assert body["applied"] is None and done == []
    assert body["targets"] == ["Alpha Mtg"]


def test_rename_speaker_targets_both_the_old_and_the_new_name(
        running_server, queued):
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
    assert queued.submits == [(["Alpha Mtg", "Bravo Mtg"], "identity change")]


def test_remove_speaker_targets_only_the_meetings_carrying_that_name(
        running_server, queued):
    identify.enroll("Dana Cole", _unit(1), source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                        "display": "Dana Cole"}])
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(3)})

    status, body = _post(running_server, "/api/remove_speaker",
                         {"name": "Dana Cole"})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg"]
    assert queued.submits == [(["Alpha Mtg"], "identity change")]


def test_merge_speakers_targets_both_sides(running_server, queued):
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
                         {"src": f"uid:{uid}", "dst": "name:Dana Cole",
                          "confirm": True})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"]
    assert queued.submits == [(["Alpha Mtg", "Bravo Mtg"], "identity change")]


def test_merge_speakers_uid_into_name_reaches_a_meeting_its_samples_no_longer_match(
        running_server, queued):
    """The drawer's merge of an unknown into a person retires the uid through
    the same promote(). A meeting whose row had left the stack was found by
    nothing afterwards, exactly as in /api/name."""
    va, early, late = _unit(1), _unit(3), _unit(4)
    identify.enroll("Dana Cole", va, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": va},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                        "display": "Dana Cole"}])
    _meeting("Bravo Mtg", {"SPEAKER_00": early})
    _meeting("Charlie Mtg", {"SPEAKER_00": late})
    _meeting("Delta Mtg", {"SPEAKER_00": _unit(9)})
    uid = unknowns.assign({"SPEAKER_00": early}, {"SPEAKER_00": None},
                          "Bravo Mtg")["SPEAKER_00"]
    _evict_early_sample(uid, "Charlie Mtg", late)

    status, body = _post(running_server, "/api/merge_speakers",
                         {"src": f"uid:{uid}", "dst": "name:Dana Cole",
                          "confirm": True})
    assert status == 200 and body["ok"] is True
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"]
    assert queued.submits == [
        (["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"], "identity change")]


def test_remove_sample_targets_the_remaining_profile(running_server, queued):
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
    assert queued.submits == [(["Alpha Mtg"], "identity change")]


def test_reassign_sample_targets_both_profiles(running_server, queued):
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
    assert queued.submits == [(["Alpha Mtg", "Bravo Mtg"], "identity change")]


def test_api_relabel_still_walks_the_whole_library(running_server, queued):
    """The explicit "re-identify everything" button is the one place --all is
    still right — it is the user asking for exactly that."""
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)})
    status, body = _post(running_server, "/api/relabel", {})
    assert status == 200 and body["ok"] is True
    assert queued.alls == ["re-identify everything"]
    assert queued.submits == []


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


def test_a_broken_index_queues_a_full_pass_when_there_is_a_queue(
        sandbox, queued, monkeypatch):
    """Same fallback, on a panel that HAS a queue: a full pass as a job, never
    a spawned child that reloads the models this process already holds."""
    from stt import centroids
    monkeypatch.setattr(centroids, "targets",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(srv, "_relabel_now", lambda base: True)
    res = srv._relabel_targets(vector=[1.0], first="Alpha Mtg")
    assert res == {"applied": "Alpha Mtg", "targets": ["--all"]}
    assert queued.alls == ["identity change"] and queued.submits == []


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


# ---------------------------------------------------------------------------
# Phase 5: the preview a naming panel reads BEFORE the save, and the two
# suggestion fields every unknown carries in /api/state. Same endpoints, same
# sandbox library; the question here is what the panel is told.
# ---------------------------------------------------------------------------

def _at_cos(v, c, seed):
    """A unit vector whose cosine against `v` is exactly `c`."""
    u = _unit(seed)
    u = u - np.dot(u, v) * v
    u = u / np.linalg.norm(u)
    w = c * np.asarray(v, float) + np.sqrt(1.0 - c * c) * u
    return w / np.linalg.norm(w)


def test_name_preview_for_a_meeting_cluster_names_here_and_the_others(
        running_server, sandbox):
    """The meeting in hand is always part of its own naming, and the preview
    says which OTHER meetings the same voice reaches."""
    v = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": v, "SPEAKER_01": _unit(5)})
    _meeting("Bravo Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(6)})

    status, body = _post(running_server, "/api/name_preview",
                         {"meeting": "Alpha Mtg", "speaker": "SPEAKER_00"})
    assert status == 200
    assert body["here"] == "Alpha Mtg"
    assert body["targets"] == ["Alpha Mtg", "Bravo Mtg"]
    assert body["looks_like"] == [], "nobody is enrolled yet"


def test_name_preview_keeps_the_meeting_even_with_no_embedding(
        running_server, sandbox):
    """A cluster the diarizer left no centroid for scores against nothing, so
    the index returns nothing. The meeting the user is looking at is still the
    meeting this save names."""
    _meeting("Alpha Mtg", {"SPEAKER_00": _unit(1)})
    status, body = _post(running_server, "/api/name_preview",
                         {"meeting": "Alpha Mtg", "speaker": "SPEAKER_99"})
    assert status == 200
    assert body == {"targets": ["Alpha Mtg"], "here": "Alpha Mtg",
                    "looks_like": []}


def test_name_preview_refuses_a_meeting_that_is_not_in_the_library(
        running_server, sandbox):
    status, _ = _post(running_server, "/api/name_preview",
                      {"meeting": "../../etc/passwd", "speaker": "SPEAKER_00"})
    assert status == 400


def test_name_preview_for_a_uid_lists_the_meetings_it_was_heard_in(
        running_server, sandbox):
    v = _unit(3)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(7)})
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Alpha Mtg")["SPEAKER_00"]

    status, body = _post(running_server, "/api/name_preview", {"uid": uid})
    assert status == 200
    assert body["here"] is None, "a uid naming has no single meeting in hand"
    assert body["targets"] == ["Alpha Mtg"]


def test_name_preview_for_a_uid_that_is_already_named_says_nothing(
        running_server, sandbox):
    """A stale chip. The SAVE explains itself; a preview must not say it twice,
    and it must not 500 on a uid the registry no longer holds."""
    status, body = _post(running_server, "/api/name_preview", {"uid": "U404"})
    assert status == 200
    assert body == {"targets": [], "here": None, "looks_like": []}


def test_name_preview_looks_like_is_best_first_and_cut_at_match_min(
        running_server, sandbox):
    """The audit's nineteen clusters: at or above MATCH_MIN, under the naming
    margin. The preview ranks them best first, keeps three, and drops anyone
    below the floor."""
    v = _unit(11)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    identify.enroll("Ada Vance", _at_cos(v, 0.95, 21), source="Alpha Mtg")
    identify.enroll("Bo Nash", _at_cos(v, 0.80, 22), source="Alpha Mtg")
    identify.enroll("Cy Frame", _at_cos(v, 0.70, 23), source="Alpha Mtg")
    identify.enroll("Dee Ward", _at_cos(v, 0.40, 24), source="Alpha Mtg")

    status, body = _post(running_server, "/api/name_preview",
                         {"meeting": "Alpha Mtg", "speaker": "SPEAKER_00"})
    assert status == 200
    assert [x["name"] for x in body["looks_like"]] == [
        "Ada Vance", "Bo Nash", "Cy Frame"], \
        "best first, at most three, and nobody under MATCH_MIN"
    assert [round(x["score"], 2) for x in body["looks_like"]] == [0.95, 0.80, 0.70]
    assert all(x["score"] >= unknowns.MATCH_MIN for x in body["looks_like"])


# ---------- the two suggestion fields in /api/state ----------

def _unknown_state(uid):
    return next(u for u in srv.gather_state()["unknowns"] if u["uid"] == uid)


def test_state_unknowns_carry_resolved_suggestions_and_looks_like(sandbox):
    """Phase 4 records a near miss as `suggested: [{uid, score}]`. The panel
    needs a display name to render and a uid that still exists to Link to."""
    v = _unit(31)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(32)})
    a = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                        "Alpha Mtg")["SPEAKER_00"]
    b = unknowns.assign({"SPEAKER_00": _unit(32)}, {"SPEAKER_00": None},
                        "Bravo Mtg")["SPEAKER_00"]
    identify.enroll("Ada Vance", _at_cos(v, 0.84, 41), source="Bravo Mtg")
    identify.enroll("Dee Ward", _at_cos(v, 0.40, 42), source="Bravo Mtg")
    reg = unknowns.load()
    reg["speakers"][a]["suggested"] = [{"uid": b, "score": 0.72},
                                       {"uid": "U404", "score": 0.99}]
    unknowns.save(reg)

    ent = _unknown_state(a)
    assert ent["suggested"] == [{"uid": b, "display": unknowns.display(b),
                                 "score": 0.72}], \
        "resolved to a display name, and a uid that no longer exists is dropped"
    assert [x["name"] for x in ent["looks_like"]] == ["Ada Vance"], \
        "an enrolled near miss above MATCH_MIN; the 0.40 one is not a guess"
    assert round(ent["looks_like"][0]["score"], 2) == 0.84
    # an unknown with neither still carries both keys, as empty lists
    other = _unknown_state(b)
    assert other["suggested"] == [] and isinstance(other["looks_like"], list)


def test_a_dropped_suggestion_is_never_offered(sandbox):
    """A tombstoned voice ("not a real speaker") must not come back as a Link
    the user can click."""
    v = _unit(33)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(34)})
    a = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                        "Alpha Mtg")["SPEAKER_00"]
    b = unknowns.assign({"SPEAKER_00": _unit(34)}, {"SPEAKER_00": None},
                        "Bravo Mtg")["SPEAKER_00"]
    reg = unknowns.load()
    reg["speakers"][a]["suggested"] = [{"uid": b, "score": 0.72}]
    reg["speakers"][b]["dropped"] = True
    unknowns.save(reg)
    assert _unknown_state(a)["suggested"] == []


def test_looks_like_is_not_rescored_while_the_registries_stand_still(
        sandbox, monkeypatch):
    """The panel polls every 2 seconds and the score is an all-vs-all against
    every enrolled print. The cache keys on the sample file's mtime and the
    registry's, so an unchanged library costs nothing after the first poll."""
    v = _unit(51)
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None}, "Alpha Mtg")
    identify.enroll("Ada Vance", _at_cos(v, 0.84, 61), source="Alpha Mtg")

    calls = []
    real = identify.score_against
    monkeypatch.setattr(identify, "score_against",
                        lambda vec, samples: calls.append(1) or real(vec, samples))

    srv.gather_state()
    first = len(calls)
    assert first > 0, "the first poll has to score something"
    srv.gather_state()
    assert len(calls) == first, "an unchanged registry must not be rescored"

    # a new enrollment moves registry.json, and the answer is computed again
    identify.enroll("Bo Nash", _at_cos(v, 0.70, 62), source="Alpha Mtg")
    srv.gather_state()
    assert len(calls) > first, "a changed registry must invalidate the cache"


def _near_unit(v, seed, eps=0.05):
    w = v + eps * _unit(seed)
    return w / np.linalg.norm(w)


def test_merge_speakers_warns_like_naming_when_the_voice_does_not_match(
        running_server, queued):
    """/api/name warns with suspect_sample when an unknown's voice does not
    match the person. A merge of the same unknown into the same person, or
    of one person into another, skipped that check and stacked foreign
    voices into a profile."""
    casey, stranger, dana = _unit(1), _unit(2), _unit(3)
    identify.enroll("Casey Quinn", casey, source="Alpha Mtg")
    identify.enroll("Dana Fox", dana, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": casey})
    _meeting("Bravo Mtg", {"SPEAKER_00": stranger})
    uid = unknowns.assign({"SPEAKER_00": stranger}, {"SPEAKER_00": None},
                          "Bravo Mtg")["SPEAKER_00"]

    for src in (f"uid:{uid}", "name:Dana Fox"):
        status, body = _post(running_server, "/api/merge_speakers",
                             {"src": src, "dst": "name:Casey Quinn"})
        assert status == 200 and body["ok"] is False, src
        assert body["warn"] == "suspect_sample" and body["need_confirm"] is True
    assert identify.load_voiceprints()["Casey Quinn"].shape[0] == 1
    assert "Dana Fox" in identify.load_registry()
    assert unknowns.samples_of(uid) is not None

    status, body = _post(running_server, "/api/merge_speakers",
                         {"src": "name:Dana Fox", "dst": "name:Casey Quinn",
                          "confirm": True})
    assert body["ok"] is True
    assert "Dana Fox" not in identify.load_registry()


def test_merge_speakers_of_the_same_voice_needs_no_confirm(running_server, queued):
    v = _unit(1)
    identify.enroll("Casey Quinn", v, source="Alpha Mtg")
    identify.enroll("Casey Q", _near_unit(v, 5), source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    status, body = _post(running_server, "/api/merge_speakers",
                         {"src": "name:Casey Q", "dst": "name:Casey Quinn"})
    assert body["ok"] is True, body


def test_a_rename_onto_an_enrolled_person_warns_like_a_merge(running_server, queued):
    """A rename onto a name that is already enrolled is a merge. It skipped
    the wrong-person check that /api/merge_speakers runs, so a case variant
    of an enrolled name stacked a foreign voice into that profile."""
    casey, dana = _unit(1), _unit(3)
    identify.enroll("Casey Quinn", casey, source="Alpha Mtg")
    identify.enroll("Dana Fox", dana, source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": casey})
    for new in ("Casey Quinn", "casey quinn"):
        status, body = _post(running_server, "/api/rename_speaker",
                             {"name": "Dana Fox", "new": new})
        assert status == 200 and body["ok"] is False, new
        assert body["warn"] == "suspect_sample" and body["need_confirm"] is True
        assert body["dst"] == "Casey Quinn"
    assert "Dana Fox" in identify.load_registry()
    assert identify.load_voiceprints()["Casey Quinn"].shape[0] == 1

    status, body = _post(running_server, "/api/rename_speaker",
                         {"name": "Dana Fox", "new": "Casey Quinn", "confirm": True})
    assert body["ok"] is True
    assert "Dana Fox" not in identify.load_registry()


def test_a_rename_to_a_new_name_or_onto_the_same_voice_needs_no_confirm(
        running_server, queued):
    v = _unit(1)
    identify.enroll("Casey Quinn", v, source="Alpha Mtg")
    identify.enroll("Casey Q", _near_unit(v, 5), source="Alpha Mtg")
    identify.enroll("Dana Fox", _unit(3), source="Alpha Mtg")
    _meeting("Alpha Mtg", {"SPEAKER_00": v})
    status, body = _post(running_server, "/api/rename_speaker",
                         {"name": "Dana Fox", "new": "Dana Fox-Reyes"})
    assert body["ok"] is True, body
    status, body = _post(running_server, "/api/rename_speaker",
                         {"name": "Casey Q", "new": "Casey Quinn"})
    assert body["ok"] is True, body


def test_both_shells_route_a_rename_warning_to_the_merge_confirm():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "gui" / "static"
    nxt = (root / "next.js").read_text()
    assert "if(r&&r.need_confirm){UI.naming=null;UI.confirm='mg:'+N.rename;UI.mergeTo=r.dst" in nxt
    app = (root / "app.js").read_text()
    assert "DRAWER.mergeWarn={dst:r.dst" in app

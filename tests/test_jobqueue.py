"""The relabel queue: one worker thread in the panel process, one job at a
time, coalescing in front of it.

Every test drives the REAL RelabelQueue. Only `relabel.relabel_one` and
`relabel._pass_over_everything` are stubbed, so the queue's own machinery — the
worker loop, the flock, the marker, the per-meeting failure boundary, the
progress counters — is what is under test.
"""
import fcntl
import json
import threading
import time

import pytest

import relabel
from gui import jobqueue
from gui import server as srv
from stt import config, control
from conftest import mfile


@pytest.fixture
def queue(sandbox):
    """A started queue, always stopped again — a leaked worker would hold the
    sandbox's relabel lock into the next test."""
    q = jobqueue.RelabelQueue()
    yield q
    q.stop(timeout=5)


def _until(fn, timeout=10.0, what="condition"):
    """Wait for `fn()` to return something truthy and return it."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _on(q, base, timeout=10.0):
    """Wait until the worker is actually working on `base`. Deliberately not
    "a job is running": `current` is set INSIDE the lock, after the marker is
    published, so waiting on it is the only point where the exclusion state is
    settled."""
    return _until(lambda: (q.snapshot()["running"] or {}).get("current") == base
                  and q.snapshot()["running"],
                  timeout, f"the worker to reach {base}")


def _drain(q, timeout=10.0):
    """Wait until nothing is running and nothing is queued; return the final
    snapshot."""
    def idle():
        s = q.snapshot()
        return s if (s["running"] is None and not s["queued"]) else None
    return _until(idle, timeout, "the queue to drain")


def _record(monkeypatch):
    """relabel_one replaced by a recorder. Returns the list it appends to."""
    seen = []
    monkeypatch.setattr(relabel, "relabel_one",
                        lambda base, **kw: seen.append(base))
    return seen


# ---------- coalescing ----------

def test_two_submits_before_the_worker_starts_become_one_job(sandbox, queue,
                                                             monkeypatch):
    """Naming two voices in quick succession used to spawn two full passes that
    blocked on each other's lock. A queued job has not started, so the second
    submit unions into it: one job, one lock acquisition, no meeting twice."""
    seen = _record(monkeypatch)
    queue.submit(["Alpha Mtg", "Bravo Mtg"], "named Dana")
    queue.submit(["Bravo Mtg", "Charlie Mtg"], "named Sam")

    snap = queue.snapshot()
    assert snap["running"] is None
    assert snap["queued"] == [{"kind": "relabel", "reason": "named Dana", "n": 3}], \
        "one job, three distinct meetings — Bravo is not queued twice"

    queue.start()
    _drain(queue)
    assert seen == ["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"], \
        "the union keeps submission order and drops the repeat"


def test_a_full_pass_absorbs_every_queued_relabel(sandbox, queue, monkeypatch):
    """`--all` is a superset of any target list, so a queued targeted job is
    pointless work once one is waiting. It is dropped, not run first."""
    seen = _record(monkeypatch)
    passes = []
    monkeypatch.setattr(relabel, "_pass_over_everything",
                        lambda **kw: passes.append(1))

    queue.submit(["Alpha Mtg"], "named Dana")
    queue.submit(["Bravo Mtg"], "named Sam")
    assert len(queue.snapshot()["queued"]) == 1
    queue.submit_all("re-identify everything")
    assert queue.snapshot()["queued"] == [
        {"kind": "all", "reason": "re-identify everything", "n": 0}]

    # and a relabel submitted BEHIND the queued full pass is absorbed too
    assert queue.submit(["Delta Mtg"], "named Kim") is False
    assert len(queue.snapshot()["queued"]) == 1
    # a second full pass adds nothing either
    assert queue.submit_all("again") is False

    queue.start()
    _drain(queue)
    assert seen == [], "the absorbed target lists never ran"
    assert passes == [1], "exactly one whole-library pass"


def test_a_running_job_is_never_mutated(sandbox, queue, monkeypatch):
    """Progress can only be trusted if the list it counts against is frozen.
    A submit that lands mid-job starts a NEW job behind it."""
    gate = threading.Event()
    seen = []

    def fake(base, **kw):
        seen.append(base)
        if base == "Alpha Mtg":
            gate.wait(10)
    monkeypatch.setattr(relabel, "relabel_one", fake)

    queue.start()
    queue.submit(["Alpha Mtg"], "named Dana")
    _on(queue, "Alpha Mtg")

    queue.submit(["Bravo Mtg"], "named Sam")
    snap = queue.snapshot()
    assert snap["running"]["total"] == 1, "the running job's list is frozen"
    assert snap["queued"] == [{"kind": "relabel", "reason": "named Sam", "n": 1}]

    gate.set()
    _drain(queue)
    assert seen == ["Alpha Mtg", "Bravo Mtg"]


# ---------- progress ----------

def test_snapshot_progress_moves_as_meetings_finish(sandbox, queue, monkeypatch):
    """"naming: 3 of 7" comes from here, so done/current/total have to move
    while the job runs, not only at the end."""
    gate = threading.Event()

    def fake(base, **kw):
        if base == "Bravo Mtg":
            gate.wait(10)
    monkeypatch.setattr(relabel, "relabel_one", fake)

    queue.start()
    queue.submit(["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"], "named Dana")
    r = _on(queue, "Bravo Mtg")
    assert r["kind"] == "relabel" and r["reason"] == "named Dana"
    assert r["total"] == 3 and r["done"] == 1
    assert r["started"], "a running job says when it started"

    gate.set()
    snap = _drain(queue)
    assert snap["running"] is None
    assert snap["last"]["kind"] == "relabel"
    assert snap["last"]["done"] == 3 and snap["last"]["failed"] == []
    assert snap["last"]["error"] is None and snap["last"]["finished"]


def test_a_whole_library_pass_reports_no_total(sandbox, queue, monkeypatch):
    """`_pass_over_everything` re-lists the library before every step so a
    meeting renamed mid-pass is picked up. Its length is therefore not known in
    advance, and the queue must say so rather than invent a denominator."""
    gate = threading.Event()
    monkeypatch.setattr(relabel, "_pass_over_everything",
                        lambda **kw: gate.wait(10))
    queue.start()
    queue.submit_all("queued follow-up")
    r = _until(lambda: queue.snapshot()["running"], what="the pass to start")
    assert r["kind"] == "all" and r["total"] is None and r["done"] == 0
    gate.set()
    _drain(queue)


# ---------- failure boundaries ----------

def test_one_unreadable_meeting_never_abandons_the_rest_of_the_job(
        sandbox, queue, monkeypatch):
    """relabel._safe_one is the per-meeting boundary a CLI pass uses. The queue
    uses the same one AND names the meetings that did not take, so a failure is
    reported instead of only printed into a log nobody reads."""
    seen = []

    def fake(base, **kw):
        seen.append(base)
        if base == "Bravo Mtg":
            raise ValueError("its transcript moved")
    monkeypatch.setattr(relabel, "relabel_one", fake)

    queue.start()
    queue.submit(["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"], "named Dana")
    snap = _drain(queue)

    assert seen == ["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"], \
        "the meeting after the failure still ran"
    assert snap["last"]["done"] == 3
    assert snap["last"]["failed"] == ["Bravo Mtg"]
    assert snap["last"]["error"] is None, \
        "a per-meeting failure is not a job failure"


def test_a_job_that_blows_up_is_recorded_and_the_worker_keeps_going(
        sandbox, queue, monkeypatch):
    """Anything outside the per-meeting boundary — the lock, the import, the
    pass itself — must not kill the worker, or every later naming in this panel
    session silently never applies."""
    calls = []

    def boom(**kw):
        calls.append(1)
        raise RuntimeError("the library is gone")
    monkeypatch.setattr(relabel, "_pass_over_everything", boom)
    seen = _record(monkeypatch)

    queue.start()
    queue.submit_all("re-identify everything")
    _until(lambda: (queue.snapshot()["last"] or {}).get("kind") == "all",
           what="the failing job to finish")
    assert queue.snapshot()["last"]["error"] == "RuntimeError: the library is gone"

    queue.submit(["Alpha Mtg"], "named Dana")
    snap = _drain(queue)
    assert calls == [1] and seen == ["Alpha Mtg"], "the worker survived"
    assert snap["last"]["kind"] == "relabel" and snap["last"]["error"] is None


# ---------- exclusion ----------

def test_a_running_job_holds_the_relabel_lock_and_publishes_the_marker(
        sandbox, queue, monkeypatch):
    """The CLI (`relabel.py`), run_batch's end-of-run pass and the panel's own
    `_relabel_now` all exclude each other through this flock. The queue is just
    another taker of it — nothing about that changed."""
    gate = threading.Event()
    monkeypatch.setattr(relabel, "relabel_one", lambda base, **kw: gate.wait(10))

    queue.start()
    queue.submit(["Alpha Mtg"], "named Dana")
    _on(queue, "Alpha Mtg")

    with open(control.relabel_lock_path(), "w") as fd:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert control.relabel_running() is True, \
        "the marker rides inside the lock, so the 2s poll can say so"
    assert srv._relabel_now("Alpha Mtg") is False, \
        "an in-request relabel steps aside for the job instead of blocking"

    gate.set()
    _drain(queue)
    # released, and the marker cleaned up
    with open(control.relabel_lock_path(), "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    assert control.relabel_running() is False


# ---------- the panel's side ----------

def _meeting(base):
    mfile(base, ".json").write_text(json.dumps(
        {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": "2026-05-01",
         "generated_at": "2026-05-01T09:00:00",
         "speakers": [], "segments": [], "words": []}))
    mfile(base, ".txt").write_text("stub")


def test_the_pending_flag_is_consumed_into_one_queued_full_pass(
        sandbox, monkeypatch):
    """`relabel.py` still writes relabel_pending.flag when a CLI pass loses the
    lock. The poll used to respawn `relabel --all` on a 60s cooldown and hope;
    it now consumes the flag ONCE into the queue, so the follow-up can neither
    be dropped nor run twice."""
    _meeting("Some Meeting 05012026")
    q = jobqueue.RelabelQueue()            # deliberately not started
    monkeypatch.setattr(srv, "RELABEL_QUEUE", q)
    monkeypatch.setattr(srv, "_spawn",
                        lambda cmd: pytest.fail(f"nothing may be spawned: {cmd}"))
    flag = config.PROJECT_DIR / relabel.PENDING_FLAG_NAME
    flag.write_text("all")

    st = srv.gather_state()
    assert not flag.exists(), "the flag is consumed, not left for the next poll"
    assert st["relabel_pending"] is False
    assert q.snapshot()["queued"] == [
        {"kind": "all", "reason": "queued follow-up", "n": 0}]

    # a second poll must not queue a phantom second pass
    srv.gather_state()
    assert len(q.snapshot()["queued"]) == 1


def test_state_carries_the_queue_shape(sandbox, queue, monkeypatch):
    """/api/state gains one field, `jobs`, and relabel_running now also means
    "our own worker is busy" — an in-process job has no child to discover."""
    _meeting("Some Meeting 05012026")
    monkeypatch.setattr(srv, "RELABEL_QUEUE", queue)
    # the marker would answer this on its own; force it False so the test
    # proves the OR arm that reads the queue
    monkeypatch.setattr(control, "relabel_running", lambda: False)

    st = srv.gather_state()
    assert st["jobs"] == {"running": None, "queued": [], "last": None}
    assert st["relabel_running"] is False

    gate = threading.Event()
    monkeypatch.setattr(relabel, "relabel_one", lambda base, **kw: gate.wait(10))
    queue.start()
    queue.submit(["Some Meeting 05012026"], "named Dana")
    _on(queue, "Some Meeting 05012026")

    st = srv.gather_state()
    assert st["relabel_running"] is True
    r = st["jobs"]["running"]
    assert set(r) == {"kind", "reason", "total", "done", "current", "started"}
    assert r["kind"] == "relabel" and r["reason"] == "named Dana"
    assert r["total"] == 1 and r["done"] == 0
    assert r["current"] == "Some Meeting 05012026"

    gate.set()
    _drain(queue)
    st = srv.gather_state()
    assert st["relabel_running"] is False
    assert set(st["jobs"]["last"]) == {"kind", "reason", "done", "finished",
                                       "failed", "error"}


def test_every_timeline_row_carries_its_save_stamp(sandbox):
    """The open meeting page reloads when ITS row's generated_at moves, so
    every row has to carry the field — and a meeting row has to carry the real
    value from its json, not a guess."""
    _meeting("Some Meeting 05012026")
    rows = srv.gather_state()["timeline"]
    assert rows and all("generated_at" in r for r in rows)
    row = next(r for r in rows if r["id"] == "Some Meeting 05012026")
    assert row["generated_at"] == "2026-05-01T09:00:00"

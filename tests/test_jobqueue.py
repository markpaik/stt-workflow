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


def _seed(*bases):
    """Put these meetings on disk. A queued job resolves its targets before it
    runs them (by id, else by the stored name), so a target that names nothing
    at all is reported as a failure instead of being handed to relabel — which
    means a test about the QUEUE has to queue meetings that exist."""
    for b in bases:
        _meeting(b)


# ---------- coalescing ----------

def test_two_submits_before_the_worker_starts_become_one_job(sandbox, queue,
                                                             monkeypatch):
    """Naming two voices in quick succession used to spawn two full passes that
    blocked on each other's lock. A queued job has not started, so the second
    submit unions into it: one job, one lock acquisition, no meeting twice."""
    _seed("Alpha Mtg", "Bravo Mtg", "Charlie Mtg")
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


def test_a_queued_job_follows_a_meeting_renamed_before_the_worker_ran(
        sandbox, queue, monkeypatch):
    """A job carries each target's meeting id, resolved at submit time. The
    meeting can be renamed while the job waits — the payload's NAME would then
    name nothing, and the naming the user just made would silently never apply
    to it. The id does not move, so the worker finds the meeting under its new
    name, once."""
    from stt import config, summarize
    _seed("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Alpha Mtg 05012026")
    seen = _record(monkeypatch)

    queue.submit(["Alpha Mtg 05012026"], "named Dana")   # the worker is not started
    r = summarize.rename_meeting("Alpha Mtg 05012026", "Focus Group")
    assert r["ok"] and r["base"] == "2026-05-01 Focus Group"
    assert config.base_of(mid) == "2026-05-01 Focus Group"

    queue.start()
    snap = _drain(queue)
    assert seen == ["2026-05-01 Focus Group"], \
        "relabelled under the new name, exactly once"
    assert snap["last"]["done"] == 1 and snap["last"]["failed"] == []


def test_a_target_that_no_longer_exists_is_reported_not_dropped(
        sandbox, queue, monkeypatch):
    """When neither the id nor the stored name resolves, the meeting was
    deleted between submit and here. Say so in `last` rather than counting it
    as work done."""
    _seed("Alpha Mtg 05012026")
    seen = _record(monkeypatch)
    queue.submit(["Alpha Mtg 05012026", "Gone Mtg 04012026"], "named Dana")
    queue.start()
    snap = _drain(queue)
    assert seen == ["Alpha Mtg 05012026"]
    assert snap["last"]["failed"] == ["Gone Mtg 04012026"]


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
    _seed("Alpha Mtg", "Bravo Mtg")
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
    _seed("Alpha Mtg", "Bravo Mtg", "Charlie Mtg")
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
    _seed("Alpha Mtg", "Bravo Mtg", "Charlie Mtg")
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
    _seed("Alpha Mtg")
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
    _seed("Alpha Mtg")
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


def test_a_start_after_a_timed_out_stop_keeps_the_worker(sandbox):
    """stop() set _stopping, timed out on the job in hand, and start() saw
    the live thread and returned. The worker exited after that job, and a
    later submit sat queued forever."""
    ran, release = [], threading.Event()

    class Slow(jobqueue.RelabelQueue):
        def _execute(self, job):
            if job.reason == "slow":
                release.wait(10)
            ran.append(job.reason)

    q = Slow().start()
    try:
        q.submit(["Standup 05012026"], "slow")
        _until(lambda: q.snapshot()["running"], what="the slow job to start")
        assert q.stop(timeout=0.05) is False      # the job in hand outlasts it
        q.start()
        release.set()
        _until(lambda: "slow" in ran, what="the slow job to end")
        q.submit(["Standup 05022026"], "after")
        _until(lambda: "after" in ran, what="the later job to run")
        assert q._thread is not None and q._thread.is_alive()
    finally:
        release.set()
        q.stop(timeout=5)

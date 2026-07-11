"""C5: run_batch's SIGTERM handler calls status.end_run() and jobs.add() while
the main thread may already hold those flocks. flock treats the handler's fresh
fd as a different holder, so an unguarded re-entry self-deadlocks until the 8s
SIGKILL escalation. The per-thread reentrancy guard must let the nested write
through. Each nested call runs in a worker thread joined with a timeout, so a
regression fails cleanly instead of hanging the whole suite."""
import threading


def test_status_lock_is_reentrant_within_a_thread(sandbox):
    from stt import status
    status.start_run(["a.m4a"])
    result = {}

    def outer_then_nested():
        with status._lock():          # the thread already holds the status lock
            status.end_run()          # ...and re-enters via end_run's own _lock
        result["ran"] = True

    t = threading.Thread(target=outer_then_nested)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "nested locked write deadlocked (reentrancy guard missing)"
    assert result.get("ran") and status.read().get("running") is False


def test_jobs_mutate_is_reentrant_within_a_thread(sandbox):
    from stt import jobs
    result = {}

    def body():
        def outer(cur):
            jobs.add({"files": ["y"], "paths": []})   # nested add re-enters _mutate
            return cur, None
        jobs._mutate(outer)
        result["ran"] = True

    t = threading.Thread(target=body)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "nested jobs mutate deadlocked (reentrancy guard missing)"
    assert result.get("ran")

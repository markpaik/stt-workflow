"""One relabel queue, owned by the panel process.

Every identity change used to spawn its own `run.sh relabel` child. Each child
paid a cold model load, several of them could be in flight at once (each one
blocking on the others' flock), and the panel could say nothing about any of
them beyond a single global "a relabel is running" boolean. There was no
progress, no coalescing, and no way to answer "how much is left".

This is the replacement: ONE daemon thread inside the panel process, running
one job at a time, with a queue in front of it that coalesces.

Why a thread and not a child process. `gui/server._relabel_now` already imports
`relabel` and runs `relabel_one` inside the request, so the punctuator and the
speaker models are ALREADY resident here — a child would load a second copy of
them (~1 GB) to do work this process can do warm. The thread also makes
progress a plain attribute read instead of a pipe protocol.

Exclusion is unchanged. A job holds `control.relabel_lock_path()` with a
BLOCKING flock for its whole run, and publishes `control.relabel_marker()`
inside it, exactly as `relabel.main` and `relabel.relabel_all` do. So the CLI
(`relabel.py`), `run_batch`'s end-of-run pass, and the panel's own
`_relabel_now` still exclude this worker and each other. flock is held per open
file description, so `_relabel_now`'s non-blocking acquire on its own fd fails
while this thread holds the lock, and the request falls back to queueing —
which is exactly the intended behavior.

Coalescing rules:
  * a queued `relabel` job unions later relabel submits into its base list,
    for as long as it has not started;
  * a queued `all` job absorbs every queued relabel and every later relabel
    submit — a full pass is a superset of any target list;
  * a RUNNING job is never mutated. Its base list, total and identity are
    fixed the moment the worker picks it up, so `snapshot()` progress can
    never run backwards or past its own total.
"""
import fcntl
import threading
from datetime import datetime

from stt import control

RELABEL = "relabel"     # a targeted pass over an explicit base list
ALL = "all"             # the whole library, re-listed as it goes


def empty_snapshot() -> dict:
    """The shape `snapshot()` returns, with nothing running and nothing queued.
    `/api/state` serves this when no queue was started (the CLI-only panel
    mode, and tests that drive the Handler directly), so the client sees one
    shape and never has to test for a missing key."""
    return {"running": None, "queued": [], "last": None}


def _clean(bases) -> list:
    """The submitted bases as an ordered, de-duplicated list of real names."""
    out, seen = [], set()
    for b in bases or ():
        b = str(b).strip()
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Job:
    __slots__ = ("kind", "reason", "bases", "done", "current", "started",
                 "failed")

    def __init__(self, kind, reason, bases=()):
        self.kind = kind
        self.reason = str(reason or "")
        self.bases = list(bases)
        self.done = 0
        self.current = None
        self.started = None
        self.failed = []

    @property
    def total(self):
        # an `all` pass re-lists the library before every step (a meeting
        # renamed mid-pass has to be picked up under its new name), so its
        # length is not known in advance and must not be guessed. None is the
        # honest answer, and the pill reads it as "applying names…".
        return None if self.kind == ALL else len(self.bases)


class RelabelQueue:
    """submit / submit_all / snapshot, and one worker thread behind them."""

    def __init__(self):
        self._cv = threading.Condition()   # guards _queued, _running, _last
        self._queued = []
        self._running = None
        self._last = None
        self._stopping = False
        self._thread = None

    # ---------- lifecycle ----------

    def start(self):
        """Start the worker. Idempotent: a second call is a no-op, so the
        menu bar restarting the panel thread cannot end up with two workers
        racing for the same lock."""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stopping = False
            self._thread = threading.Thread(target=self._work, name="relabel-queue",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=5.0):
        """Ask the worker to finish the job in hand and exit. Queued jobs are
        abandoned, not run: the panel is going away, and `relabel.py` still
        writes `relabel_pending.flag` for anything that has to survive a
        restart. Returns True when the thread is gone."""
        with self._cv:
            self._stopping = True
            t = self._thread
            self._cv.notify_all()
        if t is None:
            return True
        t.join(timeout=timeout)
        return not t.is_alive()

    # ---------- submission ----------

    def submit(self, bases, reason=""):
        """Queue a targeted pass over `bases`. Returns True when the submit
        changed the queue (a new job, or bases added to a queued one)."""
        bases = _clean(bases)
        if not bases:
            return False
        with self._cv:
            for j in self._queued:
                if j.kind == ALL:
                    return False        # a full pass already covers these
                if j.kind == RELABEL:
                    # union, order-preserving: the queued job has not started,
                    # so growing its list is safe and saves a whole second pass
                    have = set(j.bases)
                    added = [b for b in bases if b not in have]
                    j.bases.extend(added)
                    return bool(added)
            self._queued.append(_Job(RELABEL, reason, bases))
            self._cv.notify_all()
        return True

    def submit_all(self, reason=""):
        """Queue a pass over the whole library, absorbing queued relabels."""
        with self._cv:
            for j in self._queued:
                if j.kind == ALL:
                    return False        # one queued full pass is enough
            self._queued = [j for j in self._queued if j.kind != RELABEL]
            self._queued.append(_Job(ALL, reason))
            self._cv.notify_all()
        return True

    # ---------- reporting ----------

    def snapshot(self) -> dict:
        """What the panel shows: the running job's progress, what is waiting
        behind it, and how the last job ended."""
        with self._cv:
            r = None
            if self._running is not None:
                j = self._running
                r = {"kind": j.kind, "reason": j.reason, "total": j.total,
                     "done": j.done, "current": j.current, "started": j.started}
            return {"running": r,
                    "queued": [{"kind": j.kind, "reason": j.reason,
                                "n": len(j.bases)} for j in self._queued],
                    "last": dict(self._last) if self._last else None}

    # ---------- the worker ----------

    def _work(self):
        while True:
            with self._cv:
                while not self._queued and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    self._thread = None
                    return
                job = self._queued.pop(0)
                job.started = _now()
                self._running = job
            err = None
            try:
                self._execute(job)
            except BaseException as e:      # noqa: BLE001 - see below
                # EVERY exception, per job. One unreadable meeting, a lock that
                # cannot be opened, an import that blew up — none of it may
                # kill the worker, or every later naming in this panel session
                # silently never applies. The failure is reported through
                # `last` instead.
                err = f"{type(e).__name__}: {e}"
            with self._cv:
                self._last = {"kind": job.kind, "reason": job.reason,
                              "done": job.done, "finished": _now(),
                              "failed": list(job.failed), "error": err}
                self._running = None
                self._cv.notify_all()

    def _execute(self, job):
        # imported here, not at module import: `relabel` pulls the whole
        # pipeline (torch, the punctuator) in, and the panel must start fast
        # and must still import in a test process that never runs a job.
        import relabel

        with open(control.relabel_lock_path(), "w") as fd:
            # BLOCKING, unlike the CLI's non-blocking acquire: this job is
            # already queued, so waiting its turn behind `run_batch`'s
            # end-of-run pass is exactly right — dropping it would lose the
            # naming the user just made.
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # the marker rides inside the lock, so `control.relabel_running`
                # (the panel's 2s read-only probe) is true for as long as this
                # job really is running, and no longer
                with control.relabel_marker():
                    if job.kind == ALL:
                        relabel._pass_over_everything()
                    else:
                        self._run_bases(job, relabel)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _run_bases(self, job, relabel):
        for base in job.bases:
            with self._cv:
                job.current = base
            # _safe_one is relabel.py's own per-meeting boundary: one
            # unreadable meeting never abandons the rest of the list. It
            # returns the exception it swallowed, so the queue can name the
            # meetings that did not take.
            err = relabel._safe_one(base)
            with self._cv:
                job.done += 1
                if err is not None:
                    job.failed.append(base)
        with self._cv:
            job.current = None

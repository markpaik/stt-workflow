"""Lightweight live status for the menu-bar GUI and control panel.

The batch writes each file's stage here as it moves through the pipeline; the GUI
polls it. Supports multiple files in flight (parallel workers). Writes are atomic
and every function swallows its own errors — status reporting must never break
transcription.
"""
import contextlib
import fcntl
import json
import os
import threading
import time as _time
from datetime import datetime

from . import config

STATUS_PATH = config.PROJECT_DIR / "status.json"
HISTORY_LOG = config.PROJECT_DIR / "results.jsonl"  # permanent, one line per result

# ordered pipeline stages (for progress estimation / display)
STAGES = ["queued", "downloading", "converting", "transcribing", "diarizing",
          "writing", "done"]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def read() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _write(d):
    try:
        d["updated_at"] = _now()
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass


_lock_state = threading.local()


@contextlib.contextmanager
def _lock():
    """Serialize a read-modify-write across processes. The main run_batch process
    and every --parallel worker mutate the SAME status.json, so an unlocked RMW
    lost updates (a worker's write built from a pre-read snapshot clobbered
    another worker's just-published stage). Mirrors identify.lock_registry /
    jobs._mutate. A lock-acquire failure degrades to an unlocked write rather
    than raising — status reporting must never break the pipeline.

    Reentrant per THREAD: run_batch's SIGTERM handler calls end_run() while the
    main thread may already be inside a _lock() (mid finish_file/start_run).
    flock treats the handler's fresh fd as a DIFFERENT holder, so it would block
    on a lock this very process holds — a self-deadlock the SIGKILL escalation
    only breaks 8s later, meanwhile losing the clean shutdown. When this thread
    already holds it, skip the OS lock and write directly; we are the sole writer
    in that window. Thread-local, so genuine concurrent threads still serialize."""
    if getattr(_lock_state, "held", False):
        yield
        return
    lk = None
    try:
        lk = open(STATUS_PATH.with_suffix(".lock"), "w")
        fcntl.flock(lk, fcntl.LOCK_EX)
    except OSError:
        if lk is not None:
            lk.close()
        lk = None
    _lock_state.held = True
    try:
        yield
    finally:
        _lock_state.held = False
        if lk is not None:
            try:
                fcntl.flock(lk, fcntl.LOCK_UN)
            finally:
                lk.close()


def start_run(pending, sweep=False, summarizes=False):
    """`pending` is the files this run will process, in the order it will
    run them (shortest first). `sweep` marks a run that re-reads the watched
    folders before every file, so a file that lands mid-run joins it by
    length: the panel orders every waiting file that way while it runs.
    `summarizes` marks a run that writes a description after each file, so
    the queue's ETA counts one summary for each file still to finish."""
    # pgid lets the stop path find and kill the WHOLE process group — including
    # parallel workers whose command lines don't mention run_batch.py — even
    # after the parent has died.
    try:
        pgid = os.getpgid(0)
    except OSError:
        pgid = None
    with _lock():
        prev = read()
        # start_run rebuilds status.json from scratch — carry forward the keys
        # that outlive a single batch: `recent` (result history) and
        # `recording` (a capture in progress in the menu bar, whose banner must
        # survive a batch kicking off mid-recording).
        _write({"running": True, "pid": os.getpid(), "pgid": pgid, "started_at": _now(),
                "active": {}, "pending": list(pending), "sweep": bool(sweep),
                "summarizes": bool(summarizes),
                "recent": prev.get("recent", []),
                "recording": prev.get("recording"),
                "recorder_note": prev.get("recorder_note"),
                # recent summary wall times: the summarizing stage's estimate
                "summary_secs": prev.get("summary_secs", [])})


def set_pending(pending):
    """Replace the files still to run, in the order they will run. The batch
    re-picks the shortest file left before every file it starts, so this
    order changes as files arrive and finish."""
    with _lock():
        d = read()
        d["pending"] = list(pending)
        _write(d)


def set_recorder_note(ok, text):
    """The last recording's outcome ('Saved X — processing' / 'captured NO
    audio…'), persisted so BOTH surfaces show it in place: notifications from
    this unbundled python app either fail or arrive as unwanted osascript
    banners, so the menu bar and the panel are the feedback channel. Cleared by
    the next start (or dismissed in the panel)."""
    with _lock():
        d = read()
        d["recorder_note"] = {"ok": bool(ok), "text": str(text), "at": _now()}
        _write(d)


def clear_recorder_note():
    with _lock():
        d = read()
        if d.pop("recorder_note", None) is not None:
            _write(d)


NOTE_TTL_SECS = 20  # a success is an acknowledgement, not a task


def recorder_note():
    """The last recording's outcome. A SUCCESS expires on its own (it is just an
    acknowledgement — nothing to act on), so it never needs dismissing. A FAILURE
    persists until dismissed: it asks for a decision (grant permissions, check the
    log), and silently expiring it would be exactly the disappearing-error problem
    that made every empty capture look like 'nothing happened'."""
    n = read().get("recorder_note")
    if not n or not n.get("ok"):
        return n
    try:
        age = (datetime.now() - datetime.fromisoformat(n["at"])).total_seconds()
        return None if age > NOTE_TTL_SECS else n
    except (ValueError, TypeError, KeyError):
        return n


def set_recording(info):
    """Record that the menu-bar recorder is capturing (or clear with None).
    Locked read-modify-write so it composes with the batch's own writes; the
    key rides through start_run/set_stage/finish_file untouched."""
    with _lock():
        d = read()
        if info is None:
            d.pop("recording", None)
        else:
            d["recording"] = info
        _write(d)


def clear_recording():
    set_recording(None)


def recording():
    return read().get("recording")


def _exists(fn, ident) -> bool:
    """False only when the system says no such process (or group) exists.
    Anything it cannot tell (a bad id, a process of another user, a group
    of zombies, which macOS answers with EPERM) counts as alive, so the
    guards below keep refusing."""
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        return True
    if ident <= 0:
        return True
    try:
        fn(ident, 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True


def _owner_gone(d) -> bool:
    """True when status.json names the batch that wrote it and no process
    of that batch is left: not its pid, and not its process group (a
    --parallel worker can outlive a killed parent and still be writing).
    No pid (an older file, or a stage set outside a run) is not proof the
    owner is gone."""
    pid = d.get("pid")
    if not pid or _exists(os.kill, pid):
        return False
    pgid = d.get("pgid")
    return not (pgid and _exists(os.killpg, pgid))


def active_now() -> dict:
    """The files a batch is processing right now: status.json's active
    entries, or {} when the batch that wrote them is gone. Only end_run
    clears them, so a batch killed by SIGKILL, a crash, a power loss or a
    reboot left them behind. Every move of that meeting (archive, rename,
    summarize) and the queue's delete of that file then refused with 'being
    processed right now' until the next batch started, while the panel,
    which checks the processes, showed nothing running."""
    d = read()
    act = d.get("active") or {}
    if not isinstance(act, dict) or (act and _owner_gone(d)):
        return {}
    return act


def batch_running(d=None) -> bool:
    """Does status.json say a batch runs, and is that batch still alive?
    Only end_run clears `running`, so a SIGKILL, a crash or a power loss
    left it true, and every tool that refuses while a batch runs refused
    until the next batch started. A flag with no pid, or one whose pid or
    group cannot be checked, still counts as running (_owner_gone)."""
    d = read() if d is None else d
    return bool(d.get("running")) and not _owner_gone(d)


def meeting_active(base: str) -> bool:
    """Is the batch writing meeting `base` RIGHT NOW? Guards every folder move
    (rename/archive/delete) against racing a run. Active entries are keyed by
    SOURCE file name, but the meeting folder can be date-stamped ('LT Weekly
    Meeting' -> 'LT Weekly Meeting 06042026'), so the stem alone stopped
    matching new meetings — each entry also carries the resolved `base` the run
    announced via set_stage. Entries a dead batch left behind do not count
    (active_now)."""
    for k, v in active_now().items():
        if os.path.splitext(os.path.basename(k))[0] == base:
            return True
        if isinstance(v, dict) and v.get("base") == base:
            return True
    return False


def set_stage(name, stage, progress=None, duration=None, diarize=None, verify=None,
              base=None, est_sec=None):
    """progress: 0..1 within the current stage (None = unknown); duration: audio
    seconds (sent once, then remembered). diarize/verify: this file's actual
    settings (sent once, then remembered) — estimate_progress() needs to know
    up front whether "diarizing"/"verifying" will happen at all, rather than
    inferring it from whatever the CURRENT stage happens to be, which made the
    ETA jump the instant a --no-diarize or verify-mode file reached a stage
    the guess didn't anticipate. est_sec: the expected seconds of a stage
    outside the audio model (summarizing), kept while the stage lasts."""
    with _lock():
        d = read()
        active = d.get("active", {})
        prev = active.get(name, {})
        # stage_since is monotonic (compared only against _time.monotonic() in
        # estimate_progress) so an NTP jump / sleep can't inflate the wall-clock
        # bound; matches run_batch.stage_secs and control.stop_run. progress_at
        # below stays wall-clock because the panel compares it to Date.now().
        entry = {"stage": stage, "since": prev.get("since", _now()),
                 "stage_since": (prev.get("stage_since", _time.monotonic())
                                 if prev.get("stage") == stage else _time.monotonic())}
        # actual wall seconds of each FINISHED stage, so the panel can show
        # "Transcribing 3m ✓" instead of generalizing over the whole pipeline
        done_secs = dict(prev.get("done_secs") or {})
        if (prev.get("stage") and prev.get("stage") != stage
                and prev.get("stage_since") is not None):
            done_secs[prev["stage"]] = round(
                _time.monotonic() - prev["stage_since"], 1)
        if done_secs:
            entry["done_secs"] = done_secs
        if duration or prev.get("duration"):
            entry["duration"] = duration or prev.get("duration")
        if base or prev.get("base"):  # the resolved meeting folder (sent once)
            entry["base"] = base or prev.get("base")
        if diarize is not None or "diarize" in prev:
            entry["diarize"] = diarize if diarize is not None else prev.get("diarize")
        if verify is not None or "verify" in prev:
            entry["verify"] = verify if verify is not None else prev.get("verify")
        if est_sec is not None:
            entry["est_sec"] = round(float(est_sec), 1)
        elif prev.get("stage") == stage and "est_sec" in prev:
            entry["est_sec"] = prev["est_sec"]
        if progress is not None:
            entry["progress"] = round(float(progress), 3)
            # when the value last CHANGED — long hook-less stretches (pyannote's
            # clustering tail) freeze the bar; the panel uses this to say "still
            # working" instead of letting a stale ETA erode trust
            moved = (prev.get("stage") != stage
                     or entry["progress"] != prev.get("progress"))
            entry["progress_at"] = _time.time() if moved else prev.get("progress_at", _time.time())
        elif prev.get("stage") == stage and "progress" in prev:
            entry["progress"] = prev["progress"]
            entry["progress_at"] = prev.get("progress_at")
        active[name] = entry
        d["active"] = active
        d["pending"] = [p for p in d.get("pending", []) if p != name]
        _write(d)


def stage_estimates(duration: float, n_active: int = 1, verify: bool = False,
                    diarize: bool = True) -> dict:
    """Expected wall seconds per stage for `duration` seconds of audio.
    Each stage is auto-calibrated from past runs (stt.rates: a power law of
    the duration where the samples support one, else the median rate), keyed
    by the CURRENTLY selected ASR model (read fresh from stt.env on every call)
    and worker count; config defaults until real measurements exist.
    diarize/verify control whether those stages are budgeted for AT ALL — a
    --no-diarize or non-verify run must never have their time counted in the
    total."""
    from . import rates
    asr = rates.current_asr_key()
    est = {
        "downloading": 5.0,  # usually instant; real downloads show as slow stage
        "converting": rates.stage_secs("converting", duration, n_active, asr),
        "transcribing": rates.stage_secs("transcribing", duration, n_active, asr),
        "writing": rates.stage_secs("writing", duration, n_active, asr),
    }
    if diarize:
        est["diarizing"] = rates.stage_secs("diarizing", duration, n_active, asr)
    second = _verify_engine(asr) if verify else None
    if second:
        # a verify run with no second engine runs no verifying stage
        est["verifying"] = rates.stage_secs("transcribing", duration, n_active,
                                            second)
    return est


def _verify_engine(asr):
    from . import verify
    return verify.secondary_engine(asr)


def stage_tail(stage: str, n_active: int = 1) -> float:
    """The fraction of its elapsed time a stage that has run past its estimate
    is still expected to take: the stage model's own recent overrun (see
    stt.rates._tail), never a fixed remainder."""
    from . import rates
    asr = rates.current_asr_key()
    if stage == "verifying":
        second = _verify_engine(asr)
        return (rates.stage_tail("transcribing", n_active, second) if second
                else rates.DEFAULT_TAIL)
    if stage == "downloading":
        return rates.DEFAULT_TAIL
    return rates.stage_tail(stage, n_active, asr)


STAGE_ORDER = ["downloading", "converting", "transcribing", "diarizing",
               "verifying", "writing"]

# The description runs after a file finishes and before the next one
# starts (run_batch.summarize_one). Its time does not follow the audio
# length the way the stages above do: most of it is the model's fixed cost.
# So it counts down from the median of the last SUMMARY_KEEP summaries,
# measured in wall seconds, else SUMMARY_EST_DEFAULT (60 to 80 s a file on
# this Mac with the local model).
SUMMARY_EST_DEFAULT = 75.0
SUMMARY_KEEP = 8


def note_summary(secs: float):
    """Keep one summary's wall seconds for the next estimate."""
    with _lock():
        d = read()
        d["summary_secs"] = (list(d.get("summary_secs") or [])
                             + [round(float(secs), 1)])[-SUMMARY_KEEP:]
        _write(d)


def summary_estimate(st: dict = None) -> float:
    """The seconds one description takes. `st` is a status.json the caller
    already read."""
    from statistics import median
    vals = [v for v in (st if st is not None else read()).get("summary_secs") or []
            if isinstance(v, (int, float)) and v > 0]
    return float(median(vals)) if vals else SUMMARY_EST_DEFAULT


def _est_detail(entry: dict):
    """progress_detail for a stage with its own estimate (est_sec): a
    countdown on the clock, then 'running long' with a tail of
    rates.DEFAULT_TAIL times the elapsed time."""
    from . import rates
    est = float(entry.get("est_sec") or 0)
    ss = entry.get("stage_since")
    if est <= 0 or ss is None:
        return None
    elapsed = max(0.0, _time.monotonic() - ss)
    if elapsed > est:
        return {"pct": 0.99, "eta": rates.DEFAULT_TAIL * elapsed, "over": elapsed - est}
    return {"pct": min(0.99, elapsed / est), "eta": est - elapsed, "over": None}


def progress_detail(entry: dict, n_active: int = 1):
    """{"pct": 0..1, "eta": seconds, "over": seconds or None} for one active
    file, from its known audio duration, current stage, and within-stage
    progress. None if unknowable. `over` is how far the current stage has run
    past its estimate: the panel shows "running long · +Xm" in place of a
    countdown once it is set."""
    if entry.get("stage") not in STAGE_ORDER and entry.get("est_sec"):
        return _est_detail(entry)
    dur = entry.get("duration")
    if not dur:
        return None
    stage = entry.get("stage", "downloading")
    if stage not in STAGE_ORDER:
        return None
    # this file's ACTUAL settings, known from the start — not inferred from
    # whichever stage it happens to be in right now
    diarize = entry.get("diarize", True)
    verify = entry.get("verify", False)
    est = stage_estimates(dur, n_active, verify=verify, diarize=diarize)
    total = sum(est.values())
    idx = STAGE_ORDER.index(stage)
    done = sum(est.get(s, 0.0) for s in STAGE_ORDER[:idx])
    after = sum(est.get(s, 0.0) for s in STAGE_ORDER[idx + 1:])
    frac_in = entry.get("progress")
    if frac_in is None:
        frac_in = 0.5  # mid-stage assumption when the engine gives no callback
    # a stage's progress hook can race ahead of wall time (pyannote reports
    # segmentation, ~87% "done" within minutes, then embeds and clusters
    # silently for most of the stage) — reading that 87% at face value showed
    # "4 min left" on an hour-long file for 15+ real minutes. Completion
    # credit is therefore BOUNDED by elapsed wall time in the stage: a stage
    # is never more done than the clock allows, so the ETA converges on
    # reality instead of flattering the hook.
    est_stage = est.get(stage, 0.0)
    ss = entry.get("stage_since")
    if ss and est_stage > 0:
        # stage_since is monotonic (set in set_stage) — a corrected/jumped wall
        # clock must not make the stage look "almost done" when little real work
        # happened. monotonic is system-wide per boot, so cross-process (worker
        # writes it, GUI reads it) comparison stays valid.
        elapsed = max(0.0, _time.monotonic() - ss)
        if elapsed > est_stage:
            # RUNNING LONG. The old answer pinned the stage at 97% here, which
            # left a constant 3% of it on the clock: about ten minutes on a
            # long board meeting, frozen for hours. Now the stage has taken
            # `elapsed` so far and is given a tail that grows with it (the
            # model's own recent overrun), and the panel says how far past the
            # estimate it is instead of counting down.
            # The fraction leaves the tail out, so the bar never steps back
            # at the estimate: the work done over the work known so far.
            tail = stage_tail(stage, n_active) * elapsed
            done += elapsed
            return {"pct": min(0.99, done / (done + after)),
                    "eta": tail + after, "over": elapsed - est_stage}
        wall_frac = elapsed / est_stage
        pa = entry.get("progress_at")
        if (pa is not None and _time.time() - pa > 20.0
                and wall_frac > frac_in):
            # the hook STOPPED (pyannote's clustering tail reports nothing for
            # minutes) and the monotonic clock has genuinely passed its frozen
            # value — min(hook, wall) froze the countdown there and then
            # collapsed the ETA all at once when the stage flipped. Follow the
            # elapsed clock so the ETA keeps counting down to the estimate;
            # past it, the stage is running long (above). Both conditions
            # matter: staleness alone is wall-clock (progress_at) and an NTP
            # jump can fake it, but wall_frac is monotonic, so a faked stall
            # with little real elapsed keeps the brake on.
            frac_in = wall_frac
        else:
            frac_in = min(frac_in, wall_frac)
    done += est_stage * min(1.0, max(0.0, frac_in))
    return {"pct": min(0.99, done / total), "eta": max(0.0, total - done),
            "over": None}


def estimate_progress(entry: dict, n_active: int = 1):
    """(overall_fraction 0..1, eta_seconds) for one active file — see
    progress_detail. (None, None) if unknowable."""
    d = progress_detail(entry, n_active)
    if d is None:
        return None, None
    return d["pct"], d["eta"]


def stage_breakdown(entry: dict, n_active: int = 1):
    """Per-stage view for the panel, so the ETA stops generalizing over the
    whole pipeline: actual seconds for finished stages, elapsed vs expected
    for the current one, expected for the ones still ahead. None if the
    file's duration (and so any estimate) is unknown."""
    dur = entry.get("duration")
    stage = entry.get("stage")
    if not dur or stage not in STAGE_ORDER:
        return None
    est = stage_estimates(dur, n_active, verify=entry.get("verify", False),
                          diarize=entry.get("diarize", True))
    done_secs = entry.get("done_secs") or {}
    idx = STAGE_ORDER.index(stage)
    out = []
    for i, s in enumerate(STAGE_ORDER):
        if s == stage:
            ss = entry.get("stage_since")
            elapsed = max(0.0, _time.monotonic() - ss) if ss is not None else None
            out.append({"stage": s, "state": "active",
                        "secs": round(elapsed, 1) if elapsed is not None else None,
                        "est": round(est.get(s, 0.0), 1)})
        elif i < idx or s in done_secs:
            if s in done_secs or s in est:
                out.append({"stage": s, "state": "done", "secs": done_secs.get(s)})
        elif s in est:
            out.append({"stage": s, "state": "next", "est": round(est[s], 1)})
    return out


def clear_stage(name):
    """Drop a file from the active display WITHOUT logging a 'recent' entry —
    for post-processing stages (auto-summary) on files whose completion was
    already recorded by finish_file."""
    with _lock():
        d = read()
        if d.get("active", {}).pop(name, None) is not None:
            _write(d)


HISTORY_MAX_BYTES = 4 * 1024 * 1024   # ~tens of thousands of results (years of use)
HISTORY_KEEP_LINES = 20000            # trimmed back to this most-recent count


def _append_history(entry):
    """Append one result to the permanent log, and keep it from growing without
    bound: once it passes a generous size, rewrite it with only the most recent
    lines (atomic). Called only from finish_file under the status lock, so the
    rare trim never races another writer. Must never break the pipeline."""
    try:
        with open(HISTORY_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        if HISTORY_LOG.stat().st_size > HISTORY_MAX_BYTES:
            # surrogateescape: a byte that is not UTF-8 raised here, inside
            # finish_file, and is kept as it was now
            lines = HISTORY_LOG.read_text(
                errors="surrogateescape").splitlines()[-HISTORY_KEEP_LINES:]
            tmp = HISTORY_LOG.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", errors="surrogateescape")
            os.replace(tmp, HISTORY_LOG)
    except OSError:
        pass


def recent_naming(names) -> int:
    """How many rows of the `recent` ring name one of `names`. Reads only."""
    names = set(names)
    return sum(1 for r in read().get("recent") or []
               if isinstance(r, dict) and r.get("name") in names)


def forget_recent(names) -> int:
    """Drop the `recent` rows that name one of `names` (a purge). start_run
    carries the ring forward, so a row stays until 20 newer results push it
    out. Returns how many rows were dropped."""
    names = set(names)
    with _lock():
        d = read()
        rows = d.get("recent") or []
        keep = [r for r in rows if not (isinstance(r, dict) and r.get("name") in names)]
        if len(keep) == len(rows):
            return 0
        d["recent"] = keep
        _write(d)
        return len(rows) - len(keep)


def finish_file(name, ok, summary=""):
    with _lock():
        d = read()
        entry = {"name": name, "ok": bool(ok), "summary": summary, "at": _now()}
        recent = d.get("recent", [])
        recent.insert(0, entry)
        d["recent"] = recent[:20]
        d.get("active", {}).pop(name, None)
        _write(d)
        _append_history(entry)  # the permanent, size-capped history log


def is_result_row(r) -> bool:
    """A result as finish_file writes it: an object whose name and time
    are text (or absent). Anything else came from a hand edit or a disk
    fault, and every reader skips it."""
    return isinstance(r, dict) and all(
        isinstance(r.get(k), (str, type(None))) for k in ("name", "at"))


def history_rows() -> list:
    """The permanent results log, oldest first: only its result rows. A
    line that is valid JSON but not a result ('[1,2]', 'null') raised
    AttributeError in every reader, and a byte that is not UTF-8 raised
    UnicodeDecodeError, so one bad line crashed the doctor's check and the
    panel's failure list. Such a line is skipped now, like a torn one."""
    try:
        text = HISTORY_LOG.read_text(errors="replace")
    except OSError:
        return []
    rows = []
    for ln in text.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if is_result_row(r):
            rows.append(r)
    return rows


def history():
    """Every result ever recorded, newest first: the permanent results.jsonl
    merged with the status ring (which covers results from before the log
    existed), deduplicated by (name, at)."""
    rows = history_rows()
    rows.reverse()  # newest-last on disk; the sort below is stable, so ties
    # (results finishing within the same second) must already be newest-first
    seen = {(r.get("name"), r.get("at")) for r in rows}
    recent = read().get("recent")
    rows.extend(r for r in (recent if isinstance(recent, list) else [])
                if is_result_row(r) and (r.get("name"), r.get("at")) not in seen)
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows


def end_run():
    with _lock():
        d = read()
        d["running"] = False
        d["active"] = {}
        d["pid"] = None
        d["pgid"] = None  # never leave a stale group id a future pgid could recycle
        d["pending"] = []
        d["ended_at"] = _now()
        _write(d)

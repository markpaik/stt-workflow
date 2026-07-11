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


def start_run(pending):
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
                "active": {}, "pending": list(pending),
                "recent": prev.get("recent", []),
                "recording": prev.get("recording"),
                "recorder_note": prev.get("recorder_note")})


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


def recorder_note():
    return read().get("recorder_note")


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


def set_stage(name, stage, progress=None, duration=None, diarize=None, verify=None):
    """progress: 0..1 within the current stage (None = unknown); duration: audio
    seconds (sent once, then remembered). diarize/verify: this file's actual
    settings (sent once, then remembered) — estimate_progress() needs to know
    up front whether "diarizing"/"verifying" will happen at all, rather than
    inferring it from whatever the CURRENT stage happens to be, which made the
    ETA jump the instant a --no-diarize or verify-mode file reached a stage
    the guess didn't anticipate."""
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
        if diarize is not None or "diarize" in prev:
            entry["diarize"] = diarize if diarize is not None else prev.get("diarize")
        if verify is not None or "verify" in prev:
            entry["verify"] = verify if verify is not None else prev.get("verify")
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
    Rates are auto-calibrated medians from past runs (stt.rates), keyed by the
    CURRENTLY selected ASR model (read fresh from stt.env on every call) and
    worker count; config defaults until real measurements exist. diarize/verify
    control whether those stages are budgeted for AT ALL — a --no-diarize or
    non-verify run must never have their time counted in the total."""
    from . import rates
    est = {
        "downloading": 5.0,  # usually instant; real downloads show as slow stage
        "converting": duration / rates.convert_rate(),
        "transcribing": duration / rates.asr_rate(n_active=n_active),
        "writing": rates.writing_secs(),
    }
    if diarize:
        est["diarizing"] = duration / rates.diarize_rate(n_active)
    if verify:
        sec = "mlxwhisper:turbo" if rates.current_asr_key() == "parakeet" else "parakeet"
        est["verifying"] = duration / rates.asr_rate(sec, n_active)
    return est


STAGE_ORDER = ["downloading", "converting", "transcribing", "diarizing",
               "verifying", "writing"]


def estimate_progress(entry: dict, n_active: int = 1):
    """(overall_fraction 0..1, eta_seconds) for one active file, from its known
    audio duration, current stage, and within-stage progress. None if unknowable."""
    dur = entry.get("duration")
    if not dur:
        return None, None
    stage = entry.get("stage", "downloading")
    if stage not in STAGE_ORDER:
        return None, None
    # this file's ACTUAL settings, known from the start — not inferred from
    # whichever stage it happens to be in right now
    diarize = entry.get("diarize", True)
    verify = entry.get("verify", False)
    est = stage_estimates(dur, n_active, verify=verify, diarize=diarize)
    total = sum(est.values())
    idx = STAGE_ORDER.index(stage)
    done = sum(est.get(s, 0.0) for s in STAGE_ORDER[:idx])
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
        wall_frac = min((_time.monotonic() - ss) / est_stage, 0.97)
        pa = entry.get("progress_at")
        if (pa is not None and _time.time() - pa > 20.0
                and wall_frac > frac_in):
            # the hook STOPPED (pyannote's clustering tail reports nothing for
            # minutes) and the monotonic clock has genuinely passed its frozen
            # value — min(hook, wall) froze the countdown there and then
            # collapsed the ETA all at once when the stage flipped. Follow the
            # elapsed clock so the ETA keeps counting down, but never claim
            # the stage done: its actual end is the only honest 100%. Both
            # conditions matter: staleness alone is wall-clock (progress_at)
            # and an NTP jump can fake it, but wall_frac is monotonic, so a
            # faked stall with little real elapsed keeps the brake on.
            frac_in = min(wall_frac, 0.97)
        else:
            frac_in = min(frac_in, wall_frac)
    done += est_stage * min(1.0, max(0.0, frac_in))
    overall = min(0.99, done / total)
    return overall, max(0.0, total - done)


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
            lines = HISTORY_LOG.read_text().splitlines()[-HISTORY_KEEP_LINES:]
            tmp = HISTORY_LOG.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, HISTORY_LOG)
    except OSError:
        pass


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


def history():
    """Every result ever recorded, newest first: the permanent results.jsonl
    merged with the status ring (which covers results from before the log
    existed), deduplicated by (name, at)."""
    rows = []
    try:
        if HISTORY_LOG.exists():
            for ln in HISTORY_LOG.read_text().splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    rows.append(json.loads(ln))
    except OSError:
        pass
    rows.reverse()  # newest-last on disk; the sort below is stable, so ties
    # (results finishing within the same second) must already be newest-first
    seen = {(r.get("name"), r.get("at")) for r in rows}
    rows.extend(r for r in read().get("recent", [])
                if (r.get("name"), r.get("at")) not in seen)
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

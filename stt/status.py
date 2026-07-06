"""Lightweight live status for the menu-bar GUI and control panel.

The batch writes each file's stage here as it moves through the pipeline; the GUI
polls it. Supports multiple files in flight (parallel workers). Writes are atomic
and every function swallows its own errors — status reporting must never break
transcription.
"""
import json
import os
from datetime import datetime

from . import config

STATUS_PATH = config.PROJECT_DIR / "status.json"

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


def start_run(pending):
    # pgid lets the stop path find and kill the WHOLE process group — including
    # parallel workers whose command lines don't mention run_batch.py — even
    # after the parent has died.
    try:
        pgid = os.getpgid(0)
    except OSError:
        pgid = None
    _write({"running": True, "pid": os.getpid(), "pgid": pgid, "started_at": _now(),
            "active": {}, "pending": list(pending),
            "recent": read().get("recent", [])})


def set_stage(name, stage, progress=None, duration=None):
    """progress: 0..1 within the current stage (None = unknown); duration: audio
    seconds (sent once, then remembered)."""
    d = read()
    active = d.get("active", {})
    prev = active.get(name, {})
    entry = {"stage": stage, "since": prev.get("since", _now())}
    if duration or prev.get("duration"):
        entry["duration"] = duration or prev.get("duration")
    if progress is not None:
        entry["progress"] = round(float(progress), 3)
    elif prev.get("stage") == stage and "progress" in prev:
        entry["progress"] = prev["progress"]
    active[name] = entry
    d["active"] = active
    d["pending"] = [p for p in d.get("pending", []) if p != name]
    _write(d)


def stage_estimates(duration: float, n_active: int = 1) -> dict:
    """Expected wall seconds per stage for `duration` seconds of audio.
    Rates are auto-calibrated medians from past runs (stt.rates), keyed by the
    currently selected ASR model and worker count; config defaults until then."""
    from . import rates
    return {
        "downloading": 5.0,  # usually instant; real downloads show as slow stage
        "converting": duration / rates.convert_rate(),
        "transcribing": duration / rates.asr_rate(n_active=n_active),
        "diarizing": duration / rates.diarize_rate(n_active),
        "writing": rates.writing_secs(),
    }


STAGE_ORDER = ["downloading", "converting", "transcribing", "diarizing", "writing"]


def estimate_progress(entry: dict, n_active: int = 1):
    """(overall_fraction 0..1, eta_seconds) for one active file, from its known
    audio duration, current stage, and within-stage progress. None if unknowable."""
    dur = entry.get("duration")
    if not dur:
        return None, None
    est = stage_estimates(dur, n_active)
    total = sum(est.values())
    stage = entry.get("stage", "downloading")
    if stage not in STAGE_ORDER:
        return None, None
    idx = STAGE_ORDER.index(stage)
    done = sum(est[s] for s in STAGE_ORDER[:idx])
    frac_in = entry.get("progress")
    if frac_in is None:
        frac_in = 0.5  # mid-stage assumption when the engine gives no callback
    done += est[stage] * min(1.0, max(0.0, frac_in))
    overall = min(0.99, done / total)
    return overall, max(0.0, total - done)


def finish_file(name, ok, summary=""):
    d = read()
    recent = d.get("recent", [])
    recent.insert(0, {"name": name, "ok": bool(ok), "summary": summary, "at": _now()})
    d["recent"] = recent[:20]
    d.get("active", {}).pop(name, None)
    _write(d)


def end_run():
    d = read()
    d["running"] = False
    d["active"] = {}
    d["pid"] = None
    d["pgid"] = None  # never leave a stale group id a future pgid could recycle
    d["pending"] = []
    d["ended_at"] = _now()
    _write(d)

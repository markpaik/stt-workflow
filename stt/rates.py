"""Auto-calibrated per-stage speed rates, learned from every completed run.

Each finished file contributes one JSON line to rates.jsonl: audio duration plus
measured wall-seconds per stage (converting / transcribing / diarizing / writing),
keyed by ASR model and worker count. Estimates use the MEDIAN of the last few
samples per key — robust to the odd outlier — and fall back to the hand-measured
config.EST_RATES defaults until real data exists.

Only the batch PARENT appends (workers return their timings in the result), so
there is a single writer and no locking. Stage times are measured with the
monotonic clock, which on macOS does not advance during sleep — a lid-close
mid-run cannot poison the calibration.
"""
import json
from datetime import datetime
from statistics import median

from . import config

RATES_LOG = config.PROJECT_DIR / "rates.jsonl"
MIN_AUDIO_SEC = 120.0  # tiny files are overhead-dominated; don't learn from them
KEEP_SAMPLES = 8       # median over the most recent N samples per key

_cache = {"sig": None, "learned": None}


def current_asr_key() -> str:
    kv = config._env_file()
    backend = kv.get("STT_ASR_BACKEND", "parakeet")
    if backend == "mlxwhisper":
        return f"mlxwhisper:{kv.get('STT_WHISPER_MLX_MODEL', 'large-v3')}"
    return backend


def record(duration: float, stage_secs: dict, asr_key: str, n_active: int = 1):
    """Append one completed file's measurements. Called by the batch parent only."""
    try:
        if not duration or duration < MIN_AUDIO_SEC:
            return
        secs = {k: round(float(v), 1) for k, v in (stage_secs or {}).items()
                if v and v > 0.5}
        if not secs:
            return
        row = {"at": datetime.now().isoformat(timespec="seconds"),
               "duration": round(float(duration), 1),
               "asr": asr_key, "n": int(n_active), "secs": secs}
        with open(RATES_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # calibration must never break the pipeline


def _rows():
    if not RATES_LOG.exists():
        return []
    rows = []
    for ln in RATES_LOG.read_text().splitlines()[-400:]:
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


def learned() -> dict:
    """{"convert": rate, "asr": {"parakeet@1": rate, ...}, "diarize": {"1": rate},
    "writing": secs, "runs": n} — medians of the most recent samples per key."""
    try:
        sig = RATES_LOG.stat().st_mtime if RATES_LOG.exists() else 0
    except OSError:
        sig = 0
    if _cache["sig"] == sig and _cache["learned"] is not None:
        return _cache["learned"]

    buckets = {"convert": [], "asr": {}, "diarize": {}, "writing": []}
    rows = _rows()
    for r in rows:
        d, secs, n = r.get("duration", 0), r.get("secs", {}), str(r.get("n", 1))
        if secs.get("converting"):
            buckets["convert"].append(d / secs["converting"])
        if secs.get("transcribing"):
            buckets["asr"].setdefault(f"{r.get('asr', 'parakeet')}@{n}", []).append(
                d / secs["transcribing"])
        if secs.get("diarizing"):
            buckets["diarize"].setdefault(n, []).append(d / secs["diarizing"])
        if secs.get("writing"):
            buckets["writing"].append(secs["writing"])

    def med(vals):
        return round(median(vals[-KEEP_SAMPLES:]), 2) if vals else None

    out = {"convert": med(buckets["convert"]),
           "asr": {k: med(v) for k, v in buckets["asr"].items()},
           "diarize": {k: med(v) for k, v in buckets["diarize"].items()},
           "writing": med(buckets["writing"]),
           "runs": len(rows)}
    _cache["sig"], _cache["learned"] = sig, out
    return out


# ---- estimate accessors (learned value, else config default) ----

def convert_rate() -> float:
    return learned()["convert"] or config.EST_RATES["convert"]


def asr_rate(asr_key: str = None, n_active: int = 1) -> float:
    asr_key = asr_key or current_asr_key()
    L = learned()["asr"]
    got = L.get(f"{asr_key}@{n_active}") or L.get(f"{asr_key}@1")
    return got or config.EST_RATES["asr"].get(asr_key, 30.0)


def diarize_rate(n_active: int = 1) -> float:
    L = learned()["diarize"]
    got = L.get(str(n_active))
    if got:
        return got
    base = L.get("1") or config.EST_RATES["diarize"]
    return base / (1.4 if n_active > 1 else 1.0)  # measured contention fallback


def writing_secs() -> float:
    return learned()["writing"] or config.EST_RATES["writing_fixed_sec"]


def summary() -> dict:
    """Compact human summary for the GUI settings row."""
    L = learned()
    parts = []
    for key, label in [("parakeet", "Parakeet"),
                       ("mlxwhisper:large-v3", "Whisper v3"),
                       ("mlxwhisper:turbo", "Turbo")]:
        r = L["asr"].get(f"{key}@1")
        if r:
            parts.append(f"{label} {r:.0f}×")
    d = L["diarize"].get("1")
    if d:
        parts.append(f"Speakers {d:.1f}×")
    return {"runs": L["runs"], "text": " · ".join(parts)}

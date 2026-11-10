"""Auto-calibrated per-stage speed rates, learned from every completed run.

Each finished file contributes one JSON line to rates.jsonl: audio duration plus
measured wall-seconds per stage (converting / transcribing / diarizing / writing),
keyed by ASR model and worker count.

Each stage's seconds are modelled as a POWER LAW of the audio duration,
secs = a * duration^b, fitted by least squares on log-log over the most recent
samples for that key. One flat rate (audio seconds per wall second) cannot
describe diarization here: it is pinned to the CPU and its cost grows faster
than the audio (1.26x real time at 21 min, 1.45x at 2.9 h, 1.68x at 4.5 h), and
a median taken mostly from short meetings estimated a 4.5 h board meeting about
25 percent short. The fit is used only when it is well posed (FIT_MIN_SAMPLES
samples whose durations span FIT_MIN_SPAN); b is clamped to FIT_B_RANGE.
Otherwise the estimate is the MEDIAN rate of the last few samples, as before,
and the hand-measured config.EST_RATES defaults until real data exists.
Writing is the exception: merge + punctuation + output is roughly flat by
design, a slope floored at 0.8 cannot describe it, so it always keeps its
median seconds (on the real log the fit overshot files of 90 min or more by
139 percent against 69 percent for the median).
tools/eta_backtest.py replays rates.jsonl to compare the two models.

Every model also carries a TAIL: how far past its prediction a stage tends to
run (the TAIL_Q quantile of actual over predicted in its own recent samples,
minus one). A stage already past its estimate is given that fraction of its
elapsed time as its remaining time (status.estimate_progress), never a fixed
remainder that freezes the countdown.

Only the batch PARENT appends (workers return their timings in the result), so
there is a single writer and no locking. Stage times are measured with the
monotonic clock, which on macOS does not advance during sleep — a lid-close
mid-run cannot poison the calibration.
"""
import json
import math
from datetime import datetime
from statistics import median

from . import config

RATES_LOG = config.PROJECT_DIR / "rates.jsonl"
MIN_AUDIO_SEC = 120.0  # tiny files are overhead-dominated; don't learn from them
KEEP_SAMPLES = 8       # median over the most recent N samples per key
FIT_SAMPLES = 24       # the power law is fitted over the most recent N per key
FIT_MIN_SAMPLES = 6    # fewer than this: the median rate instead
FIT_MIN_SPAN = 2.0     # longest / shortest duration the fit needs to see
FIT_B_RANGE = (0.8, 1.6)
TAIL_Q = 0.8           # the tail is this quantile of actual / predicted, minus 1
TAIL_RANGE = (0.05, 1.0)
DEFAULT_TAIL = 0.10    # before any sample exists
CONTENTION = 1.4       # measured diarize slowdown at 2 workers, when unmeasured

_cache = {"sig": None, "learned": None}


def whisper_variant(v) -> str:
    """The name a Whisper variant's speed is learned and estimated under.
    An alias of a known model is that model: large-v3-turbo runs the turbo
    repo, but as its own key it had no default (the 30x Parakeet rate, an
    ETA about 3.5 times too short) and its samples never taught turbo."""
    from .asr_mlxwhisper import VARIANT_ALIASES
    v = v or "large-v3"
    return VARIANT_ALIASES.get(v, v)


# the engine prefix of each cloud provider's transcripts (stt/asr_cloud.py
# writes "elevenlabs/<model>"), and the provider it names
_CLOUD_ENGINES = {"elevenlabs": "scribe", "openai": "openai", "mistral": "voxtral"}


def engine_key(engine):
    """The model key of the engine that wrote a transcript (the "engine" an
    ASR module returns, such as "mlx-whisper/turbo"), or None when none is
    named. A finished file's seconds are learned under this. The setting
    read at the end of the file named a model that did not run when the
    model was switched in the panel mid-run, when a strict file ran locally
    under a cloud setting, and when a cloud run fell back to Parakeet."""
    if not isinstance(engine, str) or not engine:
        return None
    head, _, tail = engine.partition("/")
    if head == "mlx-whisper":
        return f"mlxwhisper:{whisper_variant(tail)}"
    if engine != config.PARAKEET_MODEL and head in _CLOUD_ENGINES:
        return f"cloud:{_CLOUD_ENGINES[head]}"
    return "parakeet"


def current_asr_key() -> str:
    kv = config._env_file()
    # the setting's own check, as pipeline._load_asr reads it: case and
    # spaces are ignored and a typo runs Parakeet. The raw value ran Whisper
    # for "MLXWhisper" but keyed it "MLXWhisper" (the 30x default, 120 s for
    # an hour, not 857 s), and filed a typo's Parakeet runs under the typo.
    backend, _ = config._asr_problem(kv.get("STT_ASR_BACKEND"))
    if backend == "mlxwhisper":
        return f"mlxwhisper:{whisper_variant(kv.get('STT_WHISPER_MLX_MODEL'))}"
    return backend


def diarizer_family(engine) -> str:
    """"speakrs" or "pyannote" for a stored engine id. Rows from before the
    field existed (None) were all pyannote."""
    return "speakrs" if engine and str(engine).startswith("speakrs") else "pyannote"


def effective_diarizer() -> str:
    """The engine diarize() will actually run: config.DIARIZER, except that a
    speakrs setting with the helper or models missing runs pyannote
    (config checks the setting, so it is "speakrs" or "pyannote"). The ETA
    and its learned samples follow this, not the setting, so a fallback
    install is estimated at CPU speed."""
    if config.DIARIZER != "speakrs":
        return "pyannote"
    from . import speakrs_diar
    return "speakrs" if speakrs_diar.available()[0] else "pyannote"


def _est_diarize_rate() -> float:
    return config.EST_RATES["diarize"][effective_diarizer()]


def record(duration: float, stage_secs: dict, asr_key: str, n_active: int = 1,
           diarizer: str = None):
    """Append one completed file's measurements. Called by the batch parent only.
    `diarizer` is the engine that actually ran (a fallback file is pyannote)."""
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
        # rows written before this field were all pyannote (see _samples)
        row["diar"] = diarizer_family(diarizer) if diarizer else effective_diarizer()
        with open(RATES_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # calibration must never break the pipeline


def _num(v):
    """v as a float when it is a finite number above zero, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) and v > 0 else None


def _clean_row(r):
    """The row with only the values the model can use, or None. A line that
    is JSON but not a row (123, null, a list) or a value of the wrong type
    ("secs": null, a string duration) raised in _samples, so every ETA and
    the panel's /api/state failed until newer rows pushed the line out.
    record() writes only good rows: this guards a hand edit or a damaged
    file."""
    if not isinstance(r, dict):
        return None
    d, secs = _num(r.get("duration")), r.get("secs")
    if not d or not isinstance(secs, dict):
        return None
    secs = {k: v for k, v in ((k, _num(v)) for k, v in secs.items())
            if isinstance(k, str) and v}
    if not secs:
        return None
    out = {**r, "duration": d, "secs": secs}
    for key in ("asr", "diar", "at"):
        if key in out and not isinstance(out[key], str):
            del out[key]  # the readers' own default (parakeet, pyannote) applies
    n = r.get("n", 1)
    out["n"] = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
    return out


def parse_rows(lines) -> list:
    """The usable rows among rates.jsonl `lines`, in file order. A line that
    does not parse, or is not a usable row (_clean_row), is skipped.
    tools/eta_backtest.py reads the log through this too."""
    rows = []
    for ln in lines:
        try:
            r = _clean_row(json.loads(ln))
        except ValueError:
            continue
        if r is not None:
            rows.append(r)
    return rows


def _rows():
    try:
        # bytes that are not text spoil only their own line
        text = RATES_LOG.read_text(errors="replace")
    except OSError:
        return []
    return parse_rows(text.splitlines()[-400:])


def _samples(rows, diarizer: str = None) -> dict:
    """Bucket the rows into (duration, secs) samples per model key, oldest
    first: {"convert": [...], "asr": {"parakeet@1": [...]}, "diarize": {"1":
    [...]}, "writing": [...]}. Pure, so tools/eta_backtest.py can replay any
    prefix of the log. Diarize samples count only for `diarizer` (default:
    effective_diarizer()): pyannote hours must not teach the speakrs estimate."""
    want = diarizer or effective_diarizer()
    buckets = {"convert": [], "asr": {}, "diarize": {}, "writing": []}
    for r in rows:
        d, secs, n = r.get("duration", 0), r.get("secs", {}), str(r.get("n", 1))
        if not d:
            continue
        if secs.get("converting"):
            buckets["convert"].append((d, secs["converting"]))
        if secs.get("transcribing"):
            buckets["asr"].setdefault(f"{r.get('asr', 'parakeet')}@{n}", []).append(
                (d, secs["transcribing"]))
        if secs.get("diarizing") and r.get("diar", "pyannote") == want:
            buckets["diarize"].setdefault(n, []).append((d, secs["diarizing"]))
        if secs.get("verifying"):
            # the verify pass IS a transcription by the secondary engine —
            # its timing calibrates that engine's rate like any primary run
            from . import verify as _verify
            backend, variant = _verify.secondary_engine(r.get("asr", "parakeet"))
            sec_key = f"{backend}:{variant}" if variant else backend
            buckets["asr"].setdefault(f"{sec_key}@{n}", []).append(
                (d, secs["verifying"]))
        if secs.get("writing"):
            buckets["writing"].append((d, secs["writing"]))
    return buckets


def _quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    pos = q * (len(vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _tail(ratios) -> float:
    """How far past its prediction a stage tends to run, as a fraction: the
    TAIL_Q quantile of actual / predicted, minus one, clamped to TAIL_RANGE
    (a floor, so a stage that has already overrun always gets SOME time)."""
    q = _quantile(ratios, TAIL_Q)
    if q is None:
        return DEFAULT_TAIL
    return round(min(TAIL_RANGE[1], max(TAIL_RANGE[0], q - 1.0)), 3)


def _fit(pairs):
    """secs = a * duration^b by least squares on log-log over the most recent
    FIT_SAMPLES (duration, secs) pairs. None when the fit is not well posed:
    fewer than FIT_MIN_SAMPLES samples, or durations spanning less than
    FIT_MIN_SPAN (a slope read off a narrow band of lengths is noise)."""
    pts = [(d, s) for d, s in pairs[-FIT_SAMPLES:] if d > 0 and s > 0]
    if len(pts) < FIT_MIN_SAMPLES:
        return None
    ds = [d for d, _ in pts]
    if max(ds) / min(ds) < FIT_MIN_SPAN:
        return None
    xs = [math.log(d) for d, _ in pts]
    ys = [math.log(s) for _, s in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = min(FIT_B_RANGE[1], max(FIT_B_RANGE[0], b))
    # the intercept is refitted for the (possibly clamped) slope
    la = sum(y - b * x for x, y in zip(xs, ys)) / len(xs)
    ratios = [math.exp(y - (la + b * x)) for x, y in zip(xs, ys)]
    return {"la": la, "b": round(b, 4), "n": len(pts), "tail": _tail(ratios)}


def _median_model(pairs, flat=False):
    """The median model over the most recent KEEP_SAMPLES pairs: a rate
    (audio seconds per wall second), or for `flat` stages a fixed number of
    seconds. Returns (value, tail)."""
    recent = pairs[-KEEP_SAMPLES:]
    if not recent:
        return None, DEFAULT_TAIL
    if flat:
        m = round(median(s for _, s in recent), 2)
        return m, _tail([s / m for _, s in recent] if m else [])
    m = round(median(d / s for d, s in recent), 2)
    return m, _tail([s * m / d for d, s in recent])


def _learn(rows, diarizer: str = None) -> dict:
    """The model built from `rows`. The median keys ("convert", "asr",
    "diarize", "writing", "runs") keep the shape the panel's summary reads;
    "fit" holds the power laws where they are well posed and "tail" each
    model's overrun fraction (the fit's when there is one)."""
    b = _samples(rows, diarizer)
    out = {"runs": len(rows), "asr": {}, "diarize": {},
           "fit": {"asr": {}, "diarize": {}},
           "tail": {"asr": {}, "diarize": {}}}
    for name, flat in (("convert", False), ("writing", True)):
        out[name], out["tail"][name] = _median_model(b[name], flat)
        # a flat stage is never fitted: FIT_B_RANGE floors the slope at 0.8,
        # so a fit cannot say "flat" and inflated a 4.5 h meeting's writing
        # estimate about 5x over its median on the real rates.jsonl
        out["fit"][name] = None if flat else _fit(b[name])
        if out["fit"][name]:
            out["tail"][name] = out["fit"][name]["tail"]
    for group in ("asr", "diarize"):
        for k, pairs in b[group].items():
            out[group][k], out["tail"][group][k] = _median_model(pairs)
            f = _fit(pairs)
            if f:
                out["fit"][group][k] = f
                out["tail"][group][k] = f["tail"]
    return out


def learned() -> dict:
    """{"convert": rate, "asr": {"parakeet@1": rate, ...}, "diarize": {"1": rate},
    "writing": secs, "runs": n, "fit": {...}, "tail": {...}} — see _learn.
    Cached until rates.jsonl changes."""
    try:
        mtime = RATES_LOG.stat().st_mtime if RATES_LOG.exists() else 0
    except OSError:
        mtime = 0
    # the engine is part of the key: building the helper (or losing its
    # models) changes which diarize samples apply without touching the log
    sig = (mtime, effective_diarizer())
    if _cache["sig"] == sig and _cache["learned"] is not None:
        return _cache["learned"]
    out = _learn(_rows())
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
    base = L.get("1") or _est_diarize_rate()
    return base / (CONTENTION if n_active > 1 else 1.0)  # measured contention fallback


def writing_secs() -> float:
    return learned()["writing"] or config.EST_RATES["writing_fixed_sec"]


def _pick(L, stage, n_active, asr_key):
    """(fit, median, tail, scale) for one stage: the model to use and a
    multiplier for the one derived case (2 workers diarizing, unmeasured)."""
    n = str(n_active)
    if stage in ("converting", "writing"):
        k = "convert" if stage == "converting" else "writing"
        return L["fit"].get(k), L[k], L["tail"].get(k, DEFAULT_TAIL), 1.0
    if stage == "diarizing":
        if n in L["diarize"]:
            return (L["fit"]["diarize"].get(n), L["diarize"][n],
                    L["tail"]["diarize"].get(n, DEFAULT_TAIL), 1.0)
        scale = CONTENTION if n_active > 1 else 1.0
        return (L["fit"]["diarize"].get("1"), L["diarize"].get("1"),
                L["tail"]["diarize"].get("1", DEFAULT_TAIL), scale)
    # transcribing (and verifying, which callers pass as the secondary engine)
    for k in (f"{asr_key}@{n}", f"{asr_key}@1"):
        if k in L["asr"]:
            return (L["fit"]["asr"].get(k), L["asr"][k],
                    L["tail"]["asr"].get(k, DEFAULT_TAIL), 1.0)
    return None, None, DEFAULT_TAIL, 1.0


def _default(stage, duration, asr_key):
    if stage == "converting":
        return duration / config.EST_RATES["convert"]
    if stage == "writing":
        return config.EST_RATES["writing_fixed_sec"]
    if stage == "diarizing":
        return duration / _est_diarize_rate()
    return duration / config.EST_RATES["asr"].get(asr_key, 30.0)


def predict(L, stage, duration, n_active=1, asr_key=None, use_fit=True) -> float:
    """Expected wall seconds of `stage` for `duration` seconds of audio, from
    the model `L` (see _learn): the power law when it is well posed and
    `use_fit`, else the median rate (use_fit=False is the old model, kept for
    tools/eta_backtest.py), else the config default."""
    fit, med, _, scale = _pick(L, stage, n_active, asr_key)
    if use_fit and fit:
        return math.exp(fit["la"]) * duration ** fit["b"] * scale
    if med:
        secs = med if stage == "writing" else duration / med
        return secs * scale
    return _default(stage, duration, asr_key)


def stage_secs(stage, duration, n_active=1, asr_key=None) -> float:
    """predict() against the learned model; asr_key defaults to the
    currently selected ASR model."""
    return predict(learned(), stage, duration, n_active,
                   asr_key or current_asr_key())


def stage_tail(stage, n_active=1, asr_key=None) -> float:
    """The overrun fraction for `stage` (see _tail), from the model
    stage_secs uses."""
    return _pick(learned(), stage, n_active, asr_key or current_asr_key())[2]


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

"""Channel-aware helpers for stereo virtual-meeting recordings.

The meeting recorder writes L = mic ("me"), R = system ("them"). Diarizing the
system channel alone removes the me-vs-them overlap that hurts a single mixed
track; the mic channel then tells us where "me" spoke. But the mic channel is
only clean on a headphones day: with speakers, the mic ALSO picks up the others
through the speakers (bleed), where it is an ATTENUATED copy of the system
channel, not louder than it. So "me is speaking" is decided by energy
DOMINANCE (mic RMS beats system RMS by a margin), not mere mic activity — that
ratio is what separates a real me-turn from bleed.

Pure numpy + soundfile; no torch, no models. Every threshold comes from config
so it can be tuned against real recordings.
"""
import numpy as np
import soundfile as sf

from . import config

MIC_ID = "MIC_ME"  # synthetic cluster id for the mic speaker's overlaid turns


def _read_mono(path):
    data, sr = sf.read(str(path))
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    return np.asarray(data, dtype=np.float64), sr


def _frame_db(x, sr, win_ms=30, hop_ms=10):
    """Per-frame RMS in dBFS, vectorized via a cumulative sum of squares.
    Returns (frame_start_times, db, hop_seconds)."""
    win = max(1, int(sr * win_ms / 1000))
    hop = max(1, int(sr * hop_ms / 1000))
    if len(x) < win:
        rms = np.sqrt(np.mean(x * x)) if len(x) else 0.0
        db = 20 * np.log10(rms) if rms > 1e-9 else -120.0
        return np.array([0.0]), np.array([db]), hop / sr
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - win + 1, hop)
    ms = (cs[starts + win] - cs[starts]) / win
    rms = np.sqrt(np.maximum(ms, 0.0))
    db = np.where(rms > 1e-9, 20 * np.log10(np.maximum(rms, 1e-12)), -120.0)
    return starts / sr, db, hop / sr


def _spans_from_active(active, times, hop, enter_ms, exit_ms, bridge_ms, min_span_ms):
    """Hysteresis state machine over a boolean per-frame `active` mask ->
    coalesced [(start, end)] spans. Enter only after the predicate holds
    enter_ms; leave only after it fails exit_ms; then bridge tiny gaps and drop
    tiny spans."""
    enter_f = max(1, int(enter_ms / 1000 / hop))
    exit_f = max(1, int(exit_ms / 1000 / hop))
    spans, on, run_on, run_off, start_i = [], False, 0, 0, 0
    for i, a in enumerate(active):
        if not on:
            run_on = run_on + 1 if a else 0
            if run_on >= enter_f:
                on = True
                start_i = i - run_on + 1
                run_off = 0
        else:
            run_off = run_off + 1 if not a else 0
            if run_off >= exit_f:
                on = False
                spans.append((start_i, i - run_off + 1))
                run_on = 0
    if on:
        spans.append((start_i, len(active)))
    # frame indices -> seconds (each frame spans `hop`)
    secs = [(times[s] if s < len(times) else times[-1] + hop,
             (times[e - 1] + hop) if 0 < e <= len(times) else times[-1] + hop)
            for s, e in spans]
    # bridge short gaps, then drop short spans
    bridged = []
    for sp in secs:
        if bridged and sp[0] - bridged[-1][1] <= bridge_ms / 1000:
            bridged[-1] = (bridged[-1][0], sp[1])
        else:
            bridged.append(list(sp))
    # plain python floats: these spans become turn timestamps in the meeting
    # JSON, and numpy scalars are not JSON serializable
    return [(round(float(s), 3), round(float(e), 3)) for s, e in bridged
            if (e - s) >= min_span_ms / 1000]


def mic_spans(mic_path, sys_path):
    """[(start, end)] where the mic owner dominates the system channel — the
    candidate "me is speaking" turns, before the voiceprint safety net."""
    mic, sr = _read_mono(mic_path)
    sysd, _ = _read_mono(sys_path)
    n = min(len(mic), len(sysd))
    mic, sysd = mic[:n], sysd[:n]
    tm, mdb, hop = _frame_db(mic, sr)
    _, sdb, _ = _frame_db(sysd, sr)
    k = min(len(mdb), len(sdb))
    active = (mdb[:k] > config.CHANNEL_FLOOR_DBFS) & \
             ((mdb[:k] - sdb[:k]) >= config.CHANNEL_DOMINANCE_DB)
    return _spans_from_active(active, tm[:k], hop,
                              config.CHANNEL_ENTER_MS, config.CHANNEL_EXIT_MS,
                              config.CHANNEL_BRIDGE_MS, config.CHANNEL_MIN_SPAN_MS)


def sanity(mic_path, sys_path):
    """Whole-file checks that decide whether the split is even real. Returns
    {dual_mono, mic_dead, sys_dead, mic_rms_db, sys_rms_db}."""
    mic, _ = _read_mono(mic_path)
    sysd, _ = _read_mono(sys_path)
    n = min(len(mic), len(sysd))
    mic, sysd = mic[:n], sysd[:n]

    def _db(x):
        r = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        # float(): np.log10 hands back an np.float64, and every value built on
        # it (the < comparisons become np.bool_) lands in the meeting JSON as
        # channel_stats — where json.dumps refuses numpy scalars. The first
        # real stereo recordings failed the whole pipeline on exactly that.
        return float(20 * np.log10(r)) if r > 1e-9 else -120.0

    mdb, sdb = _db(mic), _db(sysd)
    # dual-mono = the two "channels" are the same signal (a mono file dressed as
    # stereo, or a recorder bug): normalized correlation ~1 and near-zero diff
    dual = False
    if n and np.linalg.norm(mic) > 0 and np.linalg.norm(sysd) > 0:
        corr = float(np.dot(mic, sysd) / (np.linalg.norm(mic) * np.linalg.norm(sysd)))
        diff = float(np.mean(np.abs(mic - sysd)) / (np.mean(np.abs(mic)) + 1e-9))
        dual = corr > 0.98 and diff < 0.05
    return {"dual_mono": bool(dual),
            "mic_dead": bool(mdb < config.CHANNEL_FLOOR_DBFS),
            "sys_dead": bool(sdb < config.CHANNEL_FLOOR_DBFS),
            "mic_rms_db": round(mdb, 1), "sys_rms_db": round(sdb, 1)}


def _subtract(span, intervals):
    """(kept, covered): parts of `span` NOT covered by any interval, and the
    covered parts. Used to yield the mic owner words only where the system
    channel is quiet, sending true double-talk to the system turn + review."""
    s, e = span
    kept, covered, cur = [], [], s
    for a, b in sorted(intervals):
        if b <= cur or a >= e:
            continue
        a, b = max(a, s), min(b, e)
        if a > cur:
            kept.append((cur, a))
        covered.append((max(cur, a), b))
        cur = max(cur, b)
    if cur < e:
        kept.append((cur, e))
    return kept, covered


def combine_turns(sys_turns, names, spans, mic_name, mean_score, min_dur=0.15):
    """Overlay mic-owner turns onto the system-channel turns.

    Where a mic span overlaps an active system turn (both talking), the system
    turn keeps the words and the overlap is flagged for review; the mic owner
    only wins words where the system channel was quiet. Returns
    (turns, names, extra_overlaps) ready for merge.assign_and_group; the mic
    speaker rides through as the synthetic id MIC_ID named `mic_name`.
    """
    sys_intervals = [(t["start"], t["end"]) for t in sys_turns]
    mic_turns, extra_overlaps = [], []
    for span in spans:
        kept, covered = _subtract(span, sys_intervals)
        for a, b in kept:
            if b - a >= min_dur:
                mic_turns.append({"start": a, "end": b, "speaker": MIC_ID})
        extra_overlaps += covered
    turns = sorted(sys_turns + mic_turns, key=lambda t: t["start"])
    names = dict(names)
    if mic_turns:
        names[MIC_ID] = {"name": mic_name, "score": mean_score, "display": mic_name}
    extra_overlaps = [(round(s, 3), round(e, 3)) for s, e in extra_overlaps if e > s]
    return turns, names, extra_overlaps

#!/usr/bin/env python
"""Replay rates.jsonl to compare the ETA models, the way the panel meets them.

For every sample, in time order, predict each stage's seconds from ONLY the
samples before it, with the old model (one median rate per stage) and the new
one (a power law of the audio duration, stt.rates._fit), then score both
against what the file actually took. Prints the mean absolute percentage
error for the whole file and for diarizing, over every file and over files of
90 minutes or more.

--midrun scores the countdown the panel shows DURING a file instead: each
file is replayed at 10, 50 and 90 percent of its real run time through
status.progress_detail (the panel's own code), with each stage reporting
progress in step with the clock, as Qwen3-ASR now does per chunk. It
prints the error of the time left, as a percent of the file's real total
(positive: the panel says more time is left than there is), per ASR
engine, once with the config defaults only (a first run) and once with
the samples before each file (learned).

Read-only: it opens rates.jsonl and nothing else.

    ./run.sh py tools/eta_backtest.py              # the project's rates.jsonl
    ./run.sh py tools/eta_backtest.py path/to/rates.jsonl
    ./run.sh py tools/eta_backtest.py --midrun path/to/rates.jsonl
"""
import argparse
import contextlib
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import rates, verify  # noqa: E402

LONG_SEC = 90 * 60


def predict_row(L, row, use_fit):
    """{stage: predicted seconds} for the stages this row actually ran,
    predicted the way status.stage_estimates does."""
    d, n, asr = row["duration"], int(row.get("n", 1)), row.get("asr", "parakeet")
    out = {}
    for stage in row["secs"]:
        if stage == "downloading":
            out[stage] = 5.0                    # a constant in both models
        elif stage == "verifying":
            # no second engine: status.stage_estimates budgets no verifying
            second = verify.secondary_engine(asr)
            if second:
                out[stage] = rates.predict(L, "transcribing", d, n, second,
                                           use_fit=use_fit)
        else:
            out[stage] = rates.predict(L, stage, d, n, asr, use_fit=use_fit)
    return out


def replay(rows):
    """One record per sample: its duration and the actual and predicted
    seconds (old and new) for the total and for diarizing."""
    recs = []
    for i, row in enumerate(rows):
        if not row.get("duration") or not row.get("secs"):
            continue
        # each row is predicted from the engine that actually ran it
        L = rates._learn(rows[:i], diarizer=row.get("diar", "pyannote"))
        rec = {"duration": row["duration"],
               "actual": sum(row["secs"].values()),
               "diar": row["secs"].get("diarizing")}
        for name, use_fit in (("old", False), ("new", True)):
            p = predict_row(L, row, use_fit)
            rec[name] = sum(p.values())
            rec[name + "_diar"] = p.get("diarizing")
        recs.append(rec)
    return recs


def mape(pairs):
    pairs = [(a, p) for a, p in pairs if a and p is not None]
    if not pairs:
        return None, 0
    return 100.0 * sum(abs(p - a) / a for a, p in pairs) / len(pairs), len(pairs)


def report(recs):
    lines = []
    for label, keep in (("all files", lambda r: True),
                        ("files >= 90 min", lambda r: r["duration"] >= LONG_SEC)):
        sub = [r for r in recs if keep(r)]
        for what, act, suffix in (("total", "actual", ""), ("diarizing", "diar", "_diar")):
            old, n = mape([(r[act], r["old" + suffix]) for r in sub])
            new, _ = mape([(r[act], r["new" + suffix]) for r in sub])
            if old is None:
                lines.append(f"{label:16} {what:10} n={n:<4} (no samples)")
                continue
            lines.append(f"{label:16} {what:10} n={n:<4} old MAPE {old:6.1f}%   "
                         f"new MAPE {new:6.1f}%")
    return lines


POINTS = (0.1, 0.5, 0.9)
_ORDER = ("downloading", "converting", "transcribing", "diarizing", "writing")


class _Clock:
    """Stands in for status._time: a fixed monotonic and wall clock."""

    def __init__(self, mono, wall):
        self._m, self._w = mono, wall

    def monotonic(self):
        return self._m

    def time(self):
        return self._w


@contextlib.contextmanager
def _as_of(L, asr, diar, clock):
    """The panel's view at one moment: model L, ASR engine asr, speaker
    engine diar, and the given clock."""
    from stt import status
    saved = (rates.learned, rates.current_asr_key, rates.effective_diarizer, status._time)
    rates.learned = lambda: L
    rates.current_asr_key = lambda: asr
    rates.effective_diarizer = lambda: diar
    status._time = clock
    try:
        yield status
    finally:
        (rates.learned, rates.current_asr_key, rates.effective_diarizer,
         status._time) = saved


def entry_at(row, t, mono=1000.0, wall=2.0e9):
    """The live status entry of `row`'s file `t` seconds after it started,
    with every stage reporting progress in step with the clock."""
    secs = row["secs"]
    start = 0.0
    for stage in _ORDER:
        dur = float(secs.get(stage, 0.0))
        if dur <= 0:
            continue
        if t < start + dur or stage == "writing":
            e = min(dur, max(0.0, t - start))
            return {"stage": stage, "duration": row["duration"],
                    "progress": round(e / dur, 3), "progress_at": wall,
                    "stage_since": mono - e, "diarize": "diarizing" in secs,
                    "verify": False}
        start += dur
    return None


def midrun(rows, learned=True, points=POINTS):
    """[{asr, duration, point, err_pct}] for every row and point."""
    out = []
    for i, row in enumerate(rows):
        secs = {k: v for k, v in (row.get("secs") or {}).items() if k in _ORDER}
        if not row.get("duration") or not secs.get("transcribing"):
            continue
        row = {**row, "secs": secs}
        total = sum(secs.values())
        L = rates._learn(rows[:i] if learned else [], diarizer=row.get("diar", "pyannote"))
        for f in points:
            t = f * total
            entry = entry_at(row, t)
            with _as_of(L, row.get("asr", "parakeet"), rates.diarizer_family(row.get("diar")),
                        _Clock(1000.0, 2.0e9)) as st:
                d = st.progress_detail(entry, int(row.get("n", 1)))
            if d is None:
                continue
            out.append({"asr": row.get("asr", "parakeet"), "duration": row["duration"],
                        "point": f, "err_pct": 100.0 * (d["eta"] - (total - t)) / total})
    return out


def midrun_report(rows):
    lines = []
    for label, learned in (("defaults only", False), ("learned", True)):
        recs = midrun(rows, learned=learned)
        for asr in sorted({r["asr"] for r in recs}):
            parts = []
            for f in POINTS:
                e = [r["err_pct"] for r in recs if r["asr"] == asr and r["point"] == f]
                if e:
                    parts.append(f"{int(f * 100)}%: median {statistics.median(e):+6.1f}% "
                                 f"(abs {statistics.median(abs(x) for x in e):5.1f}%)")
            n = len({(r["duration"], r["asr"]) for r in recs if r["asr"] == asr})
            lines.append(f"{label:14} {asr:10} n={n:<3} " + "   ".join(parts))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(rates.RATES_LOG))
    ap.add_argument("--midrun", action="store_true",
                    help="score the countdown at 10, 50 and 90 percent through each file")
    args = ap.parse_args(argv)
    # rates' own reader: a line that is JSON but not a usable row is skipped
    rows = rates.parse_rows(Path(args.path).read_text(errors="replace").splitlines())
    rows.sort(key=lambda r: r.get("at") or "")   # time order, stable for ties
    if args.midrun:
        print(f"{args.path}: time-left error as a percent of each file's real total "
              "(positive: the panel says more is left)")
        for ln in midrun_report(rows):
            print(ln)
        return 0
    recs = replay(rows)
    print(f"{args.path}: {len(recs)} samples, "
          f"{sum(r['duration'] >= LONG_SEC for r in recs)} of 90 min or more")
    for ln in report(recs):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())

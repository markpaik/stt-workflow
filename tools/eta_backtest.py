#!/usr/bin/env python
"""Replay rates.jsonl to compare the ETA models, the way the panel meets them.

For every sample, in time order, predict each stage's seconds from ONLY the
samples before it, with the old model (one median rate per stage) and the new
one (a power law of the audio duration, stt.rates._fit), then score both
against what the file actually took. Prints the mean absolute percentage
error for the whole file and for diarizing, over every file and over files of
90 minutes or more.

Read-only: it opens rates.jsonl and nothing else.

    ./run.sh py tools/eta_backtest.py              # the project's rates.jsonl
    ./run.sh py tools/eta_backtest.py path/to/rates.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import rates  # noqa: E402

LONG_SEC = 90 * 60


def _secondary(asr):
    return "mlxwhisper:turbo" if asr == "parakeet" else "parakeet"


def predict_row(L, row, use_fit):
    """{stage: predicted seconds} for the stages this row actually ran,
    predicted the way status.stage_estimates does."""
    d, n, asr = row["duration"], int(row.get("n", 1)), row.get("asr", "parakeet")
    out = {}
    for stage in row["secs"]:
        if stage == "downloading":
            out[stage] = 5.0                    # a constant in both models
        elif stage == "verifying":
            out[stage] = rates.predict(L, "transcribing", d, n, _secondary(asr),
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
        L = rates._learn(rows[:i])
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(rates.RATES_LOG))
    args = ap.parse_args(argv)
    rows = []
    for ln in Path(args.path).read_text().splitlines():
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: r.get("at") or "")   # time order, stable for ties
    recs = replay(rows)
    print(f"{args.path}: {len(recs)} samples, "
          f"{sum(r['duration'] >= LONG_SEC for r in recs)} of 90 min or more")
    for ln in report(recs):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())

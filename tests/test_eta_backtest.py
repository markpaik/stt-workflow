"""tools/eta_backtest.py: replays a rates log in time order and scores the old
(median rate) and new (power law) ETA models on what came before each file."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import eta_backtest  # noqa: E402


def _log(path, durs):
    """Diarization whose cost grows faster than the audio (b = 1.2), mostly
    short meetings and a few board meetings, like the real log."""
    rows = []
    for i, d in enumerate(durs):
        rows.append({"at": f"2026-09-{1 + i // 10:02d}T{i % 10:02d}:00:00",
                     "duration": d, "asr": "parakeet", "n": 1,
                     "secs": {"converting": d / 900, "transcribing": d / 60,
                              "diarizing": 0.2 * d ** 1.2, "writing": 15.0}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_backtest_scores_both_models_on_what_came_before(sandbox, tmp_path, capsys):
    p = tmp_path / "rates.jsonl"
    short = [1200, 1500, 1800, 2400, 2700, 3000, 3300, 3600]
    _log(p, short * 3 + [10800, 1800, 16200, 2400, 12600])
    assert eta_backtest.main([str(p)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("29 samples, 3 of 90 min or more")
    assert len(out) == 5      # the header, then total and diarizing, all and long
    long_total = next(ln for ln in out if ln.startswith("files >= 90 min  total"))
    old = float(long_total.split("old MAPE")[1].split("%")[0])
    new = float(long_total.split("new MAPE")[1].split("%")[0])
    assert new < old / 3, long_total


def test_each_prediction_sees_only_the_samples_before_it(sandbox, tmp_path):
    """The first file has no history: both models fall back to the config
    defaults, so they agree on it exactly. Replay never peeks ahead."""
    p = tmp_path / "rates.jsonl"
    _log(p, [1200, 3600])
    rows = [json.loads(ln) for ln in p.read_text().splitlines()]
    recs = eta_backtest.replay(rows)
    assert recs[0]["old"] == recs[0]["new"]
    # with one sample behind it the fit is not posed yet: still the median
    assert recs[1]["old"] == recs[1]["new"]

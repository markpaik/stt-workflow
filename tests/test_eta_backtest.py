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
                     "duration": d, "asr": "qwen3", "n": 1,
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


# ---------- --midrun: the countdown during a file ----------

def _row(asr, d, asr_rate, diar_rate, at="2026-11-11T10:00:00"):
    return {"at": at, "duration": d, "asr": asr, "n": 1, "diar": "speakrs",
            "secs": {"transcribing": d / asr_rate, "diarizing": d / diar_rate,
                     "writing": 20.0}}


def test_the_midrun_entry_follows_the_stages_in_order():
    row = _row("qwen3", 3600, 16.0, 110.0)
    e = eta_backtest.entry_at(row, 100.0)
    assert e["stage"] == "transcribing" and e["progress"] == round(100 / 225, 3)
    e = eta_backtest.entry_at(row, 225.0 + 10)
    assert e["stage"] == "diarizing" and e["stage_since"] == 1000.0 - 10
    assert eta_backtest.entry_at(row, 1e6)["stage"] == "writing"


def test_a_file_that_runs_at_the_default_rates_counts_down_exactly(sandbox, monkeypatch):
    """With the config defaults and a file that runs at exactly those rates,
    the time left the panel shows is the real time left, at every point."""
    from stt import config
    monkeypatch.setitem(config.EST_RATES, "writing_fixed_sec", 20.0)
    asr, diar = config.EST_RATES["asr"]["qwen3"], config.EST_RATES["diarize"]["speakrs"]
    row = _row("qwen3", 3600, asr, diar)
    row["secs"]["downloading"] = 5.0            # the panel budgets 5 s for it
    recs = eta_backtest.midrun([row], learned=False)
    assert [r["point"] for r in recs] == [0.1, 0.5, 0.9]
    assert all(abs(r["err_pct"]) < 0.5 for r in recs), recs


def test_a_slower_file_shows_too_little_time_left_and_the_report_names_each_engine(
        sandbox, capsys, tmp_path):
    from stt import config
    slow = _row("qwen3", 3600, config.EST_RATES["asr"]["qwen3"] / 2,
                config.EST_RATES["diarize"]["speakrs"])
    recs = eta_backtest.midrun([slow], learned=False)
    assert recs[0]["err_pct"] < -10            # half the speed: the panel is short
    p = tmp_path / "rates.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in (
        _row("cloud:scribe", 3600, 80, 110, at="2026-11-11T10:00:00"),
        _row("qwen3", 3600, 16, 110, at="2026-11-11T11:00:00"),
        _row("qwen3", 7200, 16, 110, at="2026-11-11T12:00:00"))))
    assert eta_backtest.main(["--midrun", str(p)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [ln.split()[:3] for ln in out[1:]] == [
        ["defaults", "only", "cloud:scribe"], ["defaults", "only", "qwen3"],
        ["learned", "cloud:scribe", "n=1"], ["learned", "qwen3", "n=2"]]

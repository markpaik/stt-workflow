"""rates: auto-calibration from no data -> some data -> outliers -> cache."""
from stt import config, rates, status


def _run(spd=25.0, dspd=2.0, dur=1800, asr="parakeet", n=1):
    rates.record(dur, {"converting": dur / 75, "transcribing": dur / spd,
                       "diarizing": dur / dspd, "writing": 18}, asr, n)


def _reset_cache():
    rates._cache.update(sig=None, learned=None)


def test_no_data_uses_config_defaults(sandbox):
    assert rates.asr_rate("parakeet", 1) == config.EST_RATES["asr"]["parakeet"]
    assert rates.diarize_rate(1) == config.EST_RATES["diarize"]
    assert rates.writing_secs() == config.EST_RATES["writing_fixed_sec"]


def test_short_audio_not_learned(sandbox):
    rates.record(60, {"transcribing": 2}, "parakeet", 1)
    _reset_cache()
    assert rates.learned()["runs"] == 0


def test_empty_or_zero_stages_not_learned(sandbox):
    rates.record(1800, {}, "parakeet", 1)
    rates.record(1800, {"transcribing": 0.0}, "parakeet", 1)
    _reset_cache()
    assert rates.learned()["runs"] == 0


def test_median_rejects_outlier(sandbox):
    for spd in (25, 26, 24, 2, 25):  # one sleep-corrupted outlier
        _run(spd=spd)
    _reset_cache()
    assert 24 <= rates.asr_rate("parakeet", 1) <= 26


def test_models_learn_independently(sandbox):
    _run(spd=25, asr="parakeet")
    _run(spd=4.0, asr="mlxwhisper:large-v3")
    _reset_cache()
    assert rates.asr_rate("parakeet", 1) == 25
    assert rates.asr_rate("mlxwhisper:large-v3", 1) == 4.0
    # unknown model still falls back to config
    assert rates.asr_rate("mlxwhisper:turbo", 1) == config.EST_RATES["asr"]["mlxwhisper:turbo"]


def test_parallel_contention_fallback_and_learning(sandbox):
    _run(dspd=2.0, n=1)
    _reset_cache()
    # no @2 data yet -> derived from @1 with the 1.4 contention factor
    assert abs(rates.diarize_rate(2) - 2.0 / 1.4) < 0.01
    _run(dspd=1.5, n=2)
    _reset_cache()
    assert rates.diarize_rate(2) == 1.5  # measured beats derived


def test_cache_invalidates_on_new_data(sandbox):
    _run(spd=25)
    _reset_cache()
    assert rates.asr_rate("parakeet", 1) == 25
    import os, time
    time.sleep(0.02)
    for _ in range(9):
        _run(spd=10)
    os.utime(rates.RATES_LOG)
    assert rates.asr_rate("parakeet", 1) == 10  # mtime change busts the cache


def test_corrupt_line_skipped(sandbox):
    _run(spd=25)
    with open(rates.RATES_LOG, "a") as f:
        f.write("{corrupt\n")
    _run(spd=25)
    _reset_cache()
    assert rates.learned()["runs"] == 2


def test_stage_estimates_consume_learned(sandbox, monkeypatch):
    _run(spd=20, dspd=2.5)
    _reset_cache()
    monkeypatch.setattr(rates, "current_asr_key", lambda: "parakeet")
    est = status.stage_estimates(3600, 1)
    assert abs(est["transcribing"] - 3600 / 20) < 1
    assert abs(est["diarizing"] - 3600 / 2.5) < 1
    assert est["writing"] == 18


def test_summary_shape(sandbox):
    assert rates.summary() == {"runs": 0, "text": ""}
    _run()
    _reset_cache()
    s = rates.summary()
    assert s["runs"] == 1 and "Parakeet" in s["text"]

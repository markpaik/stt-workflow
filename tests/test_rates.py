"""rates: auto-calibration from no data -> some data -> outliers -> cache."""
from stt import config, rates, status


def _run(spd=25.0, dspd=2.0, dur=1800, asr="parakeet", n=1):
    rates.record(dur, {"converting": dur / 75, "transcribing": dur / spd,
                       "diarizing": dur / dspd, "writing": 18}, asr, n)


def _reset_cache():
    rates._cache.update(sig=None, learned=None)


def test_no_data_uses_config_defaults(sandbox):
    assert rates.asr_rate("parakeet", 1) == config.EST_RATES["asr"]["parakeet"]
    assert rates.diarize_rate(1) == config.EST_RATES["diarize"][rates.effective_diarizer()]
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


# ---- the power law (A1): diarization's cost grows faster than the audio ----

def _diar_samples(durs, b=1.25, a=0.1, jitter=None):
    """One rate sample per duration whose diarizing seconds follow
    a * duration^b (times an optional per-sample factor)."""
    for i, d in enumerate(durs):
        f = jitter[i] if jitter else 1.0
        rates.record(d, {"transcribing": d / 60, "diarizing": a * d ** b * f,
                         "writing": 10}, "parakeet", 1)


def test_diarizing_follows_a_power_law_so_long_meetings_are_not_short_changed(
        sandbox, monkeypatch):
    """The panel estimated a 4.5 h board meeting about 25 percent short: one
    median rate, taken mostly from short meetings, cannot describe a stage
    whose cost grows faster than the audio. With samples that span the
    lengths, the estimate follows secs = a * duration^b instead."""
    monkeypatch.setattr(rates, "current_asr_key", lambda: "parakeet")
    _diar_samples([1200, 1500, 1800, 2400, 2700, 3000, 3600, 10800])
    long = 4.5 * 3600
    truth = 0.1 * long ** 1.25
    est = status.stage_estimates(long)["diarizing"]
    assert abs(est - truth) / truth < 0.02
    # the old model (one median rate) is what left it short
    old = long / rates.diarize_rate(1)
    assert old < 0.8 * truth
    f = rates.learned()["fit"]["diarize"]["1"]
    assert abs(f["b"] - 1.25) < 0.01 and f["n"] == 8
    # the summary the settings row reads keeps its shape
    assert set(rates.summary()) == {"runs", "text"}


def test_the_fit_needs_six_samples_spanning_a_factor_of_two(sandbox, monkeypatch):
    """A slope read off too few samples, or off a narrow band of lengths, is
    noise: the estimate stays on the median rate until both hold."""
    monkeypatch.setattr(rates, "current_asr_key", lambda: "parakeet")
    _diar_samples([1200, 1800, 2400, 3000, 3600])            # five samples
    assert "1" not in rates.learned()["fit"]["diarize"]
    med = rates.diarize_rate(1)
    assert abs(status.stage_estimates(7200)["diarizing"] - 7200 / med) < 1

    _reset_cache()
    rates.RATES_LOG.unlink()
    _diar_samples([2000, 2200, 2400, 2600, 2800, 3000, 3200, 3400])   # span 1.7
    assert "1" not in rates.learned()["fit"]["diarize"]
    med = rates.diarize_rate(1)
    assert abs(status.stage_estimates(7200)["diarizing"] - 7200 / med) < 1

    _reset_cache()
    _diar_samples([7000])          # the ninth sample widens the span past 2
    assert "1" in rates.learned()["fit"]["diarize"]


def test_the_exponent_is_clamped(sandbox):
    """A handful of odd files must not bend the curve into nonsense: b stays
    in [0.8, 1.6], and the intercept is refitted for the clamped slope."""
    _diar_samples([600, 900, 1200, 1800, 2400, 3600], b=2.5, a=1e-5)
    assert rates.learned()["fit"]["diarize"]["1"]["b"] == 1.6
    _reset_cache()
    rates.RATES_LOG.unlink()
    _diar_samples([600, 900, 1200, 1800, 2400, 3600], b=0.3, a=50)
    f = rates.learned()["fit"]["diarize"]["1"]
    assert f["b"] == 0.8
    # refitted intercept: the prediction passes through the middle of the data
    mid = rates.predict(rates.learned(), "diarizing", 1470, 1, "parakeet")
    assert 0.5 < mid / (50 * 1470 ** 0.3) < 2.0


def test_the_fit_uses_only_the_most_recent_samples(sandbox):
    """The machine changes (a new model, a new OS): the fit follows the last
    FIT_SAMPLES samples, not the whole history."""
    _diar_samples([600, 900, 1200, 1800, 2400, 3600] * 5, b=1.0, a=5.0)   # old, slow
    _diar_samples([600, 900, 1200, 1800, 2400, 3600] * 4, b=1.0, a=1.0)   # the last 24
    got = rates.predict(rates.learned(), "diarizing", 3600, 1, "parakeet")
    assert abs(got - 3600) < 1


def test_each_model_carries_a_tail_from_its_own_residuals(sandbox):
    """The tail is how far past its prediction a stage tends to run: the 80th
    percentile of actual over predicted, minus one, floored. Never one
    constant for every stage and every machine."""
    assert rates.stage_tail("diarizing", 1, "parakeet") == rates.DEFAULT_TAIL
    # every sample 1.0x except two slow ones: the 80th percentile sits in them
    jit = [1.0] * 8 + [1.5, 1.5]
    _diar_samples([600, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000, 3600],
                  b=1.0, a=1.0, jitter=jit)
    t = rates.stage_tail("diarizing", 1, "parakeet")
    assert rates.TAIL_RANGE[0] < t < 0.5, t
    # a stage that always lands on its prediction still gets the floor
    assert rates.stage_tail("transcribing", 1, "parakeet") == rates.TAIL_RANGE[0]


def test_writing_stays_flat_so_long_meetings_do_not_inflate_it(
        sandbox, monkeypatch):
    """Writing is merge + punctuation + output, roughly flat by design
    (config writing_fixed_sec). The power law's slope is floored at 0.8, so
    it cannot say "flat": fitted, a 4.5 h meeting's writing estimate came out
    near five times the median on the real rates.jsonl. Writing keeps its
    median seconds however long the meeting, while diarizing on the same
    samples still gets its curve."""
    monkeypatch.setattr(rates, "current_asr_key", lambda: "parakeet")
    durs = [600, 1200, 1800, 2400, 3000, 3600, 10800]
    for d, w in zip(durs, [9, 11, 10, 12, 8, 10, 11]):
        rates.record(d, {"transcribing": d / 60, "diarizing": 0.1 * d ** 1.25,
                         "writing": w}, "parakeet", 1)
    L = rates.learned()
    assert "1" in L["fit"]["diarize"]          # the fit still runs where it fits
    assert not L["fit"].get("writing")
    long = 4.5 * 3600
    for dur in (600, 3600, long):
        assert rates.predict(L, "writing", dur, 1, "parakeet") == 10
        assert status.stage_estimates(dur)["writing"] == 10
    # its tail comes from its own flat residuals, not a fit's
    assert rates.stage_tail("writing", 1, "parakeet") == L["tail"]["writing"]
    assert L["tail"]["writing"] == rates._median_model(
        rates._samples(rates._rows())["writing"], flat=True)[1]

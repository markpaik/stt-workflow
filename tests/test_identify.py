"""identify: enrollment lifecycle + open-set matching from every state —
empty library, one person, near-duplicate voices, strangers, corrupt vectors."""
import numpy as np
import pytest

from stt import identify


def _vec(seed, dim=256):
    return np.random.default_rng(seed).normal(size=dim)


def test_empty_library_names_nobody(sandbox):
    res = identify.name_speakers({"SPEAKER_00": _vec(1)})
    assert res["SPEAKER_00"]["name"] is None


def test_enroll_and_match(sandbox):
    identify.enroll("Mark", _vec(1))
    res = identify.name_speakers({"S0": _vec(1) + 0.05 * _vec(9)}, threshold=0.6, margin=0.1)
    assert res["S0"]["name"] == "Mark"
    assert res["S0"]["score"] > 0.9


def test_stranger_stays_unknown(sandbox):
    identify.enroll("Mark", _vec(1))
    res = identify.name_speakers({"S0": _vec(2)}, threshold=0.6, margin=0.1)
    assert res["S0"]["name"] is None


def test_margin_blocks_ambiguous_match(sandbox):
    """Two enrolled voices nearly identical: neither may claim a matching speaker
    without a clear margin — open-set safety."""
    base = _vec(1)
    identify.enroll("A", base)
    identify.enroll("B", base + 0.01 * _vec(3))
    res = identify.name_speakers({"S0": base}, threshold=0.6, margin=0.15)
    assert res["S0"]["name"] is None  # ambiguous -> no guess


def test_greedy_uniqueness(sandbox):
    identify.enroll("Mark", _vec(1))
    # two clusters both resembling Mark: only ONE gets the name
    res = identify.name_speakers({"S0": _vec(1), "S1": _vec(1) + 0.02 * _vec(4)},
                                 threshold=0.5, margin=0.0)
    named = [k for k, v in res.items() if v["name"] == "Mark"]
    assert len(named) == 1


def test_multi_sample_rolling_window(sandbox):
    for i in range(identify.MAX_SAMPLES + 3):
        identify.enroll("Mark", _vec(i))
    reg = identify.load_registry()
    assert reg["Mark"]["n_samples"] == identify.MAX_SAMPLES
    assert identify.load_voiceprints()["Mark"].shape[0] == identify.MAX_SAMPLES


def test_legacy_1d_voiceprint_still_loads(sandbox):
    from stt import config
    identify.enroll("Old", _vec(5))
    # rewrite the file in the legacy single-centroid 1-D format
    f = config.VOICEPRINTS_DIR / "Old.npy"
    np.save(f, _vec(5))
    vp = identify.load_voiceprints()["Old"]
    assert vp.ndim == 2 and vp.shape[0] == 1


def test_zero_vector_rejected(sandbox):
    with pytest.raises(ValueError):
        identify.enroll("Bad", np.zeros(256))


def test_nan_cosine_never_matches(sandbox):
    identify.enroll("Mark", _vec(1))
    bad = np.full(256, np.nan)
    res = identify.name_speakers({"S0": bad}, threshold=0.0, margin=0.0)
    assert res["S0"]["name"] is None  # NaN must not clear any threshold


def test_rename_and_merge_and_remove(sandbox):
    identify.enroll("Marc", _vec(1))
    assert identify.rename_person("Marc", "Mark")
    assert "Mark" in identify.load_registry()
    identify.enroll("Mark P", _vec(1) + 0.05 * _vec(2))
    assert identify.merge_people("Mark P", "Mark")
    assert "Mark P" not in identify.load_registry()
    assert identify.load_registry()["Mark"]["n_samples"] == 2
    assert identify.remove_person("Mark")
    assert identify.load_registry() == {}


def test_rename_to_existing_becomes_merge(sandbox):
    identify.enroll("A", _vec(1))
    identify.enroll("B", _vec(2))
    assert identify.rename_person("A", "B")
    reg = identify.load_registry()
    assert "A" not in reg and reg["B"]["n_samples"] == 2


def test_enroll_records_source_provenance(sandbox):
    identify.enroll("Mark", _vec(1), source="LT Meeting A")
    identify.enroll("Mark", _vec(2), source="LT Meeting B")
    reg = identify.load_registry()
    assert reg["Mark"]["sources"] == ["LT Meeting A", "LT Meeting B"]
    # rolling window keeps sources aligned with samples
    for i in range(identify.MAX_SAMPLES):
        identify.enroll("Mark", _vec(10 + i), source=f"M{i}")
    reg = identify.load_registry()
    assert len(reg["Mark"]["sources"]) == identify.MAX_SAMPLES
    assert reg["Mark"]["sources"][-1] == f"M{identify.MAX_SAMPLES - 1}"


def test_merge_combines_sources(sandbox):
    identify.enroll("A", _vec(1), source="M1")
    identify.enroll("B", _vec(2), source="M2")
    identify.merge_people("A", "B")
    assert identify.load_registry()["B"]["sources"] == ["M2", "M1"]


def test_remove_sample_states(sandbox):
    identify.enroll("Mark", _vec(1), source="M1")
    identify.enroll("Mark", _vec(2), source="M2")
    identify.enroll("Mark", _vec(3), source="M3")
    assert identify.remove_sample("Mark", 1)  # drop the M2 sample
    reg = identify.load_registry()
    assert reg["Mark"]["n_samples"] == 2
    assert reg["Mark"]["sources"] == ["M1", "M3"]
    assert not identify.remove_sample("Mark", 9)      # out of range
    assert identify.remove_sample("Mark", 0)
    assert not identify.remove_sample("Mark", 0)      # last sample protected
    assert not identify.remove_sample("Nobody", 0)    # unknown person

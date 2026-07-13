"""refine_turns: flag hygiene — the two-pass smoothing loop must not double-count
a flag, the mid-band id_mismatch flag needs a comparative signal (a low own-score
alone is a duration artifact, not evidence), and the unconditional short-turn
catch-all is strict-mode-only."""
import numpy as np

from stt import refine


def _vp(c):
    """A one-sample voiceprint whose cosine against the unit turn embedding
    [1, 0] is exactly c (unit vector at that angle)."""
    return np.array([[c, np.sqrt(1.0 - c * c)]])


TURN_EMB = np.array([1.0, 0.0])


def _mid_band_case(voiceprints, cluster_names=None, strict=False):
    """One 1.0s turn (mid-band: short_dur 0.3 <= 1.0 < min_reliable 1.5) whose
    cluster is named Alice; refine_turns scores TURN_EMB against each voiceprint."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C0"}]
    return refine.refine_turns(turns, [TURN_EMB],
                               cluster_names or {"C0": "Alice"},
                               voiceprints=voiceprints, strict=strict)


def test_short_low_confidence_flag_not_doubled_across_two_passes(sandbox):
    """A short turn sandwiched by one unchanged speaker, with no voice evidence
    favoring the neighbour, hits the else branch on BOTH passes; the flag and
    stats['flagged'] must each count it once."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "SPEAKER_00"},
             {"start": 5.0, "end": 5.25, "cluster": "SPEAKER_01"},  # < 0.3s short
             {"start": 5.25, "end": 10.0, "cluster": "SPEAKER_00"}]
    # middle turn's embedding is usable but there are no voiceprints/centroids,
    # so neither own nor target reference exists -> else branch (flag it, keep)
    tembs = [None, np.ones(4, dtype=float), None]
    cluster_names = {"SPEAKER_00": None, "SPEAKER_01": None}

    merged, stats = refine.refine_turns(
        turns, tembs, cluster_names, voiceprints={}, strict=False)

    assert stats["flagged"] == 1
    flags = [s["flag"] for s in stats["spans"]]
    assert flags.count("short_low_confidence") == 1


# --- id_mismatch: comparative gate (retuned 07/2026 on the real library) ------
# The median CORRECTLY-attributed mid-band turn scores 0.32 — under the old flat
# 0.40 gate — so "own score is low" must not flag on its own in normal mode.

def test_low_own_score_alone_does_not_flag_mismatch(sandbox):
    """Own 0.32 (the library's median correct mid-band score), nobody else
    close (Bob 0.10): a duration artifact, not a mismatch -> NO flag."""
    _, stats = _mid_band_case({"Alice": _vp(0.32), "Bob": _vp(0.10)})
    assert stats["flagged"] == 0
    assert not any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_low_own_score_flags_mismatch_in_strict(sandbox):
    """Strict keeps the old unconditional gate: flag, don't guess."""
    _, stats = _mid_band_case({"Alice": _vp(0.32), "Bob": _vp(0.10)}, strict=True)
    assert stats["flagged"] == 1
    assert any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_low_own_score_flags_mismatch_with_unnamed_cluster(sandbox):
    """An UNNAMED cluster in the meeting = open roster: the low score may be
    the un-enrolled person, whom no voiceprint can out-score -> old gate holds."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C0"},
             {"start": 2.0, "end": 4.0, "cluster": "C1"}]  # the anonymous voice
    _, stats = refine.refine_turns(
        turns, [TURN_EMB, None], {"C0": "Alice", "C1": None},
        voiceprints={"Alice": _vp(0.32), "Bob": _vp(0.10)}, strict=False)
    assert any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_other_voice_clearly_ahead_flags_mismatch(sandbox):
    """Own low (0.32) AND another enrolled voice genuinely fits (0.55 >= 0.50,
    margin 0.23 >= 0.15): the comparative signal -> flag."""
    _, stats = _mid_band_case({"Alice": _vp(0.32), "Bob": _vp(0.55)})
    assert stats["flagged"] == 1
    assert any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_other_voice_without_margin_does_not_flag(sandbox):
    """Bob clears the 0.50 absolute bar but only beats Alice by 0.13 (< 0.15):
    both halves of the comparative gate are required."""
    _, stats = _mid_band_case({"Alice": _vp(0.38), "Bob": _vp(0.51)})
    assert stats["flagged"] == 0
    assert not any(s["flag"] == "id_mismatch" for s in stats["spans"])


# --- short-turn catch-all: strict-mode-only -----------------------------------

def test_unsandwiched_short_turn_not_flagged_in_normal_mode(sandbox):
    """A short turn with DIFFERENT neighbors (no smoothing target, no contrary
    voice evidence) used to be flagged wholesale; normal mode now trusts the
    diarizer unless the evidence branch says otherwise."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "C0"},
             {"start": 5.0, "end": 5.2, "cluster": "C1"},  # < 0.3s, C0 != C2 around it
             {"start": 5.2, "end": 10.0, "cluster": "C2"}]
    _, stats = refine.refine_turns(turns, [None, None, None],
                                   {"C0": None, "C1": None, "C2": None},
                                   voiceprints={}, strict=False)
    assert stats["flagged"] == 0
    assert stats["spans"] == []


def test_unsandwiched_short_turn_still_flagged_in_strict(sandbox):
    """Strict semantics unchanged: every unvouched short turn is a human's call."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "C0"},
             {"start": 5.0, "end": 5.2, "cluster": "C1"},
             {"start": 5.2, "end": 10.0, "cluster": "C2"}]
    _, stats = refine.refine_turns(turns, [None, None, None],
                                   {"C0": None, "C1": None, "C2": None},
                                   voiceprints={}, strict=True)
    assert stats["flagged"] == 1
    assert [s["flag"] for s in stats["spans"]] == ["short_low_confidence"]

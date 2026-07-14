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
    """An UNNAMED cluster with substantial talk (>= 30s) = open roster: the low
    score may be the un-enrolled person, whom no voiceprint can out-score ->
    old gate holds."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C0"},
             {"start": 2.0, "end": 40.0, "cluster": "C1"}]  # the anonymous voice
    _, stats = refine.refine_turns(
        turns, [TURN_EMB, None], {"C0": "Alice", "C1": None},
        voiceprints={"Alice": _vp(0.32), "Bob": _vp(0.10)}, strict=False)
    assert any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_junk_unnamed_cluster_does_not_open_the_roster(sandbox):
    """A few seconds of unnamed noise-floor 'cluster' is NOT an un-enrolled
    attendee: it must not flip the meeting to open-roster semantics and start
    flagging every duration-artifact low score as an id_mismatch."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C0"},
             {"start": 2.0, "end": 4.5, "cluster": "C9"},   # 2.5s of junk
             {"start": 5.0, "end": 7.5, "cluster": "C9"}]   # 5s total < 30s
    _, stats = refine.refine_turns(
        turns, [TURN_EMB, None, None], {"C0": "Alice", "C9": None},
        voiceprints={"Alice": _vp(0.32), "Bob": _vp(0.10)}, strict=False)
    assert not any(s["flag"] == "id_mismatch" for s in stats["spans"])


def test_junk_cluster_still_opens_roster_in_strict(sandbox):
    """Strict keeps the unconditional semantics: even with only junk unnamed
    clusters around, a low own-score mid-band turn is flagged."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C0"},
             {"start": 2.0, "end": 4.5, "cluster": "C9"}]
    _, stats = refine.refine_turns(
        turns, [TURN_EMB, None], {"C0": "Alice", "C9": None},
        voiceprints={"Alice": _vp(0.32), "Bob": _vp(0.10)}, strict=True)
    assert any(s["flag"] == "id_mismatch" for s in stats["spans"])


# --- protected one-word answers: evidence-gated override (retuned 07/2026) ----
# The sandwich smoothing band widened to 0.6s, which pushes it into the
# protected-answer territory; flips stay EVIDENCE-GATED (the audit measured 637
# evidence-backed label changes and rejected unconditional inheritance), and a
# protected answer needs a clear >0.15 margin on its own embedding to move.

def _protected_sandwich(voiceprints, temb=TURN_EMB, word="yeah", strict=False):
    """Alice's context surrounds Bob's 0.4s protected one-word answer."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "C0"},
             {"start": 5.0, "end": 5.4, "cluster": "C1"},
             {"start": 5.4, "end": 10.0, "cluster": "C0"}]
    words = [{"start": 5.1, "end": 5.3, "word": word}]
    return refine.refine_turns(
        turns, [None, temb, None], {"C0": "Alice", "C1": "Bob"},
        voiceprints=voiceprints, words=words, strict=strict)


def test_protected_answer_released_when_context_speaker_clearly_wins(sandbox):
    """Filler case 1: the context speaker beats the assigned one by > 0.15 on
    the turn's OWN embedding — the one-word answer re-attributes."""
    merged, stats = _protected_sandwich({"Alice": _vp(0.60), "Bob": _vp(0.40)})
    assert len(merged) == 1 and merged[0]["speaker"] == "Alice"
    assert stats["protected_overridden"] == 1
    assert not any(s["flag"] == "protected_answer" for s in stats["spans"])
    assert any(s["flag"] == "smoothed" for s in stats["spans"])  # auditable


def test_protected_answer_kept_without_a_clear_margin(sandbox):
    """Filler case 2: the context speaker ahead but not by MORE than 0.15
    (0.10 and 0.14 — the exact boundary is float-fuzzy by construction, so
    the guard is tested just under it) — a meaningful 'yeah' keeps its
    diarized owner, flagged."""
    for alice in (0.50, 0.54):  # margin 0.10; margin 0.14 < 0.15
        merged, stats = _protected_sandwich({"Alice": _vp(alice), "Bob": _vp(0.40)})
        speakers = [m["speaker"] for m in merged]
        assert "Bob" in speakers, f"alice={alice}"
        assert stats["protected_overridden"] == 0
        assert any(s["flag"] == "protected_answer" for s in stats["spans"])


def test_protected_answer_never_inherited_without_voice_evidence(sandbox):
    """Filler case 3: an unusable embedding is NOT evidence — the audit
    rejected unconditional inheritance, so the answer stays put, flagged."""
    merged, stats = _protected_sandwich({"Alice": _vp(0.60), "Bob": _vp(0.40)},
                                        temb=None)
    assert "Bob" in [m["speaker"] for m in merged]
    assert stats["protected_overridden"] == 0
    assert any(s["flag"] == "protected_answer" for s in stats["spans"])


def test_non_protected_fragment_keeps_the_plain_smoothing_rules(sandbox):
    """Filler case 4: a non-protected fragment ('so') is governed by the
    ordinary evidence gate — with no usable embedding the timing sandwich
    smooths it, exactly as before."""
    merged, stats = _protected_sandwich({"Alice": _vp(0.60), "Bob": _vp(0.40)},
                                        temb=None, word="so")
    assert len(merged) == 1 and merged[0]["speaker"] == "Alice"
    assert stats["smoothed"] == 1
    assert stats["protected_overridden"] == 0


def test_protected_override_never_runs_in_strict(sandbox):
    """Strict mode does no smoothing at all: even a clear-margin protected
    answer stays with the diarizer's speaker."""
    merged, stats = _protected_sandwich({"Alice": _vp(0.60), "Bob": _vp(0.40)},
                                        strict=True)
    assert "Bob" in [m["speaker"] for m in merged]
    assert stats["protected_overridden"] == 0


def test_short_dur_widened_smooths_a_half_second_fragment(sandbox):
    """REFINE_SHORT_DUR 0.3 -> 0.6: a 0.4s non-protected fragment whose voice
    favors the surrounding speaker now smooths (it sat in the anti-signal band
    the old bound excluded)."""
    merged, stats = _protected_sandwich({"Alice": _vp(0.60), "Bob": _vp(0.40)},
                                        word="but")
    assert len(merged) == 1 and merged[0]["speaker"] == "Alice"
    assert stats["smoothed"] == 1


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


# --- mid-band open-set rescue (exp1-band-gap, 07/2026) ------------------------
# A 0.6-1.5s turn stranded in an UNNAMED cluster may move to the enrolled
# speaker its own embedding ranks first with a >= 0.10 margin: floor 0.70
# anywhere, floor 0.35 when that speaker owns an adjacent turn. Named-cluster
# turns are never moved (the swept named->named variants regressed real human
# corrections), and strict mode never runs the gate.

def _stranded_case(alice, bob, strict=False, named_owner=None):
    """One 1.0s mid-band turn; its cluster is unnamed unless named_owner is
    given. Alice/Bob voiceprints score alice/bob against the turn's embedding."""
    turns = [{"start": 0.0, "end": 1.0, "cluster": "C1"}]
    return refine.refine_turns(
        turns, [TURN_EMB], {"C1": named_owner},
        voiceprints={"Alice": _vp(alice), "Bob": _vp(bob)}, strict=strict)


def test_midband_stranded_turn_rescued_on_strong_evidence(sandbox):
    """No neighbour anchor: rank-1 at 0.72 with margin 0.32 clears the
    open-set floor — the turn moves, with auditable provenance (this is the
    synth trail_trip/trivia_night blob rescue, bs 0.727/0.709)."""
    merged, stats = _stranded_case(0.72, 0.40)
    assert merged[0]["speaker"] == "Alice"
    assert stats["reassigned"] == 1
    assert any(s["flag"] == "reassigned" for s in stats["spans"])


def test_midband_rescue_blocked_below_openset_floor_without_neighbour(sandbox):
    """0.60 is a solid score — but an open-set claim about an unnamed cluster
    with no corroborating neighbour keeps the reliable-band bar (0.70). The
    swept roster-wide low floors (0.35-0.45) admitted flips to people with no
    cluster in the meeting and were rejected."""
    merged, stats = _stranded_case(0.60, 0.20)
    assert merged[0]["speaker"] == "C1"
    assert stats["reassigned"] == 0
    assert not any(s["flag"] == "reassigned" for s in stats["spans"])


def test_midband_rescue_needs_margin_not_just_score(sandbox):
    """Rank-1 at 0.72 but the runner-up at 0.65: a 0.07 margin is a coin flip
    between enrolled voices (the two-Marks junk turns), not identity."""
    merged, stats = _stranded_case(0.72, 0.65)
    assert merged[0]["speaker"] == "C1"
    assert stats["reassigned"] == 0


def test_midband_neighbour_anchor_lowers_the_floor(sandbox):
    """The rank-1 speaker also owns the adjacent turn: adjacency + rank-1 +
    margin agree, so 0.40 suffices (the human-verified rescues on the real
    library scored 0.511 and 0.375)."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "C0"},   # Alice, adjacent
             {"start": 5.0, "end": 6.0, "cluster": "C1"}]   # stranded mid-band
    merged, stats = refine.refine_turns(
        turns, [None, TURN_EMB], {"C0": "Alice", "C1": None},
        voiceprints={"Alice": _vp(0.40), "Bob": _vp(0.20)}, strict=False)
    assert len(merged) == 1 and merged[0]["speaker"] == "Alice"
    assert stats["reassigned"] == 1


def test_midband_low_score_not_rescued_without_neighbour_anchor(sandbox):
    """Same 0.40 evidence with the anchor absent (neighbour is someone else):
    below the open-set floor, the turn stays the diarizer's."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "C2"},   # Bob, not rank-1
             {"start": 5.0, "end": 6.0, "cluster": "C1"}]
    merged, stats = refine.refine_turns(
        turns, [None, TURN_EMB], {"C2": "Bob", "C1": None},
        voiceprints={"Alice": _vp(0.40), "Bob": _vp(0.20)}, strict=False)
    assert merged[-1]["speaker"] == "C1"
    assert stats["reassigned"] == 0


def test_midband_named_cluster_never_rescued(sandbox):
    """A NAMED mid-band turn keeps its owner even when a rival is ahead on
    the turn's own embedding — 0.437 vs 0.329 is the real-library case
    (Brenda vs Briana) where flipping contradicted the human correction.
    Verification (id_mismatch flagging) governs named turns, not rescue."""
    merged, stats = _stranded_case(0.437, 0.329, named_owner="Bob")
    assert merged[0]["speaker"] == "Bob"
    assert stats["reassigned"] == 0


def test_midband_rescue_never_in_strict(sandbox):
    """Strict output is byte-identical: the would-fire strong-evidence case
    neither moves nor gains flags."""
    merged, stats = _stranded_case(0.72, 0.40, strict=True)
    assert merged[0]["speaker"] == "C1"
    assert stats["reassigned"] == 0 and stats["spans"] == []


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

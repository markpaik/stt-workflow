"""merge: word->speaker assignment under clock skew, ties, gaps, and flags."""
from stt import merge

TURNS = [
    {"start": 0.0, "end": 5.0, "speaker": "A"},
    {"start": 5.0, "end": 10.0, "speaker": "B"},
]
NAMES = {"A": {"name": "Alice", "score": 0.9}, "B": {"name": None, "score": 0.0}}


def _w(s, e, w="hi"):
    return {"start": s, "end": e, "word": w}


def test_max_overlap_assignment():
    segs, words = merge.assign_and_group([_w(1, 2), _w(6, 7)], TURNS, NAMES)
    assert words[0]["speaker"] == "A" and words[1]["speaker"] == "B"
    assert len(segs) == 2
    assert segs[0]["name"] == "Alice" and segs[1]["name"] is None


def test_boundary_word_goes_to_larger_overlap():
    # word 4.2-5.5: 0.8s in A, 0.5s in B -> A (midpoint would also say A, but
    # 4.9-5.6 has midpoint in B while overlap favors... construct skew case)
    _, words = merge.assign_and_group([_w(4.4, 5.2)], TURNS, NAMES)
    assert words[0]["speaker"] == "A"  # 0.6s in A vs 0.2s in B


def test_tie_breaks_to_previous_speaker():
    _, words = merge.assign_and_group([_w(4.0, 4.5), _w(4.75, 5.25)], TURNS, NAMES)
    # second word overlaps A and B 0.25s each -> stays with previous (A)
    assert words[1]["speaker"] == "A"


def test_word_in_gap_uses_nearest_turn():
    turns = [{"start": 0, "end": 2, "speaker": "A"}, {"start": 8, "end": 10, "speaker": "B"}]
    _, words = merge.assign_and_group([_w(2.5, 3.0)], turns, NAMES)
    assert words[0]["speaker"] == "A"


def test_no_turns_yields_unlabeled_single_segment():
    segs, words = merge.assign_and_group([_w(0, 1, "a"), _w(1, 2, "b")], [], {})
    assert words[0]["speaker"] is None
    assert len(segs) == 1 and segs[0]["text"] == "a b"


def test_consecutive_same_speaker_grouped():
    segs, _ = merge.assign_and_group([_w(0, 1, "one"), _w(1, 2, "two"), _w(6, 7, "three")],
                                     TURNS, NAMES)
    assert [s["text"] for s in segs] == ["one two", "three"]


def test_overlap_flags_word_and_majority_marks_segment():
    words = [_w(0, 1, "clean"), _w(1, 2, "clean2"), _w(6, 7, "talked-over")]
    segs, labeled = merge.assign_and_group(words, TURNS, NAMES, overlaps=[(5.5, 8.0)])
    assert "overlap" not in labeled[0].get("flags", [])
    assert "overlap" in labeled[2]["flags"]
    a_seg, b_seg = segs
    assert not a_seg["overlap"] and b_seg["overlap"]  # only fully-flagged segment marked


def test_minority_flag_does_not_mark_long_segment():
    words = [_w(i, i + 1, f"w{i}") for i in range(5)]  # one segment, speaker A
    segs, labeled = merge.assign_and_group(words, TURNS, NAMES, overlaps=[(0.0, 0.9)])
    assert len(segs) == 1
    assert "overlap" in labeled[0]["flags"]  # the word itself IS flagged...
    assert not segs[0]["overlap"]  # ...but 1 of 5 words < half -> segment unmarked


def test_punctuation_spacing_cleaned():
    words = [_w(0, 1, "Hello"), _w(1, 2, ","), _w(2, 3, "world")]
    segs, _ = merge.assign_and_group(words, TURNS, NAMES)
    assert segs[0]["text"] == "Hello, world"


def test_zero_duration_word_at_boundary_respects_previous_speaker():
    """A zero-duration word (start==end, from 3-decimal rounding) sitting
    exactly at a turn boundary can't produce a positive overlap either way —
    it must still honor the previous-speaker tie-break like a normal
    boundary word does, not silently default to whichever turn comes first
    in the list regardless of context (non-deterministic misattribution)."""
    # first word solidly in B -> prev_speaker becomes B; second word is a
    # zero-duration point exactly at the A(0-5)/B(5-10) boundary
    _, words = merge.assign_and_group([_w(6.0, 7.0), _w(5.0, 5.0)], TURNS, NAMES)
    assert words[0]["speaker"] == "B"
    assert words[1]["speaker"] == "B"  # ties to previous speaker, not turn A


def test_zero_duration_word_inside_a_turn_is_assigned_to_it():
    _, words = merge.assign_and_group([_w(2.0, 2.0)], TURNS, NAMES)
    assert words[0]["speaker"] == "A"

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

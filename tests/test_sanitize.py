"""sanitize: ASR hallucination-loop collapse (the 'now now now…' failure)."""
from stt import sanitize


def _w(i, word):
    return {"start": float(i), "end": i + 0.9, "word": word}


def test_normal_speech_untouched():
    words = [_w(i, w) for i, w in enumerate("the plan looks good to me".split())]
    out, spans = sanitize.collapse_repeats(words)
    assert out == words and spans == []


def test_natural_repetition_kept():
    words = [_w(i, w) for i, w in enumerate(["no", "no", "no", "that's", "fine"])]
    out, spans = sanitize.collapse_repeats(words)
    assert [w["word"] for w in out] == ["no", "no", "no", "that's", "fine"]
    assert spans == []


def test_unigram_loop_collapsed_and_flagged():
    words = ([_w(0, "I'm")] + [_w(i + 1, "now") for i in range(120)]
             + [_w(200, "things"), _w(201, "that")])
    out, spans = sanitize.collapse_repeats(words)
    texts = [w["word"] for w in out]
    assert texts.count("now") == sanitize.MAX_NATURAL_REPEATS
    assert texts[-2:] == ["things", "that"]
    assert len(spans) == 1 and spans[0]["flag"] == "possible:asr_loop"
    assert spans[0]["start"] == 1.0  # covers the loop for review


def test_phrase_loop_collapsed():
    pair = ["you", "know"]
    words = [_w(i, pair[i % 2]) for i in range(24)] + [_w(99, "anyway")]
    out, spans = sanitize.collapse_repeats(words)
    texts = [w["word"] for w in out]
    assert texts.count("you") == sanitize.MAX_NATURAL_REPEATS
    assert texts[-1] == "anyway"
    assert len(spans) == 1


def test_case_and_punct_insensitive():
    words = [_w(i, w) for i, w in
             enumerate(["Now,", "now", "NOW", "now.", "now", "now", "now", "ok"])]
    out, spans = sanitize.collapse_repeats(words)
    assert len(spans) == 1
    assert [w["word"] for w in out][-1] == "ok"


def test_two_separate_loops_both_flagged():
    words = ([_w(i, "yes") for i in range(8)] + [_w(20, "middle")]
             + [_w(30 + i, "right") for i in range(8)])
    out, spans = sanitize.collapse_repeats(words)
    assert len(spans) == 2
    texts = [w["word"] for w in out]
    assert texts.count("yes") == 3 and texts.count("right") == 3 and "middle" in texts


def test_words_pass_through_letter_for_letter():
    """The "<unk>" repair went with Parakeet, the one engine that emitted
    it: a word is never rewritten, only a loop is cut."""
    assert not hasattr(sanitize, "strip_unk")
    words = [_w(0, "two-year"), _w(1, "self-management"), _w(2, "people.")]
    out, spans = sanitize.collapse_repeats(words)
    assert out == words and spans == []

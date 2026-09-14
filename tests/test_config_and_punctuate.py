"""config env parsing + punctuate's word-preserving safety guarantees."""
from stt import config, punctuate


def test_env_file_parsing(sandbox):
    (sandbox / "stt.env").write_text(
        "# comment\n"
        "HF_TOKEN=abc\n"
        "  STT_ASR_BACKEND = parakeet \n"
        "BROKEN LINE\n"
        "EMPTY=\n")
    kv = config._env_file()
    assert kv["HF_TOKEN"] == "abc"
    assert kv["STT_ASR_BACKEND"] == "parakeet"
    assert kv["EMPTY"] == ""
    assert "BROKEN LINE" not in kv


def test_dir_overrides_read_fresh(sandbox):
    (sandbox / "stt.env").write_text(f"STT_MEETINGS_DIR={sandbox}/elsewhere\n")
    assert str(config.meetings_dir()).endswith("/elsewhere")
    (sandbox / "stt.env").write_text("")  # cleared -> falls back to default
    assert config.meetings_dir() == config.MEETINGS_DIR


def test_punctuate_fails_open_on_model_error(sandbox, monkeypatch):
    class Boom:
        def infer(self, xs):
            raise RuntimeError("model exploded")
    monkeypatch.setattr(punctuate, "_model", Boom())
    assert punctuate.restore("hello world how are you") == "hello world how are you"


def test_punctuate_rejects_word_count_change(sandbox, monkeypatch):
    class Rewriter:
        def infer(self, xs):
            return [["Hello world, and MORE words injected."]]
    monkeypatch.setattr(punctuate, "_model", Rewriter())
    # model tried to add words -> output rejected, original text kept
    assert punctuate.restore("hello world") == "hello world"


def test_punctuate_accepts_word_preserving_output(sandbox, monkeypatch):
    class Good:
        def infer(self, xs):
            return [["Hello world.", "How are you?"]]
    monkeypatch.setattr(punctuate, "_model", Good())
    out = punctuate.restore("hello world how are you")
    assert out == "Hello world. How are you?"


def test_punctuate_rejects_same_count_word_substitution(sandbox, monkeypatch):
    """The hard requirement is WORD identity, not just word count — a
    same-count substitution (a real risk under distribution shift) must be
    rejected exactly like an added/removed word, not shipped silently into a
    transcript of a possibly sensitive conversation."""
    class Substituter:
        def infer(self, xs):
            # same number of tokens as "yes we agreed to the settlement",
            # but "settlement" silently became "disagreement"
            return [["Yes, we agreed to the disagreement."]]
    monkeypatch.setattr(punctuate, "_model", Substituter())
    assert punctuate.restore("yes we agreed to the settlement") == "yes we agreed to the settlement"


def test_punctuate_empty_passthrough(sandbox):
    assert punctuate.restore("") == ""
    assert punctuate.restore("   ") == "   "


def test_restore_segments_counts_changes(sandbox, monkeypatch):
    class Good:
        def infer(self, xs):
            return [["Fixed."]]
    monkeypatch.setattr(punctuate, "_model", Good())
    segs = [{"text": "fixed"}, {"text": ""}]
    n = punctuate.restore_segments(segs)
    assert n == 1 and segs[0]["text"] == "Fixed."


def test_punctuator_unk_repaired_from_original():
    """The punctuator's vocab lacks hyphens: 'self-management' round-trips as
    '<unk>management'. The original word must be restored, punctuation kept."""
    from stt import punctuate
    norm = "we did self-management and a two-year plan"
    out = "We did <unk>management. And a <unk>year plan."
    fixed = punctuate._repair_unk(out, norm)
    assert fixed == "We did self-management. And a two-year plan."
    # untouched when counts mismatch (safety check rejects later anyway)
    assert punctuate._repair_unk("<unk> b c", "one two") == "<unk> b c"
    assert punctuate._repair_unk("clean text", norm) == "clean text"


# ---------------------------------------------------------------------------
# Batched restoration. Measured 2026-09-13: 4.0s of a 7.1s relabel was the
# punctuator reloading its SentencePiece tokenizer once per infer() call, 273
# times. One call over the whole list takes 0.73s. Batching may change how the
# model is CALLED and nothing about what is allowed through, so these compare
# the batched result against the per-segment result it replaces.
# ---------------------------------------------------------------------------
class _Fake:
    """Truecases the first word and ends with a period, INDEPENDENTLY per
    input — so a batch of five behaves exactly like five calls of one, and any
    difference the test finds is the batching, not the fake."""

    def __init__(self, swap=None, fail_batches=False):
        self.swap = swap or {}
        self.fail_batches = fail_batches
        self.calls = []

    def infer(self, xs):
        self.calls.append(list(xs))
        if self.fail_batches and len(xs) > 1:
            raise RuntimeError("batched inference exploded")
        out = []
        for x in xs:
            toks = [self.swap.get(t, t) for t in x.split()]
            out.append([" ".join([toks[0].capitalize()] + toks[1:]) + "."])
        return out


_FIVE = ["hello world how are you",
         "the board meets on tuesday",
         "we reviewed the attendance data",
         "",
         "that is all for today"]


def test_batched_punctuation_equals_per_segment_output(sandbox, monkeypatch):
    monkeypatch.setattr(punctuate, "_model", _Fake())
    one_by_one = [punctuate.restore(t) for t in _FIVE]

    fake = _Fake()
    monkeypatch.setattr(punctuate, "_model", fake)
    segs = [{"text": t} for t in _FIVE]
    changed = punctuate.restore_segments(segs)

    assert [s["text"] for s in segs] == one_by_one
    assert changed == 4, "the empty segment is not a change"
    assert len(fake.calls) == 1, \
        "the whole meeting must go through the model in ONE call"
    assert fake.calls[0] == [punctuate._normalize_for_model(t)
                             for t in _FIVE if t.strip()]


def test_batched_word_change_only_costs_its_own_segment(sandbox, monkeypatch):
    """The word-identity check is PER SEGMENT. A batch where the model
    substituted a word in one line must keep that line's original text and
    still punctuate its neighbours — refusing the whole batch would silently
    leave a meeting unpunctuated, and accepting it would ship an altered word
    into a possibly sensitive transcript."""
    fake = _Fake(swap={"settlement": "disagreement"})
    monkeypatch.setattr(punctuate, "_model", fake)
    segs = [{"text": "hello world"},
            {"text": "yes we agreed to the settlement"},
            {"text": "how are you"}]
    changed = punctuate.restore_segments(segs)

    assert segs[1]["text"] == "yes we agreed to the settlement", \
        "a substituted word must be refused, exactly as in the single path"
    assert segs[0]["text"] == "Hello world."
    assert segs[2]["text"] == "How are you."
    assert changed == 2


def test_batched_failure_falls_back_to_the_per_segment_loop(sandbox, monkeypatch):
    """Fails open, then fails SLOW: a batched call that raises must not leave
    every segment unpunctuated when the one-at-a-time path still works."""
    fake = _Fake(fail_batches=True)
    monkeypatch.setattr(punctuate, "_model", fake)
    segs = [{"text": "hello world"}, {"text": "how are you"}]
    changed = punctuate.restore_segments(segs)

    assert changed == 2
    assert [s["text"] for s in segs] == ["Hello world.", "How are you."]
    assert len(fake.calls) == 3, "one batched attempt, then one call per segment"


def test_batched_result_of_the_wrong_length_is_refused(sandbox, monkeypatch):
    """Zipping a short result list onto the segments would land one line's text
    on another speaker's line. That is worse than no punctuation."""
    class Short:
        def infer(self, xs):
            return [["Hello world."]]
    monkeypatch.setattr(punctuate, "_model", Short())
    segs = [{"text": "hello world"}, {"text": "how are you"}]
    punctuate.restore_segments(segs)
    # the fallback loop re-ran each segment ALONE, where a 1-long result is the
    # right shape: segment 1 punctuates, segment 2's word-identity check refuses
    # the first segment's text and keeps its own. Neither line got the other's.
    assert [s["text"] for s in segs] == ["Hello world.", "how are you"]


def test_offline_gate_needs_both_cached_files(sandbox, monkeypatch):
    """HF_HUB_OFFLINE is set only when the model is provably on disk: setting
    it blind would turn a first run into a permanent failure."""
    root = sandbox / "hfcache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    assert punctuate._model_files_cached() is False

    snap = (root / ("models--" + punctuate._HF_REPO.replace("/", "--"))
            / "snapshots" / "abc123")
    snap.mkdir(parents=True)
    (snap / punctuate._SPE_FILENAME).write_bytes(b"x")
    assert punctuate._model_files_cached() is False, \
        "one of the two files is not enough"
    (snap / punctuate._ONNX_FILENAME).write_bytes(b"x")
    assert punctuate._model_files_cached() is True

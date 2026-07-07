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

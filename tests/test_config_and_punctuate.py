"""config env parsing + punctuate's word-preserving safety guarantees."""
import os

import pytest

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



class _SplitsLikeTheModel:
    """Answers the way the real model did on 2026-11-10: it punctuates the
    pieces of a split token ('3 5') as separate words, and may put a comma or
    a full stop between them."""

    def __init__(self, answers):
        self.answers = answers

    def infer(self, xs):
        return [[self.answers[x]] for x in xs]


def test_punctuate_keeps_punctuation_inside_a_token(sandbox, monkeypatch):
    """'3.5' reaches the model as '3 5'. The identity check ignores
    punctuation, so '3 5' passed it and shipped: a rate, a time and a sum of
    money were rewritten in the transcript. The token must come back whole."""
    monkeypatch.setattr(punctuate, "_model", _SplitsLikeTheModel({
        "the rate is 3 5 percent": "The rate is 3 5 percent.",
        "meet at 9 30 in room 4 2": "Meet at 9, 30 in Room 4. 2.",
        "it costs $1 200 for the self-management course":
            "It costs $1 200 for the <unk>management course.",
        "v1 2 shipped and jo doe@example org knows":
            "V1 2 shipped, and jo doe@example. Org knows.",
        "we met at 9 a m": "We met at 9 a m",
    }))
    segs = [{"text": t} for t in [
        "The rate is 3.5 percent.",
        "Meet at 9:30 in room 4.2",
        "It costs $1,200 for the self-management course",
        "v1.2 shipped and jo.doe@example.org knows",
        "we met at 9 a.m.",
    ]]
    punctuate.restore_segments(segs)
    assert [s["text"] for s in segs] == [
        "The rate is 3.5 percent.",
        "Meet at 9:30 in Room 4.2.",
        "It costs $1,200 for the self-management course.",
        # the model's casing on the first piece only, its comma after it
        "V1.2 shipped, and jo.doe@example.org knows.",
        # the model put nothing after 'a m', so the original's '.' stays
        "We met at 9 a.m.",
    ]
    assert punctuate.restore("The rate is 3.5 percent.") == \
        "The rate is 3.5 percent."


def test_the_last_period_of_an_abbreviation_stays_before_the_models_mark():
    """The rebuild put the model's mark after an inner-punctuation token
    in place of the original's: 'e.g.' with the model's ',' came back as
    'e.g,'. An abbreviation's last period is part of the word. A number's
    is not: '3.5.' still takes the model's comma."""
    r = punctuate._restore_inner_punct
    assert r("The U s. team e. g, Ana agreed.", "the U.S. team, e.g. Ana, agreed") == \
        "The U.S. team e.g., Ana agreed."
    assert r("In the U s.", "in the U.S.") == "In the U.S."
    assert r("Met at 9 a m. then left", "met at 9 a.m. then left") == \
        "Met at 9 a.m. then left"
    assert r("See Ph d, and more", "see Ph.D. and more") == "See Ph.D., and more"
    assert r("It was 3 5, then", "it was 3.5. then") == "It was 3.5, then"
    # an original mark after the abbreviation stays when the model puts none
    assert r("Bring snacks e g fruit", "bring snacks, e.g., fruit") == \
        "Bring snacks e.g., fruit"


def test_inner_punct_rebuild_refuses_pieces_that_do_not_line_up():
    """A defensive path: output whose pieces do not match the original's count
    keeps the original text, never a half-rebuilt line."""
    assert punctuate._restore_inner_punct("Three 5 extra.", "3.5") == "3.5"
    assert punctuate._restore_inner_punct("Plain words.", "plain words") == \
        "Plain words."

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


def _hf_snapshot(root, files, rev="abc123"):
    """A huggingface_hub cache laid out the way the hub writes one: refs/main
    names the revision, and the files sit in snapshots/<rev>/."""
    repo = root / ("models--" + punctuate._HF_REPO.replace("/", "--"))
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / "main").write_text(rev)
    snap = repo / "snapshots" / rev
    snap.mkdir(parents=True, exist_ok=True)
    for name in files:
        (snap / name).write_bytes(b"x")
    return snap


def test_local_model_dir_needs_every_cached_file(sandbox, monkeypatch):
    """The model loads from disk only when EVERY file the loader reads is
    cached in one snapshot. Anything less must fall through to the normal
    download, or a first run turns into a permanent failure."""
    root = sandbox / "hfcache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    assert punctuate._cached_model_dir() is None

    snap = _hf_snapshot(root, [punctuate._SPE_FILENAME])
    assert punctuate._cached_model_dir() is None, "one file is not enough"
    _hf_snapshot(root, [punctuate._ONNX_FILENAME])
    assert punctuate._cached_model_dir() is None, \
        "the loader also reads config.yaml"
    _hf_snapshot(root, [punctuate._CONFIG_FILENAME])
    assert punctuate._cached_model_dir() == str(snap)


class _FakeCfg:
    """Mirrors punctuators' PunctCapSegConfigONNX fields, so an unknown
    keyword fails here exactly as it would against the real dataclass."""

    def __init__(self, spe_filename="sp.model", model_filename="model.onnx",
                 config_filename="config.yaml", hf_repo_id=None, directory=None):
        self.spe_filename = spe_filename
        self.model_filename = model_filename
        self.config_filename = config_filename
        self.hf_repo_id = hf_repo_id
        self.directory = directory


def _fake_punctuators(monkeypatch):
    """Stand-in punctuators package: records each config it is built with and
    the environment at that moment (the real loader calls the hub inside
    __init__, which is where HF_HUB_OFFLINE would matter)."""
    import os
    import sys
    import types
    built = []

    class FakeModel:
        def __init__(self, cfg, ort_providers=None):
            built.append({"cfg": cfg, "env": dict(os.environ)})
            if cfg.hf_repo_id is None:
                for name in (cfg.spe_filename, cfg.model_filename,
                             cfg.config_filename):
                    assert os.path.isfile(os.path.join(cfg.directory, name)), name

    mod = types.ModuleType("punctuators.models.punc_cap_seg_model")
    mod.PunctCapSegConfigONNX = _FakeCfg
    mod.PunctCapSegModelONNX = FakeModel
    monkeypatch.setitem(sys.modules, "punctuators", types.ModuleType("punctuators"))
    monkeypatch.setitem(sys.modules, "punctuators.models",
                        types.ModuleType("punctuators.models"))
    monkeypatch.setitem(sys.modules, "punctuators.models.punc_cap_seg_model", mod)
    monkeypatch.setattr(punctuate, "_model", None)
    return built


def test_a_cached_model_loads_by_path_and_leaves_the_environment_alone(
        sandbox, monkeypatch):
    """The panel is a long-lived process. An HF_HUB_OFFLINE set in os.environ
    to skip the hub leaked into every child it spawned, so a batch whose
    diarizer still had to download failed. A cached model now loads from its
    local snapshot directory, and the environment is never written."""
    import os
    root = sandbox / "hfcache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    every = (punctuate._SPE_FILENAME, punctuate._ONNX_FILENAME, "config.yaml")
    snap = _hf_snapshot(root, every)
    built = _fake_punctuators(monkeypatch)
    before = dict(os.environ)

    punctuate._get_model()

    assert dict(os.environ) == before, "the process environment was changed"
    assert "HF_HUB_OFFLINE" not in os.environ
    assert len(built) == 1
    cfg = built[0]["cfg"]
    assert cfg.hf_repo_id is None, "a cached model must not go through the hub"
    assert cfg.directory == str(snap)
    assert (cfg.spe_filename, cfg.model_filename, cfg.config_filename) == every
    assert built[0]["env"] == before


def test_an_uncached_model_downloads_as_normal_and_leaves_the_environment_alone(
        sandbox, monkeypatch):
    import os
    root = sandbox / "hfcache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _hf_snapshot(root, [punctuate._SPE_FILENAME])      # a partial download
    built = _fake_punctuators(monkeypatch)
    before = dict(os.environ)

    punctuate._get_model()

    assert dict(os.environ) == before
    cfg = built[0]["cfg"]
    assert cfg.hf_repo_id == punctuate._HF_REPO and cfg.directory is None
    assert (cfg.spe_filename, cfg.model_filename) == \
        (punctuate._SPE_FILENAME, punctuate._ONNX_FILENAME)


# ---------- a bad number in stt.env warns and keeps the default ----------

def _import_config(tmp_path, **env):
    """Import stt.config in a fresh interpreter with `env` set. Returns
    (returncode, stdout, stderr)."""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("STT_SPEAKRS") and k != "STT_DIARIZER"}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), **env})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c; "
                        "print(c.SPEAKRS_AHC, c.SPEAKRS_TIMEOUT_SEC, c.DIARIZER)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr


@pytest.mark.parametrize("name, raw", [
    ("STT_SPEAKRS_AHC", "abc"), ("STT_SPEAKRS_AHC", "-1"), ("STT_SPEAKRS_AHC", "nan"),
    ("STT_SPEAKRS_AHC", "inf"), ("STT_SPEAKRS_TIMEOUT_SEC", "abc"),
    ("STT_SPEAKRS_TIMEOUT_SEC", "0"), ("STT_SPEAKRS_TIMEOUT_SEC", "-5"),
    ("STT_SPEAKRS_TIMEOUT_SEC", "12.5")])
def test_a_bad_speakrs_number_warns_and_keeps_the_default(tmp_path, name, raw):
    """float('') or int('abc') at import stopped every command that imports
    stt.config. -1 or nan passed, then every file fell back to pyannote, and
    a timeout of 0 timed every helper run out at once."""
    rc, out, err = _import_config(tmp_path, **{name: raw})
    assert rc == 0, err
    assert out == "0.55 3600 speakrs"
    assert name in err and "using" in err


def test_an_empty_or_good_speakrs_number_is_quiet(tmp_path):
    rc, out, err = _import_config(tmp_path, STT_SPEAKRS_AHC="", STT_SPEAKRS_TIMEOUT_SEC="")
    assert (rc, out) == (0, "0.55 3600 speakrs") and "warning" not in err
    rc, out, err = _import_config(tmp_path, STT_SPEAKRS_AHC=" 0.6 ", STT_SPEAKRS_TIMEOUT_SEC="900")
    assert (rc, out) == (0, "0.6 900 speakrs") and "warning" not in err



_TYPO_PRINT = ("from stt import config as c, unknowns as u, housekeeping as h; "
               "print(c.NAMING_THRESHOLD, c.REFINE_ID_MIN, c.RECLUSTER_ITERS, "
               "c.BATTERY_FLOOR, c.CHANNEL_FLOOR_DBFS, u.MATCH_MIN, h.LOG_MAX_BYTES)")
_TYPO_DEFAULTS = "0.6 0.6 2 20 -45.0 0.6 10000000"


@pytest.mark.parametrize("name, raw", [
    ("STT_NAMING_THRESHOLD", "abc"), ("STT_NAMING_THRESHOLD", "nan"),
    ("STT_REFINE_ID_MIN", ""), ("STT_RECLUSTER_ITERS", "2.5"),
    ("STT_BATTERY_FLOOR", "20%"), ("STT_CHANNEL_FLOOR_DBFS", "-45dB"),
    ("STT_UNKNOWN_MATCH_MIN", "O.6"), ("STT_LOG_MAX_BYTES", "10MB")])
def test_a_typo_in_any_number_setting_warns_and_keeps_the_default(tmp_path, name, raw):
    """Every other number setting parsed with a bare float() or int() at
    import. STT_NAMING_THRESHOLD=abc, an empty STT_REFINE_ID_MIN=, or
    STT_BATTERY_FLOOR=20% raised ValueError, and the batch, the panel and
    every tool that imports stt.config stopped."""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), name: raw})
    r = subprocess.run([sys.executable, "-c", _TYPO_PRINT], capture_output=True,
                       text=True, env=e, cwd=str(repo), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == _TYPO_DEFAULTS
    if raw:
        assert name in r.stderr and "using" in r.stderr
    else:
        assert "warning" not in r.stderr


def test_a_good_number_setting_still_wins(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo),
              "STT_NAMING_THRESHOLD": " 0.7 ", "STT_RECLUSTER_ITERS": "4",
              "STT_REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS": "1e9",
              "STT_UNKNOWN_MATCH_MIN": "0.65"})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c, unknowns as u; "
                        "print(c.NAMING_THRESHOLD, c.RECLUSTER_ITERS, "
                        "c.REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS, u.MATCH_MIN)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    assert (r.returncode, r.stdout.strip()) == (0, "0.7 4 1000000000.0 0.65"), r.stderr
    assert "warning" not in r.stderr


def test_the_doctor_names_a_number_typo(sandbox, monkeypatch):
    (sandbox / "stt.env").write_text("STT_BATTERY_FLOOR=20%\n")
    monkeypatch.delenv("STT_BATTERY_FLOOR", raising=False)
    assert [p[0] for p in config.setting_problems()] == ["STT_BATTERY_FLOOR"]

@pytest.mark.parametrize("raw, want, warns", [
    ("speakr", "speakrs", True), ("pyanote", "speakrs", True), ("cuda", "speakrs", True),
    ("pyannote", "pyannote", False), (" PyAnnote ", "pyannote", False),
    ("speakrs", "speakrs", False), ("", "speakrs", False)])
def test_an_unknown_diarizer_warns_and_keeps_the_default(tmp_path, raw, want, warns):
    """diarize() tests only == "speakrs", so STT_DIARIZER=speakr (a typo)
    switched every file to the slow CPU engine with no message."""
    rc, out, err = _import_config(tmp_path, STT_DIARIZER=raw)
    assert rc == 0, err
    assert out.split()[-1] == want
    assert ("STT_DIARIZER" in err and "speakrs" in err and "pyannote" in err) is warns


def _import_asr(tmp_path, raw):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if k != "STT_ASR_BACKEND"}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), "STT_ASR_BACKEND": raw})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c, pipeline as p; "
                        "print(c.ASR_BACKEND, p._load_asr(False).__name__)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr


@pytest.mark.parametrize("raw, want, mod, warns", [
    ("mlx-whisper", "qwen3", "stt.asr_qwen3", True),
    ("whisper", "qwen3", "stt.asr_qwen3", True),
    ("whisperx", "qwen3", "stt.asr_qwen3", True),
    ("cloud:typo", "qwen3", "stt.asr_qwen3", True),
    (" MLXWhisper ", "qwen3", "stt.asr_qwen3", True),   # the removed engine
    ("mlxwhisper", "qwen3", "stt.asr_qwen3", True),
    ("parakeet", "parakeet", "stt.asr_parakeet", False),
    (" Parakeet ", "parakeet", "stt.asr_parakeet", False),
    ("qwen3", "qwen3", "stt.asr_qwen3", False),
    ("qwen", "qwen3", "stt.asr_qwen3", True),
    ("cloud:scribe", "cloud:scribe", "stt.asr_cloud", False),
    ("", "qwen3", "stt.asr_qwen3", False)])
def test_an_unknown_asr_backend_warns_and_runs_the_default(tmp_path, raw, want, mod, warns):
    """A typo ('mlx-whisper', 'whisper', or the 'whisperx' stt.env.example
    named) ran the default engine with no message. "mlxwhisper" named the
    Whisper engine, which is gone: it warns the same way. The default is
    qwen3, which falls back to Parakeet itself when it cannot run."""
    rc, out, err = _import_asr(tmp_path, raw)
    assert rc == 0, err
    assert out.split() == [want, mod]
    assert ("STT_ASR_BACKEND" in err and "using qwen3" in err) is warns


def test_a_whisper_setting_left_in_stt_env_warns_once_and_runs_the_default(tmp_path):
    """An stt.env written before Whisper was removed still says
    STT_ASR_BACKEND=mlxwhisper (run.sh exports it). One process warns once,
    names the removal, and runs the default, qwen3. With no Qwen
    environment here it estimates as Parakeet, the engine that will run,
    and a Parakeet run has no second opinion."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "stt.env").write_text(
        "STT_ASR_BACKEND=mlxwhisper\nSTT_WHISPER_MLX_MODEL=turbo\n")
    e = {k: v for k, v in os.environ.items()
         if k not in ("STT_ASR_BACKEND", "STT_WHISPER_MLX_MODEL")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo),
              "STT_ASR_BACKEND": "mlxwhisper", "STT_WHISPER_MLX_MODEL": "turbo",
              "STT_QWEN_ASR_PY": str(tmp_path / "no-venv-qwen" / "python")})
    code = ("from stt import config as c, pipeline as p, rates as r, verify as v; "
            "mods = {p._load_asr(False).__name__ for _ in range(3)}; "
            "print(c.ASR_BACKEND, *mods, r.current_asr_key(), "
            "v.secondary_engine(r.current_asr_key()))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=e, cwd=str(repo), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["qwen3", "stt.asr_qwen3", "parakeet", "None"]
    warned = [ln for ln in r.stderr.splitlines() if "STT_ASR_BACKEND" in ln]
    assert len(warned) == 1, r.stderr
    assert "the Whisper engine was removed" in warned[0]
    assert "using qwen3" in warned[0]


def test_the_doctor_names_an_unknown_asr_backend(sandbox, monkeypatch):
    from stt import config
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=whisper\n")
    monkeypatch.delenv("STT_ASR_BACKEND", raising=False)
    assert [p[0] for p in config.setting_problems()] == ["STT_ASR_BACKEND"]
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=mlxwhisper\n")
    [(name, why)] = config.setting_problems()
    assert name == "STT_ASR_BACKEND"
    assert "the Whisper engine was removed" in why and "using qwen3" in why
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=parakeet\n")
    assert config.setting_problems() == []
    from stt import asr_cloud
    assert set(config.CLOUD_ASR) == set(asr_cloud.PROVIDERS)


@pytest.mark.parametrize("value, mod, warns", [
    ("garbage", "stt.asr_qwen3", True),
    ("cloud:bogus", "stt.asr_qwen3", True),
    (" MLXWhisper ", "stt.asr_qwen3", True),
    ("parakeet", "stt.asr_parakeet", False),
    ("qwen3", "stt.asr_qwen3", False)])
def test_a_backend_set_after_load_is_checked_too(sandbox, monkeypatch, capsys,
                                                value, mod, warns):
    """The warning fired only when the config loaded. Code that set
    config.ASR_BACKEND afterward ran the default with no word, and
    'cloud:bogus' reached the cloud module."""
    from stt import pipeline
    monkeypatch.setattr(config, "ASR_BACKEND", value)
    assert pipeline._load_asr().__name__ == mod
    err = capsys.readouterr().err
    assert ("STT_ASR_BACKEND" in err and "using qwen3" in err) is warns

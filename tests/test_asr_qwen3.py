"""The Qwen3-ASR engine (stt/asr_qwen3.py) against a fake child process.

The fake speaks the worker's line protocol. Its "audio" is one word per
second: word w<t> sits at [t + 0.2, t + 0.6], and every tenth word ends a
sentence ("w9."). A chunk hears the words wholly inside it, and the fake
aligner returns their true times from wherever its window starts, with the
punctuation removed as the real aligner does. So the right answer is known
exactly: w0 .. w<N-1>, once each, in order, at their true times.
"""
import ast
import json
import sys
import wave
from pathlib import Path

import pytest

from stt import asr_qwen3, config, pipeline, rates, verify

FAKE_WORKER = r'''
import json, math, os, sys
args = sys.argv[1:]
wav = args[args.index("--wav") + 1]
mode = os.environ.get("FAKE_QWEN_MODE", "")
log = os.environ.get("FAKE_QWEN_LOG")
loop_at = os.environ.get("FAKE_QWEN_LOOP_AT")

def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": args, "offline": os.environ.get("HF_HUB_OFFLINE"),
                             "pythonpath": os.environ.get("PYTHONPATH")}) + "\n")
if mode == "missing_model":
    send({"error": "model mlx-community/Qwen3-ASR-1.7B-bf16 is not downloaded "
                   "(LocalEntryNotFoundError)", "unavailable": True})
    sys.exit(3)
if mode == "crash_at_start":
    sys.stderr.write("Traceback: ImportError: no module named mlx_audio\n")
    sys.exit(1)
send({"ready": True})

def heard(s, e):
    return [t for t in range(int(math.floor(s)), int(math.ceil(e)) + 1)
            if t + 0.2 >= s and t + 0.6 <= e]

def loops(s, e, penalty):
    if loop_at is None:
        return False
    at = float(loop_at)
    if abs(s - at) < 1e-6 and e - s > 200:
        return True                       # the full chunk loops
    if (mode in ("half_loops", "half_stuck") and at <= s and e <= at + 300 + 1e-6
            and e - s < 200):
        return mode == "half_stuck" or not penalty
    return False

n = 0
for line in sys.stdin:
    req = json.loads(line)
    if log:
        with open(log, "a") as fh:
            fh.write(line)
    op = req["op"]
    if op == "quit":
        break
    s, e = float(req["start"]), float(req["end"])
    if op == "transcribe":
        n += 1
        if mode == "die_mid_run" and n == 2:
            sys.exit(1)
        if loops(s, e, req.get("repetition_penalty")):
            send({"text": " ".join(["and so on"] * int(6 * (e - s) / 3))})
            continue
        send({"text": " ".join(f"w{t}." if t % 10 == 9 else f"w{t}" for t in heard(s, e))})
    elif op == "align":
        items = []
        for tok in req["text"].split():
            digits = "".join(ch for ch in tok if ch.isdigit())
            t = int(digits) if digits else s       # loop text: at the window start
            a, b = t + 0.2, t + 0.6
            if mode == "misalign" and e - s > 100 and 100 <= t - s < 110:
                a = b = s + 95 + 0.1 * (t - s - 100)   # packed early, no length
            elif mode == "misalign" and e - s > 100 and t - s == 110:
                a = s + 96.0                          # stretched to catch up
            items.append(["".join(ch for ch in tok if ch.isalnum()),
                          round(a - s, 3), round(b - s, 3)])
        send({"items": items})
send({"bye": True, "peak_gb": 1.5})
'''


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(seconds * 16000))
    return path


@pytest.fixture
def fake_child(tmp_path, monkeypatch):
    """config.QWEN_ASR_PY points at a fake interpreter that runs the fake
    worker, whatever script path it is handed. Returns the request log."""
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    py = tmp_path / "fake-python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{worker}" "$@"\n')
    py.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setattr(config, "QWEN_ASR_PY", py)
    monkeypatch.setattr(asr_qwen3, "model_dir", lambda repo: tmp_path)
    monkeypatch.setenv("FAKE_QWEN_LOG", str(log))
    monkeypatch.delenv("FAKE_QWEN_MODE", raising=False)
    monkeypatch.delenv("FAKE_QWEN_LOOP_AT", raising=False)
    return log


@pytest.fixture(autouse=True)
def _fresh_engine(monkeypatch):
    """Each test starts as a new process: no remembered failure, and no
    Qwen setting from the shell."""
    monkeypatch.setattr(asr_qwen3, "_unavailable", None)
    for k in ("STT_QWEN_ASR_PY", "STT_QWEN_ASR_MODEL", "STT_QWEN_ALIGNER_MODEL"):
        monkeypatch.delenv(k, raising=False)


def _requests(log):
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    return [r for r in rows if "op" in r]


def _expected(seconds):
    return [f"w{t}" for t in range(int(seconds)) if t + 0.6 <= seconds]


# ---------- the shape of the result ----------

def test_the_result_has_the_parakeet_shape_and_every_word_once(fake_child, tmp_path):
    """Three chunks (0-300, 285-585, 570-700), each aligned in windows: every
    word lands once, in order, at its true time, with its punctuation."""
    wav = _wav(tmp_path / "m.wav", 700)
    seen = []
    out = asr_qwen3.transcribe(wav, progress=seen.append)
    assert set(out) == {"text", "words", "engine"}
    assert out["engine"] == config.QWEN_ASR_MODEL
    assert all(set(w) == {"start", "end", "word"} for w in out["words"])
    got = [w["word"].rstrip(".") for w in out["words"]]
    assert got == _expected(700)
    for w in out["words"]:
        t = int(w["word"].strip("w."))
        assert w["start"] == pytest.approx(t + 0.2) and w["end"] == pytest.approx(t + 0.6)
        assert w["word"].endswith(".") is (t % 10 == 9)
    assert out["text"] == " ".join(w["word"] for w in out["words"])
    assert seen and seen[-1] == 1.0 and seen == sorted(seen)
    reqs = _requests(fake_child)
    assert [(r["start"], r["end"]) for r in reqs if r["op"] == "transcribe"] == \
        [(0.0, 300.0), (285.0, 585.0), (570.0, 700.0)]
    # a 300 s chunk is aligned twice: once whole, then from a pause in 150-190 s
    aligns = [(r["start"], r["end"]) for r in reqs if r["op"] == "align"]
    assert aligns[0] == (0.0, 300.0) and 150.0 <= aligns[1][0] <= 190.0
    assert aligns[-1] == (570.0, 700.0)        # 130 s: one call
    assert reqs[-1]["op"] == "quit"


def test_the_child_runs_isolated_and_offline(fake_child, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 20))
    first = json.loads(fake_child.read_text().splitlines()[0])
    assert first["argv"][0] == "-I" and first["argv"][1].endswith("qwen3_worker.py")
    assert first["argv"][first["argv"].index("--asr") + 1] == config.QWEN_ASR_MODEL
    assert first["argv"][first["argv"].index("--aligner") + 1] == config.QWEN_ALIGNER_MODEL
    assert first["offline"] == "1" and first["pythonpath"] is None


def test_a_short_file_and_an_empty_one(fake_child, tmp_path):
    out = asr_qwen3.transcribe(_wav(tmp_path / "short.wav", 12))
    assert [w["word"].rstrip(".") for w in out["words"]] == _expected(12)
    out = asr_qwen3.transcribe(_wav(tmp_path / "none.wav", 0.3))
    assert out["words"] == [] and out["text"] == ""


# ---------- the loop guard ----------

def test_a_looping_chunk_is_decoded_again_as_two_halves(fake_child, tmp_path, monkeypatch,
                                                         capsys):
    monkeypatch.setenv("FAKE_QWEN_LOOP_AT", "285")
    out = asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700))
    assert [w["word"].rstrip(".") for w in out["words"]] == _expected(700)
    said = capsys.readouterr().out
    assert "chunk at 285 s failed the loop guard" in said and "still fails" not in said
    tr = [(r["start"], r["end"], r.get("repetition_penalty"))
          for r in _requests(fake_child) if r["op"] == "transcribe"]
    assert (285.0, 442.5, None) in tr and (427.5, 585.0, None) in tr
    assert not any(p for _, _, p in tr)


def test_a_half_that_still_loops_gets_the_repetition_penalty(fake_child, tmp_path,
                                                             monkeypatch, capsys):
    monkeypatch.setenv("FAKE_QWEN_LOOP_AT", "285")
    monkeypatch.setenv("FAKE_QWEN_MODE", "half_loops")
    out = asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700))
    assert [w["word"].rstrip(".") for w in out["words"]] == _expected(700)
    pens = [r["repetition_penalty"] for r in _requests(fake_child)
            if r["op"] == "transcribe" and r.get("repetition_penalty")]
    assert pens == [asr_qwen3.REPETITION_PENALTY] * 2
    assert "still fails" not in capsys.readouterr().out


def test_a_half_that_loops_after_every_fallback_is_reported(fake_child, tmp_path,
                                                            monkeypatch, capsys):
    monkeypatch.setenv("FAKE_QWEN_LOOP_AT", "285")
    monkeypatch.setenv("FAKE_QWEN_MODE", "half_stuck")
    out = asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700))
    assert out["words"]                    # the file still gets a transcript
    assert "a half still fails" in capsys.readouterr().out


@pytest.mark.parametrize("text, seconds, flagged", [
    ("one two three four five", 1.0, False),            # exactly 5 words/s passes
    ("one two three four five six", 1.0, True),         # 6 words/s
    (" ".join(["a b c d"] * 8), 100.0, False),          # 8 repeats in a row pass
    (" ".join(["a b c d"] * 9), 100.0, True),           # 9 do not
    (" ".join(["a, b. C d"] * 9), 100.0, True),         # punctuation and case ignored
    ("", 300.0, False),
])
def test_the_guard_rules(text, seconds, flagged):
    assert bool(asr_qwen3.guard_reasons(text, seconds)) is flagged


def test_halves_join_on_their_longest_shared_run():
    a = "the board met and then voted on the plan".split()
    b = "voted on the plan for next year".split()
    assert asr_qwen3.merge_text(a, b) == "the board met and then voted on the plan for next year".split()
    assert asr_qwen3.merge_text(["x", "y"], ["z"]) == ["x", "y", "z"]   # nothing shared
    assert asr_qwen3.merge_text([], ["z"]) == ["z"]


# ---------- word times ----------

def test_the_aligned_words_keep_their_punctuation_and_case():
    items = [["Well", 0.1, 0.3], ["I", 0.5, 0.6], ["don't", 0.7, 0.9], ["know", 1.0, 1.2]]
    words = asr_qwen3.attach_times("Well, - I don't know.", items, 100.0, 300.0)
    assert [w["word"] for w in words] == ["Well,", "-", "I", "don't", "know."]
    assert words[0] == {"start": 100.1, "end": 100.3, "word": "Well,"}
    assert words[1] == {"start": 100.3, "end": 100.3, "word": "-"}   # no length, after "Well,"
    assert words[4]["end"] == 101.2


def test_items_that_do_not_line_up_keep_their_times():
    items = [["alpha", 1.0, 1.5], ["beta", 2.0, 9999.0]]
    words = asr_qwen3.attach_times("gamma delta", items, 10.0, 300.0)
    assert [w["word"] for w in words] == ["alpha", "beta"]
    assert words[1]["end"] == 310.0                   # clamped to the chunk


def test_the_seam_is_a_run_of_the_same_words_at_the_same_times():
    prev = [{"start": t + 0.2, "end": t + 0.6, "word": f"w{t}"} for t in range(0, 300)]
    nxt = [{"start": t + 0.25, "end": t + 0.6, "word": f"w{t}"} for t in range(285, 400)]
    got = asr_qwen3.merge_chunk_words(prev, nxt, 285.0, 300.0)
    assert [w["word"] for w in got] == [f"w{t}" for t in range(400)]
    # the words agree but their times do not: the run of words is the seam
    late = [{"start": t + 2.0, "end": t + 2.4, "word": f"w{t}"} for t in range(285, 400)]
    got = asr_qwen3.merge_chunk_words(prev, late, 285.0, 300.0)
    assert [w["word"] for w in got] == [f"w{t}" for t in range(400)]
    # no shared words at all: the seam is the middle of the overlap, by start
    other = [{"start": t + 0.2, "end": t + 0.6, "word": f"v{t}"} for t in range(285, 400)]
    got = asr_qwen3.merge_chunk_words(prev, other, 285.0, 300.0)
    names = [w["word"] for w in got]
    assert names == [f"w{t}" for t in range(293)] + [f"v{t}" for t in range(293, 400)]
    assert [w["start"] for w in got] == sorted(w["start"] for w in got)


def test_the_chunk_plan():
    assert asr_qwen3.chunk_plan(0) == []
    assert asr_qwen3.chunk_plan(100) == [(0.0, 100.0)]
    assert asr_qwen3.chunk_plan(300) == [(0.0, 300.0)]
    assert asr_qwen3.chunk_plan(301) == [(0.0, 300.0), (285.0, 301.0)]
    plan = asr_qwen3.chunk_plan(14791)
    assert plan[-1][1] == 14791 and all(b[0] == a[1] - 15 for a, b in zip(plan, plan[1:]))


# ---------- errors ----------

def test_no_interpreter_is_one_plain_error(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "QWEN_ASR_PY", tmp_path / "no-such-venv/bin/python")
    with pytest.raises(asr_qwen3.EngineUnavailable) as e:
        asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 5))
    assert "STT_QWEN_ASR_PY" in str(e.value) and "\n" not in str(e.value)
    # the next file in the same run does not try again
    monkeypatch.setattr(asr_qwen3, "_Worker", lambda wav: pytest.fail("tried again"))
    with pytest.raises(asr_qwen3.EngineUnavailable):
        asr_qwen3.transcribe(_wav(tmp_path / "m2.wav", 5))


def test_a_model_not_in_the_cache_is_named_before_any_child_starts(sandbox, monkeypatch,
                                                                    tmp_path, fake_child):
    monkeypatch.setattr(asr_qwen3, "model_dir",
                        lambda repo: None if "Aligner" in repo else tmp_path)
    assert "ForcedAligner" in asr_qwen3.problem()
    with pytest.raises(asr_qwen3.EngineUnavailable, match="is not downloaded"):
        asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 5))
    assert not fake_child.exists()              # no child was started


def test_the_settings_are_read_fresh_from_stt_env(sandbox, tmp_path):
    (config.PROJECT_DIR / "stt.env").write_text(
        f"STT_QWEN_ASR_PY={tmp_path}/py\nSTT_QWEN_ASR_MODEL=org/Qwen3-ASR-test\n")
    assert asr_qwen3.interpreter() == tmp_path / "py"
    assert asr_qwen3.asr_model() == "org/Qwen3-ASR-test"
    assert asr_qwen3.aligner_model() == config.QWEN_ALIGNER_MODEL


def test_a_model_folder_needs_its_config_and_weights(tmp_path):
    d = tmp_path / "local-model"
    d.mkdir()
    assert asr_qwen3.model_dir(str(d)) is None
    (d / "config.json").write_text("{}")
    assert asr_qwen3.model_dir(str(d)) == d


def test_a_missing_model_is_one_plain_error(fake_child, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_QWEN_MODE", "missing_model")
    with pytest.raises(asr_qwen3.EngineUnavailable, match="is not downloaded"):
        asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 5))


def test_a_child_that_cannot_start_says_why(fake_child, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_QWEN_MODE", "crash_at_start")
    with pytest.raises(asr_qwen3.EngineUnavailable, match="no module named mlx_audio"):
        asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 5))


def test_a_child_that_dies_mid_run_fails_the_file(fake_child, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_QWEN_MODE", "die_mid_run")
    with pytest.raises(RuntimeError, match="stopped") as e:
        asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700))
    assert not isinstance(e.value, asr_qwen3.EngineUnavailable)


def test_an_engine_that_cannot_run_falls_back_to_parakeet_with_one_warning(
        sandbox, monkeypatch, tmp_path, capsys):
    """No .venv-qwen: each file is transcribed by Parakeet, the meeting json
    names Parakeet, and one warning per run names the fix."""
    import types

    from stt import asr_parakeet, audio
    monkeypatch.setattr(config, "QWEN_ASR_PY", tmp_path / "no-such-venv/bin/python")
    monkeypatch.setattr(config, "ASR_BACKEND", "qwen3")
    monkeypatch.setattr(config, "PUNCTUATE", False)
    ran = []

    def _parakeet(wav, progress=None):
        ran.append(wav)
        return {"engine": config.PARAKEET_MODEL, "text": "hello there",
                "words": [{"start": 0.2, "end": 0.5, "word": "hello"},
                          {"start": 0.6, "end": 0.9, "word": "there"}]}
    monkeypatch.setattr(asr_parakeet, "transcribe", _parakeet)
    monkeypatch.setattr(audio, "to_wav16k", lambda src, wav: _wav(wav, 2))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    for n in (1, 2):
        src = config.PROJECT_DIR / f"Fallback Meeting {n} 05012026.m4a"
        src.write_bytes(b"x")
        res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                                    do_verify=False, track_unknowns=False)
        assert json.loads(res["json"].read_text())["asr_engine"] == config.PARAKEET_MODEL
    assert len(ran) == 2
    warned = [ln for ln in capsys.readouterr().err.splitlines() if "Qwen3-ASR" in ln]
    assert len(warned) == 1, warned
    assert "./init.sh" in warned[0] and "Parakeet" in warned[0]


def test_the_estimate_follows_the_engine_that_will_run(sandbox, monkeypatch, tmp_path):
    (config.PROJECT_DIR / "stt.env").write_text("STT_ASR_BACKEND=qwen3\n")
    monkeypatch.setattr(asr_qwen3, "problem", lambda deep=False: "no venv")
    assert rates.current_asr_key() == "parakeet"
    monkeypatch.setattr(asr_qwen3, "problem", lambda deep=False: None)
    assert rates.current_asr_key() == "qwen3"


# ---------- the setting ----------

def test_qwen3_is_a_backend_and_parakeet_stays_the_default(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(config, "ASR_BACKEND", "qwen3")
    assert pipeline._load_asr() is asr_qwen3
    assert pipeline._load_asr(strict=True) is asr_qwen3      # local: strict changes nothing
    monkeypatch.setattr(config, "ASR_BACKEND", " QWEN3 ")
    assert pipeline._load_asr() is asr_qwen3
    assert "STT_ASR_BACKEND" not in capsys.readouterr().err
    assert config._asr_problem("") == ("parakeet", None)
    assert config._asr_problem("qwen3") == ("qwen3", None)
    assert "qwen3" in config._asr_problem("qwen")[1]          # the warning names it


def test_qwen3_runs_are_filed_and_estimated_under_their_own_key(sandbox, monkeypatch):
    monkeypatch.setattr(asr_qwen3, "problem", lambda deep=False: None)
    assert rates.engine_key(config.QWEN_ASR_MODEL) == "qwen3"
    assert rates.engine_key("mlx-community/Qwen3-ASR-0.6B-8bit") == "qwen3"
    assert rates.engine_key(config.PARAKEET_MODEL) == "parakeet"
    (config.PROJECT_DIR / "stt.env").write_text("STT_ASR_BACKEND=qwen3\n")
    assert rates.current_asr_key() == "qwen3"
    assert rates.stage_secs("transcribing", 3600, 1) == pytest.approx(
        3600 / config.EST_RATES["asr"]["qwen3"])
    rates.record(1200.0, {"transcribing": 100.0}, "qwen3")
    rates.record(1200.0, {"transcribing": 40.0}, "parakeet")
    learned = rates.learned()["asr"]
    assert learned["qwen3@1"] == pytest.approx(12.0) and learned["parakeet@1"] == pytest.approx(30.0)
    # a second opinion on a Qwen transcript comes from Parakeet
    assert verify.secondary_engine(config.QWEN_ASR_MODEL) == "parakeet"
    assert verify.secondary_engine("qwen3") == "parakeet"


def test_the_worker_imports_nothing_from_this_package():
    """It runs with `python -I` in another venv: only the standard library at
    the top, and never `stt`."""
    tree = ast.parse(Path(asr_qwen3.WORKER).read_text())
    top = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert top <= set(sys.stdlib_module_names), top
    every = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    every |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m == "stt" or m.startswith("stt.") for m in every)
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))


# ---------- punctuation ----------

def test_the_punctuator_skips_qwen_transcripts(monkeypatch):
    from stt import punctuate
    monkeypatch.setattr(config, "PUNCTUATE", True)
    assert punctuate.runs_on(config.PARAKEET_MODEL)
    assert punctuate.runs_on("elevenlabs/scribe_v2")
    assert punctuate.runs_on(None)              # a meeting from before asr_engine
    assert not punctuate.runs_on(config.QWEN_ASR_MODEL)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    assert not punctuate.runs_on(config.PARAKEET_MODEL)


@pytest.mark.parametrize("engine, ran", [(config.QWEN_ASR_MODEL, False),
                                         (config.PARAKEET_MODEL, True)])
def test_a_qwen_transcript_keeps_its_own_punctuation(sandbox, monkeypatch, engine, ran):
    """The pipeline and relabel both skip the punctuator on a Qwen transcript,
    and the meeting json says whether it ran."""
    import types

    import relabel
    from stt import audio, diarcache, punctuate
    calls = []
    monkeypatch.setattr(punctuate, "restore_segments", lambda segs: calls.append(len(segs)))
    monkeypatch.setattr(config, "PUNCTUATE", True)
    words = [{"start": 0.2, "end": 0.5, "word": "Hello,"},
             {"start": 0.6, "end": 0.9, "word": "there."}]
    asr = types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": engine, "text": "Hello, there.", "words": words})
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: asr)
    monkeypatch.setattr(audio, "to_wav16k", lambda src, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    src = config.PROJECT_DIR / "Punct Meeting 05012026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False,
                                do_verify=False, track_unknowns=False)
    data = json.loads(res["json"].read_text())
    assert data["punctuated"] is ran and data["asr_engine"] == engine
    assert bool(calls) is ran
    assert data["segments"][0]["text"] == "Hello, there."
    # relabel rebuilds the segments and makes the same choice
    import numpy as np
    calls.clear()
    base = res["base"]
    diarcache.save(config.meeting_file(base, ".diar.npz"),
                   [{"start": 0.0, "end": 1.0, "cluster": "SPEAKER_00"}], [None],
                   {"SPEAKER_00": np.ones(256)})
    assert relabel.relabel_one(base) is True
    data = json.loads(res["json"].read_text())
    assert data["punctuated"] is ran and bool(calls) is ran


# ---------- repair of misplaced stretches ----------

def _true_words(lo, hi):
    return [{"start": t + 0.2, "end": t + 0.6, "word": f"w{t}"} for t in range(lo, hi)]


def test_clean_words_have_no_suspect_stretch():
    assert asr_qwen3.suspect_stretches(_true_words(0, 300)) == []
    # a lone dash has no length by design and is no mark
    ws = _true_words(0, 50)
    ws.insert(10, {"start": 9.6, "end": 9.6, "word": "-"})
    assert asr_qwen3.suspect_stretches(ws) == []


def test_a_packed_burst_before_a_stretched_word_is_suspect():
    ws = _true_words(0, 200)
    for t in range(100, 110):
        ws[t]["start"] = ws[t]["end"] = 95 + 0.1 * (t - 100)
    ws[110]["start"] = 96.0
    spans = asr_qwen3.suspect_stretches(ws)
    assert len(spans) == 1
    i0, i1 = spans[0]
    assert i0 <= 100 and i1 >= 110


class _FakeAligner:
    """worker.align for repair_chunk: true times for w<t>, or `bad` times."""

    def __init__(self, bad=False):
        self.calls, self.bad = [], bad

    def align(self, a, b, text):
        self.calls.append((a, b))
        out = []
        for tok in text.split():
            t = int(tok.strip("w."))
            st, en = (a, a) if self.bad else (t + 0.2, t + 0.6)
            out.append([tok.strip("."), st - a, en - a])
        return out


def test_a_suspect_stretch_is_aligned_again_between_its_neighbours():
    ws = _true_words(0, 200)
    for t in range(100, 110):
        ws[t]["start"] = ws[t]["end"] = 95 + 0.1 * (t - 100)
    ws[110]["start"] = 96.0
    fake = _FakeAligner()
    got = asr_qwen3.repair_chunk(fake, ws, 0.0, 300.0)
    assert got == _true_words(0, 200)
    # one call: words 90..122, between the end of w89 and the start of w123
    assert fake.calls == [(89.6, 123.2)]


def test_a_realignment_that_is_no_better_is_dropped():
    ws = _true_words(0, 200)
    for t in range(100, 110):
        ws[t]["start"] = ws[t]["end"] = 95 + 0.1 * (t - 100)
    ws[110]["start"] = 96.0
    before = [dict(w) for w in ws]
    got = asr_qwen3.repair_chunk(_FakeAligner(bad=True), ws, 0.0, 300.0)
    assert got == before


def test_the_engine_repairs_a_misplaced_stretch(fake_child, tmp_path, monkeypatch, capsys):
    """The fake aligner misplaces 11 words in every long window; the engine
    finds them and aligns them again on their own."""
    monkeypatch.setenv("FAKE_QWEN_MODE", "misalign")
    out = asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700))
    assert [w["word"].rstrip(".") for w in out["words"]] == _expected(700)
    for w in out["words"]:
        t = int(w["word"].strip("w."))
        assert w["start"] == pytest.approx(t + 0.2), w
    assert "word times repaired in" in capsys.readouterr().out


# ---------- progress ----------

def test_progress_moves_inside_every_chunk(fake_child, tmp_path):
    seen = []
    asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700), progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert len(seen) == 3 * 3                    # decode, align, repair per chunk
    # the first chunk's decode is 300 of 730 audio seconds planned, 85 percent done
    assert seen[0] == pytest.approx(300 * asr_qwen3.DECODE_SHARE / 730)


def test_progress_counts_the_guard_re_decodes_and_never_steps_back(fake_child, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setenv("FAKE_QWEN_LOOP_AT", "285")
    monkeypatch.setenv("FAKE_QWEN_MODE", "half_loops")
    seen = []
    asr_qwen3.transcribe(_wav(tmp_path / "m.wav", 700), progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert len(seen) == 3 * 3 + 2                # two halves re-decoded

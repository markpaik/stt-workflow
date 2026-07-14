"""tools/synth_corpus.py: script invariants, the pure layout math, render
determinism (macOS `say` is byte-stable for fixed inputs — verified here on a
real render), a tiny end-to-end generate, and the sandbox safety rails."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import diar_eval  # noqa: E402
import synth_corpus  # noqa: E402

from stt import config  # noqa: E402

HAVE_SAY = shutil.which("say") is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


# ------------------------------------------------------------ the scripts ---

def test_scripts_shape_and_coverage():
    convs = synth_corpus.CONVERSATIONS
    assert len(convs) == 5
    names = [c["name"] for c in convs]
    assert len(set(names)) == 5
    for c in convs:
        assert 2 <= len(c["speakers"]) <= 4
        for spk in c["speakers"]:
            assert spk in synth_corpus.CAST
        for turn in c["turns"]:
            spk = turn[0]
            assert spk in c["speakers"], f"{c['name']}: {spk} not in cast"
    # every cast member speaks somewhere, and has an enrollment monologue
    speaking = {t[0] for c in convs for t in c["turns"]}
    assert speaking == set(synth_corpus.CAST)
    assert set(synth_corpus.ENROLL_LINES) == set(synth_corpus.CAST)


def test_scripts_include_fillers_overlap_and_rapid_alternation():
    convs = synth_corpus.CONVERSATIONS
    # sub-second fillers: one-or-two-word turns, some rate-boosted
    fillers = [t for c in convs for t in c["turns"] if len(t[1].split()) <= 2]
    assert len(fillers) >= 10
    assert any(len(t) > 3 and t[3] for t in fillers), "no rate-boosted fillers"
    # deliberate overlap (negative gaps) in at least two conversations
    with_overlap = [c["name"] for c in convs
                    if any(len(t) > 2 and t[2] < 0 for t in c["turns"])]
    assert len(with_overlap) >= 2
    # rapid alternation: somewhere, four consecutive turns change speaker
    def rapid(c):
        spks = [t[0] for t in c["turns"]]
        return any(len({spks[i], spks[i + 1], spks[i + 2]}) >= 2
                   and spks[i] != spks[i + 1] != spks[i + 2] != spks[i + 3]
                   for i in range(len(spks) - 3))
    assert any(rapid(c) for c in convs)
    # the stereo-validation conversation exists and its mic owner speaks in it
    sv = synth_corpus.STEREO_VALIDATION
    conv = next(c for c in convs if c["name"] == sv["conversation"])
    assert sv["mic_speaker"] in conv["speakers"]


def test_script_fingerprint_is_stable_and_content_sensitive(monkeypatch):
    a = synth_corpus.script_fingerprint()
    assert a == synth_corpus.script_fingerprint()
    monkeypatch.setattr(synth_corpus, "DEFAULT_GAP", 0.99)
    assert synth_corpus.script_fingerprint() != a


# ------------------------------------------------------------ layout math ---

def test_layout_turns_gaps_and_overlaps():
    items = [("A", 2.0, 0.35),    # 0.35 .. 2.35
             ("B", 1.0, -0.5),    # overlaps A: 1.85 .. 2.85
             ("A", 0.4, 0.2)]     # after B: 3.05 .. 3.45
    turns = synth_corpus.layout_turns(items)
    assert [(t["start"], t["end"]) for t in turns] == \
        [(0.35, 2.35), (1.85, 2.85), (3.05, 3.45)]
    ov = synth_corpus.truth_overlaps(turns)
    assert ov == [[1.85, 2.35]]


def test_layout_never_goes_negative():
    turns = synth_corpus.layout_turns([("A", 1.0, -5.0)])
    assert turns[0]["start"] == 0.0


# ----------------------------------------------------------- determinism ----

@pytest.mark.skipif(not HAVE_SAY, reason="macOS say not available")
def test_say_render_is_deterministic(tmp_path):
    """`say` renders byte-identically for fixed (voice, rate, text) — the
    corpus's strongest determinism guarantee. If a macOS voice update ever
    breaks this, the contract falls back to the script fingerprint plus the
    rendered durations (the manifest stores both), and this test should be
    relaxed to duration equality."""
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    synth_corpus._render_say("Samantha", "The quick brown fox.", None, a)
    synth_corpus._render_say("Samantha", "The quick brown fox.", None, b)
    assert a.read_bytes() == b.read_bytes()
    xa, sra = sf.read(str(a))
    assert sra == synth_corpus.SAMPLE_RATE
    assert len(xa) > 0


TINY_CONVS = [{
    "name": "tiny",
    "speakers": ["Ava Sterling", "Ben Whitfield"],
    "turns": [
        ("Ava Sterling", "The garden looks better this year."),
        ("Ben Whitfield", "Yeah.", -0.3, 240),
        ("Ben Whitfield", "The roses finally took."),
        ("Ava Sterling", "Right.", 0.15, 240),
    ],
}]
TINY_ENROLL = {"Ava Sterling": "A short enrollment line for testing.",
               "Ben Whitfield": "Another short enrollment line for testing."}
TINY_STEREO = {"conversation": "tiny", "mic_speaker": "Ava Sterling",
               "bleed": 0.10}


def _tiny_scripts(monkeypatch):
    monkeypatch.setattr(synth_corpus, "CONVERSATIONS", TINY_CONVS)
    monkeypatch.setattr(synth_corpus, "ENROLL_LINES", TINY_ENROLL)
    monkeypatch.setattr(synth_corpus, "STEREO_VALIDATION", TINY_STEREO)


@pytest.mark.skipif(not (HAVE_SAY and HAVE_FFMPEG),
                    reason="needs say + ffmpeg")
def test_generate_tiny_corpus_truth_and_stereo(sandbox, tmp_path, monkeypatch):
    """End-to-end generate on a 4-turn script: manifest schema, truth spans
    that match the audio, annotated overlap, a 16k mono mix, and a stereo
    render on which the channel-energy labeler reproduces the scripted truth
    (its validation while no real recorder captures exist)."""
    _tiny_scripts(monkeypatch)
    corpus = tmp_path / "corpus"
    m = synth_corpus.generate(corpus)

    assert m["script_sha"] == synth_corpus.script_fingerprint()
    conv = m["conversations"][0]
    wav = corpus / conv["file"]
    assert wav.exists()
    info = sf.info(str(wav))
    assert info.samplerate == 16000 and info.channels == 1
    assert abs(info.duration - conv["duration"]) < 0.05
    # truth: 4 turns, speakers alternate per the script, spans ordered
    assert [t["speaker"] for t in conv["turns"]] == [
        "Ava Sterling", "Ben Whitfield", "Ben Whitfield", "Ava Sterling"]
    assert all(t["end"] > t["start"] for t in conv["turns"])
    # the negative-gap filler produced a truth overlap
    assert conv["overlaps"], "scripted overlap missing from the truth"
    # the filler really is sub-second in the truth
    fillers = [t for t in conv["turns"] if t["text"] in ("Yeah.", "Right.")]
    assert fillers and all(t["end"] - t["start"] < 1.0 for t in fillers)
    # audio energy exists inside a truth span and not before the first turn
    x, sr = sf.read(str(wav))
    t0 = conv["turns"][0]
    seg = x[int(t0["start"] * sr):int(t0["end"] * sr)]
    assert float(np.abs(seg).max()) > 0.01

    # enrollment clips rendered per cast member
    for name, rel in m["enroll"].items():
        assert (corpus / rel).exists()

    # stereo validation render: L carries the mic owner (plus bleed), R the rest
    sv = m["stereo_validation"]
    stereo = corpus / sv["file"]
    assert sf.info(str(stereo)).channels == 2
    val = diar_eval.validate_stereo_labeler(m, corpus)
    assert val["labeler_accuracy"] == 1.0
    assert val["labeled"] >= 3          # confident on nearly every turn
    assert val["n_turns"] == 4


@pytest.mark.skipif(not (HAVE_SAY and HAVE_FFMPEG),
                    reason="needs say + ffmpeg")
def test_generate_is_deterministic_across_runs(sandbox, tmp_path, monkeypatch):
    """Same scripts -> same truth spans and same file hashes (say permitting;
    byte-stability is asserted because it holds on this machine — see
    test_say_render_is_deterministic for the documented fallback)."""
    _tiny_scripts(monkeypatch)
    m1 = synth_corpus.generate(tmp_path / "c1")
    m2 = synth_corpus.generate(tmp_path / "c2")
    assert m1["script_sha"] == m2["script_sha"]
    t1 = [(t["speaker"], t["start"], t["end"]) for t in m1["conversations"][0]["turns"]]
    t2 = [(t["speaker"], t["start"], t["end"]) for t in m2["conversations"][0]["turns"]]
    assert t1 == t2
    assert m1["conversations"][0]["wav_sha256"] == m2["conversations"][0]["wav_sha256"]


# ------------------------------------------------------------ safety rails --

def test_build_home_refuses_real_data_dirs(sandbox):
    with pytest.raises(SystemExit):
        synth_corpus.build_home(config.MEETINGS_DIR / "evil_home")


def test_stages_refuse_outside_a_sandbox(sandbox, tmp_path, monkeypatch):
    # no STT_HOME at all
    monkeypatch.delenv("STT_HOME", raising=False)
    with pytest.raises(SystemExit):
        synth_corpus._assert_sandboxed()
    # STT_HOME set but pointing at a directory this tool did not build
    monkeypatch.setenv("STT_HOME", str(tmp_path / "not_a_home"))
    (tmp_path / "not_a_home").mkdir()
    with pytest.raises(SystemExit):
        synth_corpus._assert_sandboxed()


def test_ensure_corpus_refuses_unmarked_dir(sandbox, tmp_path, monkeypatch):
    """A stale/foreign directory at the corpus path is never wiped."""
    monkeypatch.setattr(synth_corpus, "CONVERSATIONS", TINY_CONVS)
    target = tmp_path / "corpus"
    target.mkdir()
    (target / "somebody_elses_file.txt").write_text("precious")
    with pytest.raises(SystemExit):
        synth_corpus.ensure_corpus(target)
    assert (target / "somebody_elses_file.txt").exists()

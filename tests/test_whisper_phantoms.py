"""Whisper on silence (stt/asr_mlxwhisper.py).

large-v3-turbo writes 'Thank you.' for a silent file, a 1 s tone and a
50 ms clip. Its scores cannot tell that from speech: measured on this
machine, silence gave no_speech_prob 0.0 and avg_logprob -0.25, the same
range as the spoken phrase. Parakeet gives empty text on the same audio.
These tests stub mlx_whisper, so no model runs. All audio is synthetic.
"""
import sys
import types
import wave

import numpy as np
import pytest

from stt import asr_mlxwhisper

SR = 16000


def _wav(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def _seg(text, start, end):
    words = text.split()
    step = (end - start) / max(1, len(words))
    return {"text": " " + text, "start": start, "end": end,
            "no_speech_prob": 0.0, "avg_logprob": -0.3,
            "words": [{"word": " " + w, "start": start + i * step,
                       "end": start + (i + 1) * step} for i, w in enumerate(words)]}


@pytest.fixture
def whisper(monkeypatch):
    """Stub mlx_whisper: each test sets the segments it returns."""
    out = {"segments": []}
    mod = types.ModuleType("mlx_whisper")
    mod.transcribe = lambda *a, **k: {
        "text": "".join(s["text"] for s in out["segments"]), "segments": out["segments"]}
    monkeypatch.setitem(sys.modules, "mlx_whisper", mod)
    return out


def _speech_like(seconds):
    t = np.arange(int(SR * seconds)) / SR
    # a 4 Hz syllable envelope over a 220 Hz voice: loud, like speech
    return 0.3 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t))


@pytest.mark.parametrize("audio", ["silence", "tone", "short"])
def test_a_phantom_phrase_alone_is_no_speech(tmp_path, whisper, audio):
    t = np.arange(SR) / SR
    samples = {"silence": np.zeros(SR * 5),
               "tone": 0.5 * np.sin(2 * np.pi * 300 * t),
               "short": 0.5 * np.sin(2 * np.pi * 300 * t[:800])}[audio]
    wav = _wav(tmp_path / f"{audio}.wav", samples)
    whisper["segments"] = [_seg("Thank you.", 0.0, len(samples) / SR)]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "" and r["words"] == []


def test_a_phantom_phrase_over_a_quiet_stretch_is_dropped_and_speech_kept(tmp_path, whisper):
    samples = np.concatenate([_speech_like(3.0), np.zeros(SR * 4)])
    wav = _wav(tmp_path / "meeting.wav", samples)
    whisper["segments"] = [_seg("We will ship the survey on Friday.", 0.0, 3.0),
                           _seg("Thank you.", 4.0, 6.5)]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "We will ship the survey on Friday."
    assert [w["word"] for w in r["words"]][-1] == "Friday."


def test_a_spoken_thank_you_inside_a_meeting_is_kept(tmp_path, whisper):
    wav = _wav(tmp_path / "meeting.wav", _speech_like(6.0))
    whisper["segments"] = [_seg("We will ship the survey on Friday.", 0.0, 4.0),
                           _seg("Thank you.", 4.2, 5.5)]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "We will ship the survey on Friday. Thank you."
    assert len(r["words"]) == 9


def test_an_unreadable_wav_skips_only_the_quiet_check(tmp_path, whisper):
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not a wav")
    whisper["segments"] = [_seg("We will ship it.", 0.0, 2.0)]
    assert asr_mlxwhisper.transcribe(bad)["text"] == "We will ship it."


def test_a_memo_of_a_spoken_thank_you_is_kept(tmp_path, whisper):
    """A transcript of phantom phrases only was always no speech, so a memo
    whose only words were a real 'Thank you.' transcribed to nothing. The
    sound under it now decides: speech swings in level, a tone does not."""
    wav = _wav(tmp_path / "memo.wav", _speech_like(0.6))
    whisper["segments"] = [_seg("Thank you.", 0.0, 0.6)]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "Thank you." and len(r["words"]) == 2


@pytest.mark.parametrize("kind", ["white", "hum"])
def test_steady_noise_is_not_speech_like(tmp_path, whisper, kind):
    rng = np.random.default_rng(7)
    t = np.arange(SR * 6) / SR
    samples = {"white": 0.02 * rng.standard_normal(SR * 6),
               "hum": 0.05 * np.sin(2 * np.pi * 60 * t)}[kind]
    wav = _wav(tmp_path / f"{kind}.wav", samples)
    whisper["segments"] = [_seg("Thank you.", 0.0, 5.4)]
    assert asr_mlxwhisper.transcribe(wav)["text"] == ""


def test_a_loop_over_noise_is_dropped(tmp_path, whisper):
    """Over steady noise turbo wrote "if you got your hand here, ..." again
    and again at temperature 1.0, compression ratio 10.2: its fallback could
    not clear the loop and kept the last try."""
    rng = np.random.default_rng(3)
    wav = _wav(tmp_path / "noise.wav", 0.02 * rng.standard_normal(SR * 6))
    loop = "if you got your hand here, " * 8
    whisper["segments"] = [{**_seg(loop, 0.0, 3.4), "compression_ratio": 10.2},
                           {**_seg(loop, 3.4, 3.9), "compression_ratio": 10.2}]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "" and r["words"] == []


def test_a_loop_over_speech_is_kept_for_review(tmp_path, whisper):
    """A failed decode over real speech still holds some of what was said:
    it stays, and the verify pass flags it."""
    wav = _wav(tmp_path / "talk.wav", _speech_like(4.0))
    whisper["segments"] = [{**_seg("no no no no no no no no", 0.0, 4.0),
                            "compression_ratio": 3.1}]
    assert asr_mlxwhisper.transcribe(wav)["text"] == "no no no no no no no no"


def test_punctuation_alone_is_no_speech(tmp_path, whisper):
    t = np.arange(SR * 8) / SR
    wav = _wav(tmp_path / "hum.wav", 0.03 * np.sin(2 * np.pi * 120 * t))
    whisper["segments"] = [_seg(".", 0.0, 1.72)]
    r = asr_mlxwhisper.transcribe(wav)
    assert r["text"] == "" and r["words"] == []


def test_only_the_suspect_spans_are_read(tmp_path, whisper, monkeypatch):
    """The phantom check read the whole wav into memory for every
    transcription: about 1.8 GB for a 4-hour file. It now reads only the
    span of a phantom or a loop, and nothing for plain speech."""
    wav = _wav(tmp_path / "long.wav", _speech_like(20.0))
    reads = []
    real = asr_mlxwhisper.wave.Wave_read.readframes

    def spy(self, n):
        reads.append(n)
        return real(self, n)
    monkeypatch.setattr(asr_mlxwhisper.wave.Wave_read, "readframes", spy)
    whisper["segments"] = [_seg("We will ship the survey on Friday.", 0.0, 18.0)]
    asr_mlxwhisper.transcribe(wav)
    assert reads == []
    whisper["segments"] = [_seg("We will ship the survey on Friday.", 0.0, 18.0),
                           _seg("Thank you.", 18.0, 19.0)]
    asr_mlxwhisper.transcribe(wav)
    assert reads and max(reads) <= SR * 1 + 1

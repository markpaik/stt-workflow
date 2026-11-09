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

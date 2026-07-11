"""channels: energy-dominance mic-span detection + sanity + turn overlay.
Pure DSP (constructed arrays via soundfile) plus a real-ffmpeg channel split."""
import subprocess

import numpy as np
import soundfile as sf

from stt import audio, channels, config


def _write(path, x, sr=16000):
    sf.write(str(path), np.asarray(x, dtype=np.float32), sr)
    return path


def _tone(sr, secs, amp=0.3, f=300):
    t = np.arange(int(sr * secs)) / sr
    return amp * np.sin(2 * np.pi * f * t)


def test_mic_dominant_yields_one_span(sandbox, tmp_path):
    sr = 16000
    # 0-1s silence, 1-3s mic loud + system quiet (me speaking), 3-4s silence
    mic = np.concatenate([_tone(sr, 1, 0.0), _tone(sr, 2, 0.5), _tone(sr, 1, 0.0)])
    sysd = np.concatenate([_tone(sr, 1, 0.0), _tone(sr, 2, 0.02), _tone(sr, 1, 0.0)])
    spans = channels.mic_spans(_write(tmp_path / "m.wav", mic),
                               _write(tmp_path / "s.wav", sysd))
    assert len(spans) == 1
    s, e = spans[0]
    assert 0.9 < s < 1.4 and 2.7 < e < 3.2   # roughly the 1-3s window (with hysteresis)


def test_bleed_yields_no_span(sandbox, tmp_path):
    sr = 16000
    # speakers day: system is loud, mic hears an ATTENUATED copy (bleed) — the
    # mic never dominates, so nothing should be attributed to "me"
    sysd = _tone(sr, 3, 0.5)
    mic = 0.15 * sysd   # attenuated copy, ~-16 dB below system
    spans = channels.mic_spans(_write(tmp_path / "m.wav", mic),
                               _write(tmp_path / "s.wav", sysd))
    assert spans == []


def test_alternating_turns(sandbox, tmp_path):
    sr = 16000
    # me (0-2), them (2-4), me (4-6)
    mic = np.concatenate([_tone(sr, 2, 0.5), _tone(sr, 2, 0.0), _tone(sr, 2, 0.5)])
    sysd = np.concatenate([_tone(sr, 2, 0.02), _tone(sr, 2, 0.5), _tone(sr, 2, 0.02)])
    spans = channels.mic_spans(_write(tmp_path / "m.wav", mic),
                               _write(tmp_path / "s.wav", sysd))
    assert len(spans) == 2


def test_sanity_flags(sandbox, tmp_path):
    sr = 16000
    loud = _tone(sr, 2, 0.5)
    # identical channels -> dual mono
    st = channels.sanity(_write(tmp_path / "a.wav", loud),
                         _write(tmp_path / "b.wav", loud))
    assert st["dual_mono"]
    # dead system channel
    st2 = channels.sanity(_write(tmp_path / "c.wav", loud),
                          _write(tmp_path / "d.wav", _tone(sr, 2, 0.0)))
    assert st2["sys_dead"] and not st2["dual_mono"]
    # dead mic channel (G4: was computed but never asserted)
    st3 = channels.sanity(_write(tmp_path / "e.wav", _tone(sr, 2, 0.0)),
                          _write(tmp_path / "f.wav", loud))
    assert st3["mic_dead"] and not st3["sys_dead"]


def test_real_channel_analysis_is_json_serializable(sandbox, tmp_path):
    """The first real stereo recordings failed the WHOLE pipeline with 'Object
    of type bool is not JSON serializable': sanity() built its flags from
    np.float64 comparisons (np.bool_) and mic_spans emitted np.float64
    timestamps, all of which land in the meeting JSON. The pipeline tests
    mocked these with plain Python values, so only real audio could catch it —
    this runs the REAL functions and round-trips their output through
    json.dumps, exactly like output.write_json does."""
    import json
    sr = 16000
    mic = np.concatenate([_tone(sr, 1, 0.0), _tone(sr, 2, 0.5)])
    sysd = np.concatenate([_tone(sr, 1, 0.5), _tone(sr, 2, 0.02)])
    st = channels.sanity(_write(tmp_path / "m.wav", mic),
                         _write(tmp_path / "s.wav", sysd))
    spans = channels.mic_spans(tmp_path / "m.wav", tmp_path / "s.wav")
    assert spans  # the fixture really produces a span, so the types are real
    round_tripped = json.loads(json.dumps({"stats": st, "spans": spans}))
    assert round_tripped["stats"]["mic_dead"] is False
    for v in st.values():
        assert not type(v).__module__.startswith("numpy"), (v, type(v))


def test_mic_spans_handles_silence_and_tiny_audio(sandbox, tmp_path):
    """G4: numeric edges must not crash or divide by zero — all-silence yields no
    spans, and audio shorter than one analysis frame is handled cleanly."""
    sr = 16000
    silent = _tone(sr, 2, 0.0)
    assert channels.mic_spans(_write(tmp_path / "m.wav", silent),
                              _write(tmp_path / "s.wav", silent)) == []
    tiny = _tone(sr, 0.01, 0.5)   # 10 ms, shorter than the ~30 ms frame window
    assert channels.mic_spans(_write(tmp_path / "t1.wav", tiny),
                              _write(tmp_path / "t2.wav", tiny)) == []


def test_combine_turns_overlays_and_trims_overlap():
    # system has a turn 2.0-3.0; a mic span 1.5-2.5 overlaps it -> the mic turn
    # is trimmed to 1.5-2.0 and 2.0-2.5 becomes a review overlap
    sys_turns = [{"start": 2.0, "end": 3.0, "speaker": "SPEAKER_00"}]
    names = {"SPEAKER_00": {"name": "Alex Rivera", "score": 0.9}}
    turns, out_names, extra = channels.combine_turns(
        sys_turns, names, [(1.5, 2.5)], "Mark Paik", 0.8)
    mic_turns = [t for t in turns if t["speaker"] == channels.MIC_ID]
    assert len(mic_turns) == 1
    assert abs(mic_turns[0]["start"] - 1.5) < 1e-6 and abs(mic_turns[0]["end"] - 2.0) < 1e-6
    assert out_names[channels.MIC_ID]["name"] == "Mark Paik"
    assert (2.0, 2.5) in extra


def test_channel_split_recovers_each_side(sandbox, tmp_path):
    """Real ffmpeg: a stereo file with 440 Hz left / 880 Hz right splits back to
    the correct per-channel tones (model: the real-ffmpeg extract tests)."""
    stereo = tmp_path / "stereo.wav"
    subprocess.run(
        [audio.FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
         "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(stereo)], check=True, capture_output=True)
    assert audio.probe_channels(stereo) == 2
    left = audio.to_wav16k_channel(stereo, tmp_path / "L.wav", 0)
    right = audio.to_wav16k_channel(stereo, tmp_path / "R.wav", 1)

    def peak_hz(path):
        x, sr = sf.read(str(path))
        spec = np.abs(np.fft.rfft(x))
        return np.fft.rfftfreq(len(x), 1 / sr)[np.argmax(spec)]

    assert abs(peak_hz(left) - 440) < 20
    assert abs(peak_hz(right) - 880) < 20

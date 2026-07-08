"""enroll.py --audio path: the talk-time tally that picks the clip's dominant
speaker must key by the same raw cluster label as the embeddings dict."""
import sys

import numpy as np

import enroll
from stt import audio, diarize, identify


def test_audio_enroll_picks_dominant_cluster_not_minority(sandbox, monkeypatch):
    """The clip's dominant voice already has a voiceprint, so its turns carry
    the enrolled NAME while embeddings stay keyed by SPEAKER_0N; a brief second
    cluster must not be enrolled in the dominant speaker's place."""
    vec_a = np.ones(256, dtype=float)          # 30s dominant cluster
    vec_b = np.ones(256, dtype=float) * -1.0   # 2s minority/noise cluster
    fake = {
        # post-refine 'speaker' is the enrolled name for the matched cluster
        "turns": [{"speaker": "Mark", "start": 0.0, "end": 30.0},
                  {"speaker": "SPEAKER_01", "start": 30.0, "end": 32.0}],
        "raw_turns": [{"cluster": "SPEAKER_00", "start": 0.0, "end": 30.0},
                      {"cluster": "SPEAKER_01", "start": 30.0, "end": 32.0}],
        "embeddings": {"SPEAKER_00": vec_a, "SPEAKER_01": vec_b},
    }
    monkeypatch.setattr(audio, "to_wav16k", lambda src, dst: dst.write_bytes(b"wav"))
    monkeypatch.setattr(diarize, "diarize", lambda wav: fake)
    captured = {}

    def fake_enroll(name, vec, replace=False, source=None):
        captured["name"] = name
        captured["vec"] = np.asarray(vec)
        return sandbox / "voiceprints" / "mark.npy"

    monkeypatch.setattr(identify, "enroll", fake_enroll)
    monkeypatch.setattr(sys, "argv",
                        ["enroll", "--audio", "mark.m4a", "--name", "Mark"])
    assert enroll.main() == 0
    # the 30s cluster (SPEAKER_00 -> vec_a) must win, not the 2s SPEAKER_01
    assert np.array_equal(captured["vec"], vec_a)

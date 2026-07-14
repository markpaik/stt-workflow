"""enroll.py --audio path: the talk-time tally that picks the clip's dominant
speaker must key by the same raw cluster label as the embeddings dict.
Plus the --from-meeting quality gate: thin clusters are refused outright and
a suspect sample (wrong-cluster enrollment) demands an explicit --confirm."""
import sys

import numpy as np
import pytest

import enroll
from stt import audio, diarcache, diarize, identify
from conftest import mfile


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


def _seed_cached_meeting(base, cluster_turns, cent_emb):
    """.emb.npz (what --from-meeting reads) + .diar.npz (what the quality
    gate measures talk evidence from)."""
    np.savez(mfile(base, ".emb.npz"), **cent_emb)
    raw = sorted(({"start": s, "end": e, "cluster": lbl}
                  for lbl, spans in cluster_turns.items() for s, e in spans),
                 key=lambda t: t["start"])
    diarcache.save(mfile(base, ".diar.npz"), raw, [None] * len(raw),
                   {k: np.asarray(v, float) for k, v in cent_emb.items()})


def test_from_meeting_refuses_a_thin_cluster(sandbox, monkeypatch):
    """A cluster carrying seconds of speech can't identify anyone: the CLI
    refuses just like /api/name (same floor, same plain language)."""
    v = np.random.default_rng(3).normal(size=256)
    _seed_cached_meeting("Mtg", {"SPEAKER_00": [(0.0, 2.0), (5.0, 6.5)]},
                         {"SPEAKER_00": v})
    monkeypatch.setattr(sys, "argv", ["enroll", "--from-meeting", "Mtg",
                                      "--speaker", "SPEAKER_00", "--name", "New Person"])
    with pytest.raises(SystemExit) as ei:
        enroll.main()
    assert "too little to identify anyone reliably" in str(ei.value)
    assert "New Person" not in identify.load_registry()


def test_from_meeting_suspect_sample_requires_confirm(sandbox, monkeypatch):
    """Enrolling the OTHER cluster of the same meeting onto a person whose
    stack already covers that meeting: refuse with the numbers unless
    --confirm says the human really means it."""
    rng = np.random.default_rng(5)
    priya = rng.normal(size=256)
    priya /= np.linalg.norm(priya)
    other = rng.normal(size=256)
    other -= (other @ priya) * priya
    other /= np.linalg.norm(other)
    spans = [(i * 4.0, i * 4.0 + 3.0) for i in range(12)]
    _seed_cached_meeting("Mtg", {"SPEAKER_00": spans,
                                 "SPEAKER_01": [(s + 60, e + 60) for s, e in spans]},
                         {"SPEAKER_00": priya, "SPEAKER_01": other})
    identify.enroll("Priya Shah", priya, source="Mtg")

    argv = ["enroll", "--from-meeting", "Mtg", "--speaker", "SPEAKER_01",
            "--name", "Priya Shah"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as ei:
        enroll.main()
    assert "--confirm" in str(ei.value)
    assert identify.load_registry()["Priya Shah"]["n_samples"] == 1

    monkeypatch.setattr(sys, "argv", argv + ["--confirm"])
    assert enroll.main() == 0
    assert identify.load_registry()["Priya Shah"]["n_samples"] == 2

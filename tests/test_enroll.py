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


BAD_NAMES = ["", "   ", "\t", "Bad\nName", "Bad\rName", "A‮B", "Zed​Q",
             "Line Two", "Nul\x00l", "C1\x85x", "﻿Lead"]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_enroll_refuses_an_empty_or_unsafe_name(sandbox, bad):
    """'   ' was enrolled as '   .npy', '' as '.npy', and 'Bad\\nName' as a
    person whose name split the transcript header in two. Nothing is written."""
    from stt import names
    v = np.random.default_rng(1).normal(size=256)
    with pytest.raises(names.BadName):
        identify.enroll(bad, v, source="M1")
    assert identify.load_registry() == {}
    assert list((sandbox / "voiceprints").glob("*.npy")) == []


def test_enroll_trims_the_edges_and_keeps_joiners(sandbox):
    v = np.random.default_rng(1).normal(size=256)
    identify.enroll("Zed Q ", v, source="M1")
    identify.enroll("Zed Q", v * 1.01, source="M2")
    assert list(identify.load_registry()) == ["Zed Q"]
    # U+200D (zero-width joiner) is part of real names and emoji: it stays
    identify.enroll("Ana‍Lee", v * -1, source="M3")
    assert "Ana‍Lee" in identify.load_registry()


def test_rename_and_reassign_refuse_an_unsafe_name(sandbox):
    from stt import names
    v = np.random.default_rng(1).normal(size=256)
    identify.enroll("Alex Rivera", v, source="M1")
    identify.enroll("Alex Rivera", v * 1.1 + 0.2, source="M2")
    assert identify.rename_person("Alex Rivera", "Alex\nRivera") is False
    assert identify.rename_person("Alex Rivera", "   ") is False
    r = identify.reassign_sample("Alex Rivera", 0, "Sam‮Chen")
    assert r == {"ok": False, "error": names.UNSAFE_NAME}
    assert list(identify.load_registry()) == ["Alex Rivera"]


@pytest.mark.parametrize("bad, msg", [
    ("   ", "--name: Type a name first."),
    ("Bad\nName", "--name: A name cannot hold a line break, a tab, or an "
                  "invisible control character.")])
def test_the_cli_refuses_an_empty_or_unsafe_name(sandbox, monkeypatch, bad, msg):
    v = np.random.default_rng(3).normal(size=256)
    _seed_cached_meeting("Mtg", {"SPEAKER_00": [(0.0, 200.0)]}, {"SPEAKER_00": v})
    monkeypatch.setattr(sys, "argv", ["enroll", "--from-meeting", "Mtg",
                                      "--speaker", "SPEAKER_00", "--name", bad])
    with pytest.raises(SystemExit) as ei:
        enroll.main()
    assert str(ei.value) == msg
    assert identify.load_registry() == {}


def test_the_cli_trims_the_name_it_enrolls(sandbox, monkeypatch):
    v = np.random.default_rng(3).normal(size=256)
    # forty clear 5 s turns: well over the quality floor
    _seed_cached_meeting("Mtg", {"SPEAKER_00": [(i * 6.0, i * 6.0 + 5.0)
                                                for i in range(40)]},
                         {"SPEAKER_00": v})
    monkeypatch.setattr(sys, "argv", ["enroll", "--from-meeting", "Mtg",
                                      "--speaker", "SPEAKER_00", "--name", "  Jo Lee  "])
    enroll.main()
    assert list(identify.load_registry()) == ["Jo Lee"]

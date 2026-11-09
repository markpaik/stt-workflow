"""Turn-level recluster (stt/recluster.py) and its place in diarize():
misfit turns move to a better cluster, small clusters never absorb anyone,
turn times never change, and the step runs only on the speakrs engine."""
import numpy as np

from stt import config, diarize, recluster, speakrs_diar

A = np.array([1.0, 0.0, 0.0])
B = np.array([0.0, 1.0, 0.0])


def _two_speakers(b_secs=130.0, odd_emb=B, odd_dur=1.0):
    """Cluster A: 13 x 10 s turns on A. Cluster B: turns on B summing to
    b_secs. One short turn labelled A carries odd_emb."""
    turns, embs, t = [], [], 0.0
    for _ in range(13):
        turns.append({"start": t, "end": t + 10.0, "cluster": "A"})
        embs.append(A)
        t += 10.0
    left = b_secs
    while left > 0:
        d = min(10.0, left)
        turns.append({"start": t, "end": t + d, "cluster": "B"})
        embs.append(B)
        t += d
        left -= d
    turns.append({"start": t, "end": t + odd_dur, "cluster": "A"})
    embs.append(odd_emb)
    return turns, embs


def test_misfit_turn_moves_to_the_cluster_it_sounds_like():
    turns, embs = _two_speakers()
    labels, moves = recluster.reassign(turns, embs)
    assert labels[-1] == "B"
    assert moves == 1
    assert labels[:-1] == [t["cluster"] for t in turns[:-1]]


def test_small_destination_cluster_never_absorbs_a_turn():
    """B has 60 s of centroid-building talk, under the 120 s floor: a one-off
    speaker (public comment) must not collect other people's turns."""
    turns, embs = _two_speakers(b_secs=60.0)
    labels, moves = recluster.reassign(turns, embs)
    assert labels[-1] == "A"
    assert moves == 0


def test_near_tie_needs_the_margin():
    """0.01 cosine better elsewhere is under the 0.02 margin: no move."""
    c = np.cos(np.pi / 4 - 0.005)
    tie = np.array([np.sqrt(1 - c * c), c, 0.0])   # B side by a hair
    turns, embs = _two_speakers(odd_emb=tie)
    labels, _ = recluster.reassign(turns, embs)
    assert labels[-1] == "A"
    labels, _ = recluster.reassign(turns, embs, margin=0.0)
    assert labels[-1] == "B"


def test_turn_too_short_to_move_stays():
    turns, embs = _two_speakers(odd_dur=0.2)
    labels, moves = recluster.reassign(turns, embs)
    assert labels[-1] == "A" and moves == 0


def test_missing_and_bad_embeddings_never_vote_or_move():
    turns, embs = _two_speakers()
    embs[-1] = None
    embs[0] = np.array([np.nan, 1.0, 0.0])
    embs[1] = np.zeros(3)
    labels, moves = recluster.reassign(turns, embs)
    assert moves == 0
    assert labels == [t["cluster"] for t in turns]


def test_no_embeddings_is_a_no_op():
    turns, _ = _two_speakers()
    assert recluster.reassign(turns, []) == ([t["cluster"] for t in turns], 0)
    assert recluster.reassign([], []) == ([], 0)


def test_long_lone_turn_leave_one_out():
    """A 6 s turn builds its own cluster's centroid. Leave-one-out takes its
    weight out, so a lone outlier in A still moves to B."""
    turns, embs = _two_speakers(odd_dur=6.0)
    labels, _ = recluster.reassign(turns, embs)
    assert labels[-1] == "B"


def test_lone_long_turn_cluster_is_a_one_off_speaker_and_stays():
    """A cluster whose only centroid-building turn is this one has no other
    evidence to compare against: it stays, it does not drift into A."""
    turns = [{"start": 0.0, "end": 130.0, "cluster": "A"},
             {"start": 130.0, "end": 136.0, "cluster": "C"}]
    labels, moves = recluster.reassign(turns, [A, np.array([0.6, 0.8, 0.0])])
    assert labels == ["A", "C"] and moves == 0


def test_apply_keeps_times_and_inputs_and_drops_emptied_centroids():
    turns = [{"start": 0.0, "end": 130.0, "cluster": "A"},
             {"start": 130.0, "end": 260.0, "cluster": "B"},
             {"start": 260.0, "end": 261.0, "cluster": "C"}]
    embs = [A, B, B]
    cents = {"A": A, "B": B, "C": B, "D": A}
    before = [dict(t) for t in turns]
    out, new_cents, moves = recluster.apply(turns, embs, cents)
    assert turns == before
    assert [(t["start"], t["end"]) for t in out] == [(t["start"], t["end"]) for t in turns]
    assert [t["cluster"] for t in out] == ["A", "B", "B"]
    assert moves == 1
    # C was emptied by the move; D never had turns and is left as it was
    assert set(new_cents) == {"A", "B", "D"}


# --- diarize() wiring ---------------------------------------------------------

def _fake_diarize(monkeypatch, engine, recluster_on=True):
    turns, embs = _two_speakers()
    raw = {"labels": ["A", "B"], "overlaps": [], "centroids": {"A": A, "B": B},
           "raw_turns": [dict(t) for t in turns], "engine": engine}
    monkeypatch.setattr(config, "RECLUSTER", recluster_on)
    monkeypatch.setattr(diarize, "_get_pipeline", lambda: object())
    if engine.startswith("speakrs"):
        monkeypatch.setattr(config, "DIARIZER", "speakrs")
        monkeypatch.setattr(speakrs_diar, "available", lambda: (True, ""))
        monkeypatch.setattr(diarize, "_run_speakrs", lambda w, p: raw)
    else:
        monkeypatch.setattr(config, "DIARIZER", "pyannote")
        monkeypatch.setattr(diarize, "_pyannote_raw", lambda *a, **k: raw)
    monkeypatch.setattr(diarize, "_embed_turns", lambda *a, **k: (list(embs), 0))
    return diarize.diarize("x.wav", voiceprints={})


def test_diarize_reclusters_speakrs_turns(monkeypatch):
    out = _fake_diarize(monkeypatch, "speakrs-coreml")
    assert out["raw_turns"][-1]["cluster"] == "B"
    assert out["refine_stats"]["reclustered"] == 1
    assert out["turns"][-1]["speaker"] == "B"


def test_diarize_recluster_switch_off(monkeypatch):
    out = _fake_diarize(monkeypatch, "speakrs-coreml", recluster_on=False)
    assert out["raw_turns"][-1]["cluster"] == "A"
    assert out["refine_stats"]["reclustered"] == 0


def test_diarize_pyannote_fallback_is_not_reclustered(monkeypatch):
    out = _fake_diarize(monkeypatch, config.DIARIZATION_MODEL)
    assert out["raw_turns"][-1]["cluster"] == "A"
    assert out["refine_stats"]["reclustered"] == 0

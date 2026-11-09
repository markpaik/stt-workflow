"""Turn-level recluster of speakrs output (between speakrs and naming).

speakrs assigns each exclusive turn a cluster from window-level embeddings.
With the per-turn pyannote embeddings in hand (the final stage computes them
anyway), a turn-level correction is cheap:

1. Reassign: a turn whose own embedding fits another cluster's centroid
   better than its own (by more than RECLUSTER_MARGIN cosine, with the turn's
   own weight taken out of its own centroid) moves there. A turn never moves
   into a cluster with under RECLUSTER_DST_MIN_SECS of centroid-building talk,
   so one-off speakers (public comment) are not absorbed.
2. A cluster the moves empty loses its centroid, so naming skips it.

Runs after per-turn embedding and before naming (stt/diarize.py). Turn
times never change, so the same embeddings serve refine downstream. numpy
only; about a second on a 4-hour meeting.
"""
import numpy as np

from . import config


def _unit_rows(embeddings, dim):
    """(n, dim) unit rows plus an ok mask: None, non-finite or zero rows are
    not ok and never vote or move."""
    n = len(embeddings)
    X = np.zeros((n, dim))
    ok = np.zeros(n, bool)
    for i, e in enumerate(embeddings):
        if e is None:
            continue
        v = np.asarray(e, float).reshape(-1)
        if v.shape[0] != dim or not np.isfinite(v).all():
            continue
        nv = np.linalg.norm(v)
        if nv > 0:
            X[i] = v / nv
            ok[i] = True
    return X, ok


def _dim(embeddings):
    for e in embeddings:
        if e is not None:
            return int(np.asarray(e).reshape(-1).shape[0])
    return 0


def reassign(turns, embeddings, margin=None, centroid_min_dur=None,
             move_min_dur=None, dst_min_secs=None, iters=None):
    """Return (cluster label per turn, moves). Pure; inputs are not changed."""
    margin = config.RECLUSTER_MARGIN if margin is None else margin
    cd = config.RECLUSTER_CENTROID_MIN_DUR if centroid_min_dur is None else centroid_min_dur
    rd = config.RECLUSTER_MOVE_MIN_DUR if move_min_dur is None else move_min_dur
    dst_min = config.RECLUSTER_DST_MIN_SECS if dst_min_secs is None else dst_min_secs
    iters = config.RECLUSTER_ITERS if iters is None else iters
    labels_in = [t["cluster"] for t in turns]
    dim = _dim(embeddings)
    if not turns or dim == 0 or len(embeddings) != len(turns) or iters < 1:
        return labels_in, 0
    names = sorted(set(labels_in))
    k = len(names)
    lab = np.array([names.index(c) for c in labels_in])
    X, ok = _unit_rows(embeddings, dim)
    dur = np.array([t["end"] - t["start"] for t in turns], float)
    cm = ok & (dur >= cd)   # turns that build centroids
    rm = ok & (dur >= rd)   # turns allowed to move
    moves = 0
    for _ in range(iters):
        w = np.where(cm, dur, 0.0)
        S = np.zeros((k, dim))
        np.add.at(S, lab, X * w[:, None])
        M = S / np.maximum(np.linalg.norm(S, axis=1, keepdims=True), 1e-9)
        with np.errstate(all="ignore"):   # Accelerate can raise spurious FP flags
            sim = X @ M.T
        sim = np.nan_to_num(sim, nan=-1.0)
        mass = np.bincount(lab[cm], weights=dur[cm], minlength=k)
        new = lab.copy()
        moved = 0
        for i in np.flatnonzero(rm):
            own = lab[i]
            if cm[i]:   # leave-one-out: the turn must not vote for its own cluster
                s = S[own] - X[i] * dur[i]
                ns = np.linalg.norm(s)
                if ns <= 1e-9:
                    continue   # the cluster's only long turn: a one-off speaker stays
                so = float(X[i] @ (s / ns))
            else:
                so = float(sim[i, own])
            o = sim[i].copy()
            o[own] = -9.0
            b = int(o.argmax())
            if o[b] - so > margin and o[b] >= 0.0 and mass[b] >= dst_min:
                new[i] = b
                moved += 1
        lab = new
        moves += moved
        if moved == 0:
            break
    return [names[j] for j in lab], moves


def apply(raw_turns, embeddings, cent_emb):
    """Recluster the turns. Returns (new turns, centroids, moves).
    New turn dicts carry the same times; only "cluster" can change. The
    speakrs centroids stay as they are (rebuilding them from turn embeddings
    added nothing once the junk-repair guard was in), except that a cluster
    the moves emptied loses its centroid, so naming cannot hand a name to a
    cluster with no turns."""
    labels, moves = reassign(raw_turns, embeddings)
    turns = [dict(t, cluster=c) for t, c in zip(raw_turns, labels)]
    emptied = {t["cluster"] for t in raw_turns} - set(labels)
    cents = {c: v for c, v in cent_emb.items() if c not in emptied}
    return turns, cents, moves

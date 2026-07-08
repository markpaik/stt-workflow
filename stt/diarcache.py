"""Persist the raw diarization (turns + per-turn embeddings + centroids) so that
`relabel` can re-run identity attribution after new enrollments WITHOUT
re-transcribing or re-diarizing the audio.
"""
import os

import numpy as np


def save(path, raw_turns, turn_embeddings, cent_emb, overlaps=None, dim=256):
    d = dim
    for e in turn_embeddings:
        if e is not None:
            d = len(e)
            break
    tembs = np.full((len(raw_turns), d), np.nan, dtype=float)
    for i, e in enumerate(turn_embeddings):
        if e is not None and len(e) == d:
            tembs[i] = e
    # atomic: write to a temp sibling then os.replace, so a crash mid-write can't
    # leave a truncated archive. Pass a file handle (not the path) so np.savez does
    # not append its own .npz to the .tmp name.
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as fh:
        np.savez(
            fh,
            starts=np.array([t["start"] for t in raw_turns], dtype=float),
            ends=np.array([t["end"] for t in raw_turns], dtype=float),
            clusters=np.array([t["cluster"] for t in raw_turns], dtype=object),
            tembs=tembs,
            cent_labels=np.array(list(cent_emb.keys()), dtype=object),
            cent_vecs=(np.array([cent_emb[k] for k in cent_emb], dtype=float)
                       if cent_emb else np.zeros((0, d))),
            overlaps=np.array(overlaps or [], dtype=float).reshape(-1, 2),
        )
    os.replace(tmp, path)


def load(path):
    d = np.load(path, allow_pickle=True)
    raw_turns = [{"start": float(s), "end": float(e), "cluster": str(c)}
                 for s, e, c in zip(d["starts"], d["ends"], d["clusters"])]
    turn_embeddings = [None if not np.isfinite(row).all() else np.asarray(row, float)
                       for row in d["tembs"]]
    cent_emb = {str(l): np.asarray(v, float)
                for l, v in zip(d["cent_labels"], d["cent_vecs"])}
    overlaps = ([(float(s), float(e)) for s, e in d["overlaps"]]
                if "overlaps" in d.files else [])  # older caches predate overlap data
    return raw_turns, turn_embeddings, cent_emb, overlaps

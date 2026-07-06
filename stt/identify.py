"""Name speakers by matching diarization embeddings to an enrolled voiceprint library.

Open-set by design: matching answers "is this one of the enrolled people?" — not
just "which enrolled person is closest?". A name is assigned only when the best
cosine clears an absolute threshold AND a margin over the runner-up; anyone else
stays an anonymous Speaker N (interviews with strangers are an explicit use case).

Voiceprints store MULTIPLE embeddings per person (up to MAX_SAMPLES, ideally from
different rooms/meetings) and score by max cosine — single-meeting centroids are
brittle across rooms, mics, and days. Embeddings are L2-normalized before storage
so no single loud sample dominates.
"""
import json
from datetime import datetime

import numpy as np

from . import config

MAX_SAMPLES = 5


def _registry_path():
    return config.VOICEPRINTS_DIR / "registry.json"


def load_registry() -> dict:
    p = _registry_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_registry(reg: dict):
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(json.dumps(reg, indent=2))


def _l2(v):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n == 0 or not np.isfinite(n):
        raise ValueError("embedding has zero/non-finite norm")
    return v / n


def load_voiceprints() -> dict:
    """Return {name: np.ndarray of shape (n_samples, dim)} for all enrolled people."""
    out = {}
    for name, meta in load_registry().items():
        f = config.VOICEPRINTS_DIR / meta["file"]
        if f.exists():
            arr = np.load(f)
            if arr.ndim == 1:  # legacy single-centroid format
                arr = arr.reshape(1, -1)
            out[name] = arr
    return out


def cosine(a, b) -> float:
    """NaN-safe cosine: returns -1.0 (never NaN) on invalid input, so a bad
    embedding can never accidentally clear a `>= threshold` comparison."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        return -1.0
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def score_against(vec, samples) -> float:
    """Best cosine of vec against a person's sample set (max over samples)."""
    return max((cosine(vec, s) for s in samples), default=-1.0)


def _log_calibration(event: dict):
    try:
        event["at"] = datetime.now().isoformat(timespec="seconds")
        with open(config.CALIBRATION_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass  # calibration logging must never break the pipeline


def name_speakers(embeddings: dict, threshold: float = None, margin: float = None,
                  allowed_names=None, context: str = "") -> dict:
    """embeddings: {label: vector}. Returns {label: {"name": str|None, "score": float}}.

    Open-set: requires best >= threshold AND (best - runner_up) >= margin.
    Greedy uniqueness: each name used at most once (see refine for the split-cluster
    exception). `allowed_names` optionally restricts eligible voiceprints.
    """
    if threshold is None:
        threshold = config.NAMING_THRESHOLD
    if margin is None:
        margin = config.NAMING_MARGIN
    prints = load_voiceprints()
    if allowed_names is not None:
        prints = {n: s for n, s in prints.items() if n in allowed_names}

    per_label = {}
    for label, vec in embeddings.items():
        scores = sorted(((score_against(vec, s), n) for n, s in prints.items()),
                        reverse=True)
        per_label[label] = scores

    # greedy assignment over all (label, name) pairs by score
    flat = sorted(((sc, label, name) for label, scores in per_label.items()
                   for sc, name in scores), reverse=True)
    assigned, used_names = {}, set()
    for score, label, name in flat:
        if label in assigned or name in used_names:
            continue
        others = [sc for sc, n in per_label[label] if n != name]
        second = max(others) if others else -1.0
        ok = score >= threshold and (score - second) >= margin
        _log_calibration({"kind": "cluster", "context": context, "label": label,
                          "name": name, "score": round(score, 3),
                          "second": round(second, 3), "accepted": bool(ok)})
        if ok:
            assigned[label] = {"name": name, "score": round(score, 3)}
            used_names.add(name)

    return {label: assigned.get(label, {"name": None, "score": 0.0})
            for label in embeddings}


def rename_person(old: str, new: str) -> bool:
    """Rename an enrolled person. If the new name already exists, this becomes a
    merge (their voice samples are combined)."""
    new = new.strip()
    reg = load_registry()
    if old not in reg or not new:
        return False
    if new in reg:
        return merge_people(old, new)
    meta = reg.pop(old)
    newf = f"{new.replace('/', '_')}.npy"
    (config.VOICEPRINTS_DIR / meta["file"]).rename(config.VOICEPRINTS_DIR / newf)
    meta["file"] = newf
    reg[new] = meta
    save_registry(reg)
    return True


def merge_people(src: str, dst: str) -> bool:
    """Combine two enrolled entries that are really the same person: src's voice
    samples are folded into dst (rolling window), src is removed."""
    reg = load_registry()
    if src not in reg or dst not in reg or src == dst:
        return False
    s = np.load(config.VOICEPRINTS_DIR / reg[src]["file"])
    d = np.load(config.VOICEPRINTS_DIR / reg[dst]["file"])
    s = s.reshape(1, -1) if s.ndim == 1 else s
    d = d.reshape(1, -1) if d.ndim == 1 else d
    src_sources = reg[src].get("sources", ["?"] * s.shape[0])
    dst_sources = reg[dst].get("sources", ["?"] * d.shape[0])
    arr = np.vstack([d, s])[-MAX_SAMPLES:]
    sources = (dst_sources + src_sources)[-MAX_SAMPLES:]
    np.save(config.VOICEPRINTS_DIR / reg[dst]["file"], arr)
    (config.VOICEPRINTS_DIR / reg[src]["file"]).unlink(missing_ok=True)
    del reg[src]
    reg[dst]["n_samples"] = int(arr.shape[0])
    reg[dst]["sources"] = sources
    save_registry(reg)
    return True


def remove_sample(name: str, index: int) -> bool:
    """Drop one voice sample (e.g. it came from a bad recording). Refuses to
    remove the last sample — remove the person instead."""
    reg = load_registry()
    if name not in reg:
        return False
    f = config.VOICEPRINTS_DIR / reg[name]["file"]
    if not f.exists():
        return False
    arr = np.load(f)
    arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
    if not (0 <= index < arr.shape[0]) or arr.shape[0] <= 1:
        return False
    sources = reg[name].get("sources", ["?"] * arr.shape[0])
    keep = [i for i in range(arr.shape[0]) if i != index]
    np.save(f, arr[keep])
    reg[name]["n_samples"] = len(keep)
    reg[name]["sources"] = [s for i, s in enumerate(sources) if i != index]
    save_registry(reg)
    return True


def remove_person(name: str) -> bool:
    """Un-enroll someone (their turns revert to unknown numbering on relabel)."""
    reg = load_registry()
    if name not in reg:
        return False
    (config.VOICEPRINTS_DIR / reg[name]["file"]).unlink(missing_ok=True)
    del reg[name]
    save_registry(reg)
    return True


def enroll(name: str, vector, replace: bool = False, source: str = None):
    """Add a voiceprint sample. Keeps up to MAX_SAMPLES most-recent L2-normalized
    samples per person (scored by max cosine at match time). `source` records
    which recording the sample came from — kept as a rolling list aligned with
    the samples, so the GUI can show provenance and locate playable audio."""
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    reg = load_registry()
    fname = f"{name.replace('/', '_')}.npy"
    fpath = config.VOICEPRINTS_DIR / fname
    vector = _l2(vector)  # raises on zero/non-finite

    if name in reg and fpath.exists() and not replace:
        arr = np.load(fpath)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if any(cosine(vector, row) > 0.999 for row in arr):
            return fpath  # same sample again (e.g. re-promote after a relabel) — skip
        prev_sources = reg[name].get("sources", ["?"] * arr.shape[0])
        arr = np.vstack([arr, vector])
        sources = (prev_sources + [source or "?"])[-MAX_SAMPLES:]
        arr = arr[-MAX_SAMPLES:]
    else:
        arr = vector.reshape(1, -1)
        sources = [source or "?"]

    reg[name] = {"file": fname, "dim": int(vector.shape[0]),
                 "n_samples": int(arr.shape[0]), "sources": sources}
    np.save(fpath, arr)
    save_registry(reg)
    return fpath

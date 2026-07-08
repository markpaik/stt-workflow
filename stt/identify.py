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
import contextlib
import fcntl
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime

import numpy as np

from . import config

MAX_SAMPLES = 5

_lock_depth = threading.local()


@contextlib.contextmanager
def lock_registry():
    """Serialize ALL voiceprint registry mutation — identify.py's
    registry.json AND unknowns.py's unknowns.json (promote() touches both) —
    across the batch and relabel processes, which are designed to run
    concurrently. Without this, two processes can race a read-modify-write of
    either file and silently clobber a fresh enrollment or misnumber an
    unknown speaker.

    Re-entrant WITHIN one thread (promote() calls enroll() while already
    holding the lock): flock itself isn't re-entrant, so a naive nested
    acquire from the same process would deadlock against itself. A depth
    counter makes only the outermost call take the real OS lock; a
    concurrent call from another process/thread still genuinely waits for it."""
    depth = getattr(_lock_depth, "n", 0)
    if depth > 0:
        _lock_depth.n = depth + 1
        try:
            yield
        finally:
            _lock_depth.n = depth
        return
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.VOICEPRINTS_DIR / ".registry.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _lock_depth.n = 1
        try:
            yield
        finally:
            _lock_depth.n = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _registry_path():
    return config.VOICEPRINTS_DIR / "registry.json"


def load_registry() -> dict:
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        # a corrupt registry must be LOUD, not a silent reset to empty — every
        # enrolled voiceprint would otherwise look freshly un-enrolled
        print(f"WARNING: {p} is corrupt ({e}) — treating as empty. Voiceprint "
              "files on disk are untouched; fix or restore the registry before "
              "re-enrolling anyone.", file=sys.stderr)
        return {}


def save_registry(reg: dict):
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    p = _registry_path()
    # rolling backup: before overwriting a registry that HAS people, keep the
    # previous version as .bak — one accidental wipe of this file is the
    # difference between "restore in seconds" and rebuilding every voiceprint
    # from meeting caches (biometric data with no other copy on this machine)
    if p.exists():
        try:
            has_people = bool(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            has_people = None  # unparseable => precious; never clobber a good .bak
        if has_people is None:
            # corrupt content is precious too: route it to a DISTINCT timestamped
            # sidecar so a previously-written GOOD .bak stays restorable
            with contextlib.suppress(OSError):
                shutil.copy2(p, p.with_suffix(f".json.corrupt-{int(time.time())}"))
        elif has_people:
            # copy (not rename) so registry.json is never absent from its path —
            # an unlocked concurrent reader must never see 'nobody enrolled'
            with contextlib.suppress(OSError):
                shutil.copy2(p, p.with_suffix(".json.bak"))
    _atomic_write(p, json.dumps(reg, indent=2))


def _unique_filename(stem: str, reg: dict) -> str:
    """A `name.replace('/', '_')` sanitized stem can collide with an unrelated
    person's file — "A/B" and "A_B" both sanitize to "A_B.npy". Disambiguate
    against every file already claimed in the registry (the source of truth
    for what's in use — not just an on-disk exists() check, since callers
    already remove/never-added the current person's own entry before calling
    this, so a legitimate re-enrollment is never blocked by itself)."""
    claimed = {meta["file"] for meta in reg.values()}
    fname = f"{stem}.npy"
    n = 2
    while fname in claimed:
        fname = f"{stem}_{n}.npy"
        n += 1
    return fname


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
    embedding can never accidentally clear a `>= threshold` comparison. A
    dimension mismatch is exactly as invalid — without this check, one
    voiceprint saved under a different embedding size (e.g. after a model
    change) would raise inside np.dot for EVERY comparison against it, which
    since this is called in an all-vs-all loop, poisons matching for every
    speaker in every meeting, not just that one entry's owner."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.shape != b.shape:
        return -1.0
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
    with lock_registry():
        new = new.strip()
        reg = load_registry()
        if old not in reg or not new:
            return False
        if new in reg:
            return merge_people(old, new)
        meta = reg.pop(old)
        oldf = config.VOICEPRINTS_DIR / meta["file"]
        if not oldf.exists():
            return False  # orphaned entry: file gone out-of-band, nothing to rename
        newf = _unique_filename(new.replace("/", "_"), reg)
        # copy -> commit registry -> delete original, so a crash always leaves
        # either the old or the new mapping fully valid. A rename-before-save
        # could orphan the moved file with the registry still pointing at the
        # vanished original (person un-enrolled, samples unreferenced on disk).
        shutil.copy2(oldf, config.VOICEPRINTS_DIR / newf)
        meta["file"] = newf
        reg[new] = meta
        save_registry(reg)
        oldf.unlink(missing_ok=True)
        return True


def _merge_budget(n_d, n_s, cap):
    """How many samples to keep from dst and src when merging two people, so the
    combined profile carries BOTH voices. Splits the cap between them (src takes
    the floor half, dst the rest) and lets a side with fewer samples yield its
    slack to the other. A plain tail of [dst, src] dropped ALL of dst whenever
    dst already held cap samples — the merged profile then represented only the
    person merged in last, exactly the voice the user was NOT looking at."""
    if n_d + n_s <= cap:
        return n_d, n_s
    keep_s = min(n_s, cap // 2)
    keep_d = min(n_d, cap - keep_s)
    keep_s = min(n_s, cap - keep_d)  # reclaim slack dst couldn't use
    return keep_d, keep_s


def merge_people(src: str, dst: str) -> bool:
    """Combine two enrolled entries that are really the same person: src's voice
    samples are folded into dst (keeping a spread of BOTH within MAX_SAMPLES),
    src is removed."""
    with lock_registry():
        reg = load_registry()
        if src not in reg or dst not in reg or src == dst:
            return False
        sf = config.VOICEPRINTS_DIR / reg[src]["file"]
        df = config.VOICEPRINTS_DIR / reg[dst]["file"]
        if not (sf.exists() and df.exists()):
            return False  # orphaned entry: fail cleanly like remove_sample, not a crash
        s = np.load(sf)
        d = np.load(df)
        s = s.reshape(1, -1) if s.ndim == 1 else s
        d = d.reshape(1, -1) if d.ndim == 1 else d
        src_sources = reg[src].get("sources", ["?"] * s.shape[0])
        dst_sources = reg[dst].get("sources", ["?"] * d.shape[0])
        keep_d, keep_s = _merge_budget(d.shape[0], s.shape[0], MAX_SAMPLES)
        # most-recent within each side; explicit positive indices so a 0 keep
        # slices to empty, never to the whole list the way [-0:] would
        arr = np.vstack([d[d.shape[0] - keep_d:], s[s.shape[0] - keep_s:]])
        sources = (dst_sources[len(dst_sources) - keep_d:]
                   + src_sources[len(src_sources) - keep_s:])
        np.save(df, arr)
        sf.unlink(missing_ok=True)
        del reg[src]
        reg[dst]["n_samples"] = int(arr.shape[0])
        reg[dst]["sources"] = sources
        save_registry(reg)
        return True


def remove_sample(name: str, index: int) -> bool:
    """Drop one voice sample (e.g. it came from a bad recording). Refuses to
    remove the last sample — remove the person instead."""
    with lock_registry():
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


def reassign_sample(name: str, index: int, to_name: str) -> dict:
    """Move one voice sample from `name`'s profile to `to_name` (an existing
    person, or a brand-new one). The honest fix when a sample was enrolled under
    the WRONG identity: the embedding is carried across, not thrown away, and its
    source meeting travels with it. Unlike remove_sample this may empty and drop
    the source profile — reassigning its last sample means the whole thing was
    the wrong person all along."""
    to_name = (to_name or "").strip()
    if not to_name:
        return {"ok": False, "error": "a destination name is required"}
    with lock_registry():
        reg = load_registry()
        if name not in reg:
            return {"ok": False, "error": f"no profile for {name}"}
        if to_name == name:
            return {"ok": False, "error": "already this person"}
        f = config.VOICEPRINTS_DIR / reg[name]["file"]
        if not f.exists():
            return {"ok": False, "error": "voiceprint file missing"}
        arr = np.load(f)
        arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
        if not (0 <= index < arr.shape[0]):
            return {"ok": False, "error": "no such sample"}
        vec = arr[index]
        sources = reg[name].get("sources", ["?"] * arr.shape[0])
        src = sources[index] if index < len(sources) else None
        keep = [i for i in range(arr.shape[0]) if i != index]
        if keep:
            np.save(f, arr[keep])
            reg[name]["n_samples"] = len(keep)
            reg[name]["sources"] = [s for i, s in enumerate(sources) if i != index]
        else:  # that was the source's only sample — the profile was wrong wholesale
            f.unlink(missing_ok=True)
            del reg[name]
        save_registry(reg)  # commit the removal BEFORE enroll re-reads the registry
        # enroll re-locks (reentrant) and re-normalizes; vec is already unit-norm
        enroll(to_name, vec, source=src)
        return {"ok": True, "to": to_name, "source_emptied": name not in reg}


def remove_person(name: str) -> bool:
    """Un-enroll someone (their turns revert to unknown numbering on relabel)."""
    with lock_registry():
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
    with lock_registry():
        reg = load_registry()
        # reuse this person's OWN file if already enrolled — their filename may
        # have been disambiguated at first enrollment, so recomputing it fresh
        # here could drift from what the registry actually points at. A
        # brand-new name gets one that can't collide with any OTHER enrolled
        # person's file.
        fname = reg[name]["file"] if name in reg else _unique_filename(
            name.replace("/", "_"), reg)
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

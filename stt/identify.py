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
import re
import shutil
import sys
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config, names
from .errors import UserError

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
    _private_dir()
    with open(config.VOICEPRINTS_DIR / ".registry.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _lock_depth.n = 1
        try:
            yield
        finally:
            _lock_depth.n = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def _private_dir():
    """The voiceprints folder, created owner-only. A folder an older install
    left 0755 is tightened too: voiceprints are biometric data."""
    d = config.VOICEPRINTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _save_npy(path, arr):
    """A sample stack, written whole or not at all: through a temporary
    file and os.replace, so a crash or a full disk mid-write leaves the old
    stack, never a truncated file that every later read raises on."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            np.save(fh, arr)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _atomic_write(path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _registry_path():
    return config.VOICEPRINTS_DIR / "registry.json"


REGISTRY_UNREADABLE = (
    "The voiceprint registry (registry.json) cannot be read, so nothing was "
    "saved. Restore registry.json.bak, or rebuild it with "
    "tools/rebuild_voiceprints.py --apply.")


class RegistryUnreadable(UserError):
    """registry.json is there and cannot be read. Nothing writes over it:
    an enrollment saved over a damaged registry left one person in it, and
    the next save copied that one-person registry over the good .bak."""


def _read_registry(p):
    """The registry at `p`: {} when it is not there, None when it is there
    and is not a readable JSON object."""
    if not p.exists():
        return {}
    try:
        reg = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return reg if isinstance(reg, dict) else None


def load_registry() -> dict:
    p = _registry_path()
    reg = _read_registry(p)
    if reg is None:
        # a corrupt registry must be LOUD, not a silent reset to empty — every
        # enrolled voiceprint would otherwise look freshly un-enrolled
        print(f"WARNING: {p} is corrupt — treating as empty. Voiceprint "
              "files on disk are untouched, and nothing writes the registry "
              "until it is restored or rebuilt.", file=sys.stderr)
        return {}
    return reg


def load_registry_for_write() -> dict:
    """The registry for a read-modify-write. Raises RegistryUnreadable when
    the file is damaged, before anything is written."""
    reg = _read_registry(_registry_path())
    if reg is None:
        raise RegistryUnreadable(REGISTRY_UNREADABLE)
    return reg


def save_registry(reg: dict):
    _private_dir()
    p = _registry_path()
    # rolling backup: before overwriting a registry that HAS people, keep the
    # previous version as .bak — one accidental wipe of this file is the
    # difference between "restore in seconds" and rebuilding every voiceprint
    # from meeting caches (biometric data with no other copy on this machine)
    on_disk = _read_registry(p)
    if on_disk is None:
        # damaged content is precious: it is never written over, and a GOOD
        # .bak from an earlier save stays restorable
        raise RegistryUnreadable(REGISTRY_UNREADABLE)
    if on_disk:
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
    # folded (_fold): the default macOS disk ignores case and Unicode
    # normalization, so "alex rivera.npy" IS "Alex Rivera.npy" there, and an
    # 'e' plus a combining accent IS the precomposed 'é'
    claimed = {_fold(meta["file"]) for meta in reg.values()}
    # unknown voices keep their U###.npy stacks in the same folder, and a
    # file nobody claims may still be someone's only copy: never reuse either
    with contextlib.suppress(OSError, ValueError, AttributeError):
        unk = json.loads((config.VOICEPRINTS_DIR / "unknowns.json").read_text())
        claimed |= {_fold(str(m.get("file", "")))
                    for m in (unk.get("speakers") or {}).values()}
    with contextlib.suppress(OSError):
        claimed |= {_fold(p.name) for p in config.VOICEPRINTS_DIR.glob("*.npy")}
    fname = f"{stem}.npy"
    n = 2
    while _fold(fname) in claimed:
        fname = f"{stem}_{n}.npy"
        n += 1
    return fname


def _fold(s: str) -> str:
    """`s` as the default macOS disk compares a file name: case and Unicode
    normalization both ignored. 'Jos' + U+00E9 (typed) and 'Jose' + U+0301
    (pasted from Finder) are one file there, so they are one key here.
    NFD on both sides of the casefold is the Unicode canonical caseless
    match."""
    return unicodedata.normalize(
        "NFD", unicodedata.normalize("NFD", str(s)).casefold())


def _name_key(name: str) -> str:
    return _fold(" ".join(str(name).split()))


_RESERVED = re.compile(r"U\d+|(?:speaker|voice)\s+\d+", re.IGNORECASE)


def reserved_name(name) -> bool:
    """'U007' is an unknown voice's id, and its samples live in U007.npy in
    this same folder. A person by that name would share the file.

    'Speaker 7' and 'Voice 3' are the labels an unknown voice shows
    (unknowns.display) and a meeting-only cluster shows. A person enrolled
    as 'Speaker 7' looked like unknown U007 everywhere, and speaker_text
    reads such a label as no name at all."""
    return bool(_RESERVED.fullmatch(" ".join(str(name or "").split())))


RESERVED_ERROR = ("Names like U7, Speaker 7 or Voice 3 are reserved for unknown "
                  "voices. Pick a different name.")


def canonical_name(name: str, reg: dict = None) -> str:
    """The enrolled spelling of `name` when one differs only in case,
    spacing or Unicode normalization, else `name` stripped. "alex rivera"
    typed for an enrolled "Alex Rivera" is the same person: a second entry
    would point at the same file on a case-insensitive disk and overwrite
    her samples. A precomposed 'é' and an 'e' with a combining accent are
    the same file on that disk too."""
    name = str(name or "").strip()
    if reg is None:
        reg = load_registry()
    if name in reg:
        return name
    key = _name_key(name)
    for existing in reg:
        if _name_key(existing) == key:
            return existing
    return name


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


def loo_scores(samples) -> list:
    """Leave-one-out: each sample's best cosine against the REST of its own
    stack. A wrong-voice enrollment sits far below its stackmates here — the
    honest way to locate a bad sample without guessing at indexes. A single
    sample scores -1.0 (nothing to compare against)."""
    arr = np.asarray(samples, dtype=float)
    arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
    out = []
    for i in range(arr.shape[0]):
        rest = [arr[j] for j in range(arr.shape[0]) if j != i]
        out.append(round(score_against(arr[i], rest), 3))
    return out


def sample_check(name, vector, exclude=(), prints=None):
    """Should this new sample join `name`'s EXISTING stack? None = clean (or
    the person is new, so there is no stack to disagree with). Otherwise
    {"own", "cross", "cross_name"}: the candidate scores below
    ENROLL_STACK_MIN against the person's own samples, or matches some OTHER
    profile better than its target — the classic wrong-cluster enrollment
    that poisons a profile and misattributes every later meeting.

    `exclude` leaves profiles out of the cross check: a merge of one person
    into another checks the source's own rows, which always match the
    source best. `prints` (load_voiceprints()) lets a caller that checks
    many rows read the profiles from disk once."""
    prints = load_voiceprints() if prints is None else prints
    if name not in prints:
        # enroll() files 'jordan lee' under an enrolled 'Jordan Lee'
        # (canonical_name), so the sample is checked against that stack too
        key = _name_key(name)
        name = next((nm for nm in prints if _name_key(nm) == key), name)
    own = prints.get(name)
    if own is None:
        return None
    v = _l2(np.asarray(vector, dtype=float))
    own_sc = score_against(v, own)
    cross_name, cross_sc = None, -1.0
    for nm, s in prints.items():
        if nm == name or nm in exclude:
            continue
        sc = score_against(v, s)
        if sc > cross_sc:
            cross_sc, cross_name = sc, nm
    if own_sc < config.ENROLL_STACK_MIN or (cross_name is not None and cross_sc > own_sc):
        return {"own": round(own_sc, 3), "cross": round(cross_sc, 3),
                "cross_name": cross_name}
    return None


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
        reg = load_registry()
        if old not in reg:
            return False
        if (not str(new or "").strip() or names.has_unsafe(new)
                or len(str(new).strip()) > names.NAME_MAX):
            return False
        new = canonical_name(new, {k: v for k, v in reg.items() if k != old})
        if not new or new == old or reserved_name(new):
            return False
        if new in reg:
            return merge_people(old, new)
        if _name_key(new) == _name_key(old):
            # a spelling fix that changes only case or spacing: the file stays.
            # Copy-then-unlink onto a name that is the SAME file on a
            # case-insensitive disk would delete the only copy.
            reg[new] = reg.pop(old)
            save_registry(reg)
            return True
        meta = reg.pop(old)
        oldf = config.VOICEPRINTS_DIR / meta["file"]
        if not oldf.exists():
            return False  # orphaned entry: file gone out-of-band, nothing to rename
        newf = _unique_filename(names.file_stem(new), reg)
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


def aligned_source_ids(meta: dict) -> list:
    """`meta["source_ids"]` padded (or truncated) to exactly len(sources).

    An entry enrolled before meeting ids existed has no `source_ids` at all,
    and one written by an older build can be short. Every reader and writer
    goes through here, so the two lists are index-aligned by construction and a
    legacy entry simply reads as "no id for any sample" — base-only, exactly
    the behaviour it had before."""
    srcs = meta.get("sources", []) or []
    ids = meta.get("source_ids") or []
    ids = list(ids)[:len(srcs)]
    return ids + [None] * (len(srcs) - len(ids))


def resolved_sources(meta: dict) -> list:
    """This profile's sample sources as LIVE basenames where that is knowable.

    The stored base is the answer while it exists. When it does not — the
    meeting was renamed and a chase failed, or a rename happened while this
    process was not looking — the id resolves through the index to wherever the
    meeting actually is. That is the whole point of carrying the id: a ▶ button
    that used to go dead on a rename now survives one."""
    out = []
    for base, mid in zip(meta.get("sources", []) or [], aligned_source_ids(meta)):
        if mid and base and base != "?" and not config.meeting_file(base, ".json").exists():
            base = config.base_of(mid) or base
        out.append(base)
    return out


def rename_source_refs(old_base: str, new_base: str) -> int:
    """A meeting rename must follow into enrolled profiles too: each sample's
    `sources` entry names the meeting it came from, and the speaker dialog's
    per-sample ▶ plays from exactly that meeting. Returns people touched.

    This chase stays (it keeps the stored name honest and readable), but it is
    no longer the only thing standing between a rename and a dead play button:
    the aligned `source_ids` survive it failing, and resolved_sources reads
    through them."""
    n = 0
    new_id = config.id_of(new_base)
    with lock_registry():
        reg = load_registry()
        for meta in reg.values():
            srcs = meta.get("sources", [])
            if old_base in srcs:
                ids = aligned_source_ids(meta)
                # heal while we are here: a sample whose source we are renaming
                # and which carries no id yet gets the id of where it landed
                meta["source_ids"] = [new_id if (s == old_base and i is None) else i
                                      for s, i in zip(srcs, ids)]
                meta["sources"] = [new_base if s == old_base else s for s in srcs]
                n += 1
        if n:
            save_registry(reg)
    return n


def forget_source_refs(base: str, meeting_id=None) -> int:
    """A deleted meeting's name off the samples it fed: each `sources`
    entry that names it becomes '?' and its id None, as the doctor's fix
    writes for a dead reference. The sample keeps its row: its embedding
    still identifies the person. Without this, a normal Delete left the
    next doctor check listing one `registry_dangling` finding per person
    the meeting fed.

    An entry another meeting still answers to (a live or archived twin of
    the name, or an id that resolves) is left alone, the doctor's own
    liveness rule. A damaged registry is never written over. Returns the
    people touched."""
    alive = set(config.meeting_bases()) | set(config.archived_bases())
    ids = config.load_meetings_index()["ids"]

    def dead(s, i):
        if s in alive:
            return False
        return not (i and ids.get(i) in alive)

    n = 0
    with lock_registry():
        reg = load_registry_for_write()
        for meta in reg.values():
            if not isinstance(meta, dict) or not isinstance(meta.get("sources"), list):
                continue
            srcs = meta["sources"]
            sids = aligned_source_ids(meta)
            hit = {k for k, (s, i) in enumerate(zip(srcs, sids))
                   if (s == base or (meeting_id and i == meeting_id)) and dead(s, i)}
            if hit:
                meta["sources"] = ["?" if k in hit else s for k, s in enumerate(srcs)]
                meta["source_ids"] = [None if k in hit else i for k, i in enumerate(sids)]
                n += 1
        if n:
            save_registry(reg)
    return n


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
        src_ids = aligned_source_ids(reg[src]) or [None] * s.shape[0]
        dst_ids = aligned_source_ids(reg[dst]) or [None] * d.shape[0]
        keep_d, keep_s = _merge_budget(d.shape[0], s.shape[0], MAX_SAMPLES)
        # most-recent within each side; explicit positive indices so a 0 keep
        # slices to empty, never to the whole list the way [-0:] would
        arr = np.vstack([d[d.shape[0] - keep_d:], s[s.shape[0] - keep_s:]])
        sources = (dst_sources[len(dst_sources) - keep_d:]
                   + src_sources[len(src_sources) - keep_s:])
        source_ids = (dst_ids[len(dst_ids) - keep_d:]
                      + src_ids[len(src_ids) - keep_s:])
        _save_npy(df, arr)
        sf.unlink(missing_ok=True)
        del reg[src]
        reg[dst]["n_samples"] = int(arr.shape[0])
        reg[dst]["sources"] = sources
        reg[dst]["source_ids"] = source_ids   # same slice, same order, aligned
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
        ids = aligned_source_ids(reg[name]) or [None] * arr.shape[0]
        keep = [i for i in range(arr.shape[0]) if i != index]
        _save_npy(f, arr[keep])
        reg[name]["n_samples"] = len(keep)
        reg[name]["sources"] = [s for i, s in enumerate(sources) if i != index]
        reg[name]["source_ids"] = [m for i, m in enumerate(ids) if i != index]
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
    if names.has_unsafe(to_name):
        return {"ok": False, "error": names.UNSAFE_NAME}
    if reserved_name(to_name):
        return {"ok": False, "error": RESERVED_ERROR}
    with lock_registry():
        reg = load_registry()
        to_name = canonical_name(to_name, reg)
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
        ids = aligned_source_ids(reg[name]) or [None] * arr.shape[0]
        src = sources[index] if index < len(sources) else None
        src_id = ids[index] if index < len(ids) else None
        keep = [i for i in range(arr.shape[0]) if i != index]
        if keep:
            _save_npy(f, arr[keep])
            reg[name]["n_samples"] = len(keep)
            reg[name]["sources"] = [s for i, s in enumerate(sources) if i != index]
            reg[name]["source_ids"] = [m for i, m in enumerate(ids) if i != index]
        else:  # that was the source's only sample — the profile was wrong wholesale
            f.unlink(missing_ok=True)
            del reg[name]
        save_registry(reg)  # commit the removal BEFORE enroll re-reads the registry
        # enroll re-locks (reentrant) and re-normalizes; vec is already unit-norm.
        # The id travels WITH the sample: the source meeting may have been
        # renamed since, in which case the stored base no longer resolves and
        # re-deriving the id from it would silently drop the provenance.
        enroll(to_name, vec, source=src, source_id=src_id)
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


def _cap_keeping_newest_and_diverse(arr, sources, source_ids, cap):
    """Enforce the per-person sample cap by dropping the MOST REDUNDANT older
    sample (the one whose cosine to any other retained sample is highest), never
    the freshest one (arr[-1]). A profile then keeps a spread across rooms, mics,
    and days instead of narrowing to whichever samples arrived most recently:
    matching scores by max cosine, so five near-duplicates from one recording
    help far less than five samples that each cover a different condition."""
    while arr.shape[0] > cap:
        n = arr.shape[0]
        drop = max(range(n - 1),  # older samples only — keep the newest
                   key=lambda i: max(cosine(arr[i], arr[j])
                                     for j in range(n) if j != i))
        keep = [k for k in range(n) if k != drop]
        arr = arr[keep]
        sources = [sources[k] for k in keep]
        source_ids = [source_ids[k] for k in keep]   # aligned through the drop
    return arr, sources, source_ids


def enroll(name: str, vector, replace: bool = False, source: str = None,
           source_id: str = None):
    """Add a voiceprint sample. Keeps up to MAX_SAMPLES L2-normalized samples per
    person, evicting the most redundant one so the set stays diverse (scored by
    max cosine at match time). `source` records which recording the sample came
    from — kept as a rolling list aligned with the samples, so the GUI can show
    provenance and locate playable audio.

    `source_ids` runs beside `sources`, one entry per sample, holding that
    meeting's immutable id (or None when it has none). The NAME can go stale —
    a rename chase is best-effort and a meeting can move while nothing is
    watching — and then the ▶ button dies; the id cannot. Callers who already
    hold the id (a sample being reassigned between people) pass `source_id`
    rather than have it re-derived from a base that may no longer resolve.

    Two parallel lists, not a list of {id, base} pairs: every existing registry
    on disk carries plain-string `sources`, several readers index straight into
    it, and a shape change would have to be migrated everywhere at once. A
    parallel list is additive — an entry without one reads exactly as it did.

    An empty name, or one with a line break or an invisible control
    character in it, raises names.BadName (a ValueError) before anything is
    written: '   ' became '   .npy', and 'Bad\nName' became a person whose
    name split the transcript header in two.
    """
    name = names.person_name(name)
    if reserved_name(name):
        raise ValueError(RESERVED_ERROR)
    _private_dir()
    with lock_registry():
        reg = load_registry_for_write()   # a damaged registry: no stray .npy
        name = canonical_name(name, reg)
        # reuse this person's OWN file if already enrolled — their filename may
        # have been disambiguated at first enrollment, so recomputing it fresh
        # here could drift from what the registry actually points at. A
        # brand-new name gets one that can't collide with any OTHER enrolled
        # person's file.
        fname = reg[name]["file"] if name in reg else _unique_filename(
            names.file_stem(name), reg)
        fpath = config.VOICEPRINTS_DIR / fname
        vector = _l2(vector)  # raises on zero/non-finite
        if source_id is None and source and source != "?":
            source_id = config.id_of(source)

        if name in reg and fpath.exists() and not replace:
            arr = np.load(fpath)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            if any(cosine(vector, row) > 0.999 for row in arr):
                return fpath  # same sample again (e.g. re-promote after a relabel) — skip
            prev_sources = reg[name].get("sources", ["?"] * arr.shape[0])
            prev_ids = aligned_source_ids(reg[name]) or [None] * arr.shape[0]
            arr = np.vstack([arr, vector])
            sources = prev_sources + [source or "?"]
            source_ids = prev_ids + [source_id]
            arr, sources, source_ids = _cap_keeping_newest_and_diverse(
                arr, sources, source_ids, MAX_SAMPLES)
        else:
            arr = vector.reshape(1, -1)
            sources = [source or "?"]
            source_ids = [source_id]

        reg[name] = {"file": fname, "dim": int(vector.shape[0]),
                     "n_samples": int(arr.shape[0]), "sources": sources,
                     "source_ids": source_ids}
        _save_npy(fpath, arr)
        save_registry(reg)
        return fpath

"""A library-wide index of every meeting's cluster centroids, so an identity
change resolves to the handful of meetings it can actually affect.

Naming one person used to spawn `relabel --all`: 99 meetings, ~7 seconds each,
seven minutes of work to change at most a few transcripts. The centroids are
already on disk in each meeting's `.diar.npz`; reading all of them every time is
the only slow part, so they are cached in `centroid_index.npz` under the project
dir and refreshed per meeting by that cache file's mtime. Each meeting's roster
names ride beside them in `centroid_index.json`, refreshed by the meeting
json's own mtime, so a name lookup inside an HTTP request never parses a
multi-megabyte transcript it has already seen.

`targets()` answers "which meetings can this change touch?" from three
independent signals — a voice that scores near threshold against some cluster, a
meeting whose roster already carries the name, and a meeting an unknown-voice id
was heard in. It is deliberately GENEROUS: the margin below the naming threshold
and the union of all three signals mean a borderline meeting is relabeled
needlessly rather than missed.
"""
import hashlib
import json
import os

import numpy as np

from . import config, diarcache, identify, unknowns

INDEX_NAME = "centroid_index.npz"
META_NAME = "centroid_index.json"
# how far BELOW the naming threshold a cluster still counts as a target: a
# change that moves a score by a little must not be missed because the cluster
# sat just under the bar before it.
TARGET_MARGIN = 0.10


def index_path():
    return config.PROJECT_DIR / INDEX_NAME


def meta_path():
    return config.PROJECT_DIR / META_NAME


def _key(base: str) -> str:
    """A stable npz member name for a base. Meeting names carry spaces, dots
    and punctuation, and they are the cache key — hashing keeps the archive
    member names legal and keeps them from changing when the library does."""
    return "b" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _cached_bases() -> list:
    return [b for b in config.meeting_bases()
            if config.meeting_file(b, ".diar.npz").exists()]


def _load_cache():
    """(meta, arrays) from disk, or ({}, {}) if either half is missing or
    unreadable. A corrupt cache is a slow pass, never a wrong answer."""
    try:
        meta = json.loads(meta_path().read_text())
        if not isinstance(meta, dict):
            return {}, {}
    except (OSError, ValueError):
        return {}, {}
    try:
        with np.load(index_path(), allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
    except Exception:
        # any damage at all: a truncated zip raises BadZipFile, a bad member
        # raises zlib.error or EOFError. Each one escaped the old (OSError,
        # ValueError) catch, so the refresh never wrote a good index, and
        # every naming fell back to relabel --all until the file was deleted.
        return {}, {}
    return meta, arrays


def _save_cache(meta: dict, arrays: dict):
    config.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    # atomic on both halves: a crash mid-write must not leave a json sidecar
    # promising vectors the npz does not hold.
    tmp = str(index_path()) + ".tmp"
    with open(tmp, "wb") as fh:
        np.savez(fh, _empty=np.zeros(0), **arrays)
    os.replace(tmp, index_path())
    tmpj = str(meta_path()) + ".tmp"
    with open(tmpj, "w") as fh:
        json.dump(meta, fh)
    os.replace(tmpj, meta_path())


def _json_stamp(base: str):
    """[mtime_ns, size] of the meeting json, or None when it cannot be read.
    Taken BEFORE the json is parsed: a write that lands in between leaves an
    older stamp beside newer names, which the next pass simply re-reads."""
    try:
        st = config.meeting_file(base, ".json").stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _read_roster(base: str) -> list:
    """The names on this meeting's speaker roster, sorted. It is the only
    record of a meeting-local or manually-added name, which no centroid can
    match. An unreadable json has no names."""
    try:
        data = json.loads(config.meeting_file(base, ".json").read_text())
    except (OSError, ValueError):
        return []
    speakers = data.get("speakers") if isinstance(data, dict) else None
    names = set()
    for s in speakers or []:
        if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"]:
            names.add(s["name"])
    return sorted(names)


def _unpack(entry: dict, arrays: dict):
    vecs = arrays.get(entry.get("key"))
    labels = entry.get("labels") or []
    if vecs is None or len(vecs) != len(labels):
        return None
    return {str(l): np.asarray(v, float) for l, v in zip(labels, vecs)}


def index(refresh: bool = True) -> dict:
    """{base: {cluster_label: centroid vector}} for every meeting with a
    `.diar.npz`.

    With refresh (the default) each meeting is re-read only when its cache file
    is new or its mtime moved, meetings that no longer exist are dropped, and
    the on-disk index is rewritten when any of that changed. With refresh=False
    nothing on disk is touched and only what the cache already holds is
    returned."""
    if not refresh:
        meta, arrays = _load_cache()
        out = {}
        for base in _cached_bases():
            ent = meta.get(base)
            cents = _unpack(ent, arrays) if ent else None
            if cents is not None:
                out[base] = cents
        return out
    return _refresh()[0]


def _refresh():
    """(centroids, meta) with both brought up to date on disk. The meta entry
    for each base carries its roster names, re-read only when the meeting
    json's [mtime_ns, size] moved."""
    meta, arrays = _load_cache()
    bases = _cached_bases()
    out, new_meta, new_arrays = {}, {}, {}
    recomputed = False
    for base in bases:
        p = config.meeting_file(base, ".diar.npz")
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue  # it moved between the listing and now — next pass gets it
        ent = meta.get(base)
        cents = None
        if ent is not None and ent.get("mtime") == mtime:
            cents = _unpack(ent, arrays)
        if cents is None:
            recomputed = True
            try:
                _, _, cent_emb, _ = diarcache.load(p)
            except Exception:
                # an unreadable cache is still a meeting: index it as having no
                # clusters, so it is not re-read on every single call
                cent_emb = {}
            cents = {str(k): np.asarray(v, float) for k, v in cent_emb.items()}
        labels = list(cents.keys())
        vecs = (np.asarray([cents[l] for l in labels], dtype=float)
                if labels else np.zeros((0, 0)))
        stamp = _json_stamp(base)
        names = ent.get("names") if ent is not None else None
        if (stamp is None or ent is None or ent.get("json") != stamp
                or not isinstance(names, list)):
            names = _read_roster(base)
            if ent is None or ent.get("json") != stamp or ent.get("names") != names:
                recomputed = True
        new_meta[base] = {"mtime": mtime, "key": _key(base), "labels": labels,
                          "json": stamp, "names": names}
        new_arrays[_key(base)] = vecs
        out[base] = cents
    if recomputed or set(new_meta) != set(meta):
        try:
            _save_cache(new_meta, new_arrays)
        except OSError:
            pass  # a cache we could not write is a slow next pass, not an error
    return out, new_meta


def _named_bases(names: set, meta: dict) -> set:
    """Meetings whose roster already carries one of these names, answered from
    the roster names cached in the index meta. A transcript json runs to
    megabytes and this runs inside a naming, rename or merge request, so the
    json is parsed only when its mtime moved (in _refresh), never here."""
    return {base for base, ent in meta.items()
            if isinstance(ent, dict) and names & set(ent.get("names") or [])}


def _uid_bases(uids: set) -> set:
    """Meetings an unknown-voice id was heard in, straight from its registry
    entry — the authoritative record, and one no score can reconstruct once the
    id has been merged or promoted. Read through resolved_meetings, so a meeting
    renamed after the reference was written answers under its live name (its
    meeting id carries it) instead of being filtered out as unknown."""
    hit = set()
    speakers = (unknowns.load() or {}).get("speakers") or {}
    for uid in uids:
        ent = speakers.get(uid)
        if isinstance(ent, dict):
            hit.update(str(m) for m in unknowns.resolved_meetings(ent) if m)
    return hit


def targets(vector=None, names=(), uids=()) -> list:
    """Sorted bases an identity change can affect.

    - `vector`: one embedding, or a stack of them (a person's whole voiceprint).
      A meeting is a target when ANY of its clusters scores at or above
      NAMING_THRESHOLD - TARGET_MARGIN against it.
    - `names`: meetings whose speaker roster already carries one of these names.
    - `uids`: meetings listed under an unknown-voice id's `meetings`.

    Only meetings with a `.diar.npz` are returned — `relabel` skips the rest,
    exactly as `all_bases()` does."""
    names = {str(n) for n in (names or []) if str(n).strip()}
    uids = {str(u) for u in (uids or []) if str(u).strip()}
    idx, meta = _refresh()
    hit = set()

    if vector is not None:
        samples = np.atleast_2d(np.asarray(vector, dtype=float))
        if samples.size:
            floor = config.NAMING_THRESHOLD - TARGET_MARGIN
            for base, cents in idx.items():
                for vec in cents.values():
                    if identify.score_against(vec, samples) >= floor:
                        hit.add(base)
                        break
    if names:
        hit |= _named_bases(names, meta) & set(idx)
    if uids:
        hit |= _uid_bases(uids) & set(idx)
    return sorted(hit)

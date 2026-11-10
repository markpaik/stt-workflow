"""The voice registries' health check, and the repairs that are safe to
automate.

The audit of 2026-09-13 found the two failure modes this module exists for.
U050 linked two clusters that score 0.48 against each other across two PSSP
meetings — one global number spanning two people, because every link appends
a centroid and a chain of barely-passing matches walks the stack away from the
voice it started on. U028 and U040 were identical twins — one voice holding
two numbers, minted fresh by every relabel pass. Phase 4 of
docs/relabel-redesign.md stops both from forming. This module finds the ones
already on disk, and the lock and reference litter beside them.

A `check` never writes. `fix` writes, and deliberately does LESS than it
could: two enrolled people who share a voice are reported and left alone,
because merging two named profiles destroys a distinction only the user can
judge. Everything else — one voice under two global numbers, a stack that
spans two voices, a reference to a meeting that no longer exists, a lock file
nobody can ever ask for again, a counter below its own high-water mark — has
one right answer, and this applies it.

The invariants the hygiene test guards are the same ones `check` runs, so
there is one definition of "clean" and the test cannot drift from the tool.
"""
import contextlib
import json
import os
import threading
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import config, identify, unknowns
from .identify import cosine

# two entries this close are the same voice. Well under the 0.999 the old
# hygiene checker used: 0.999 only ever caught a byte-identical twin, and the
# U028/U040 pair was found by hand, not by the checker.
DUPLICATE_SIM = config.env_float("STT_DOCTOR_DUPLICATE_SIM", 0.95)
# two rows INSIDE one stack this close add nothing to max-cosine matching and
# squeeze out a diverse sample at the cap
STACK_DUP_SIM = 0.999


class Finding(NamedTuple):
    """One defect. `kind` is what it is, `subject` is what it is about (a
    name, a global id, a pair 'U028/U040', a filename), `detail` is the
    sentence a human reads, and `fixable` says whether fix() will touch it."""
    kind: str
    subject: str
    detail: str
    fixable: bool


def format_finding(f: Finding) -> str:
    return f"{f.subject}: {f.detail}"


# ---------- reading ----------

def _stack(path: Path):
    """A sample file as a 2-D array, or None when it is not there."""
    if not path.exists():
        return None
    arr = np.load(path)
    return arr.reshape(1, -1) if arr.ndim == 1 else arr


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


_UNREADABLE = object()


def _read_store(path: Path, default):
    """A registry file, or `default` when it is not there, or _UNREADABLE
    when it is there and cannot be read. A missing registry is a library
    nobody enrolled in yet. A corrupt one reads as empty everywhere else
    (identify.load_registry warns and carries on), so this is the one place
    that says so in a report."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return _UNREADABLE


def _corrupt(name: str, who: str) -> Finding:
    return Finding("corrupt", name,
                   f"cannot be read, so every {who} reads as gone. Restore "
                   f"{name}.bak, or rebuild it with tools/rebuild_voiceprints.py",
                   False)


def _entry_problem(meta, list_key):
    """Why one registry entry cannot be checked, or None. Each shape here
    crashed the doctor with an AttributeError or a TypeError."""
    if not isinstance(meta, dict):
        return "registry entry is not an object"
    if not isinstance(meta.get("file"), str) or not meta.get("file"):
        return "registry entry names no voiceprint file"
    if not isinstance(meta.get(list_key) or [], list):
        return f"'{list_key}' is not a list"
    return None


def _bad_row(row) -> bool:
    return not np.isfinite(row).all() or abs(np.linalg.norm(row) - 1.0) > 1e-3


class Library(NamedTuple):
    """What the meeting store holds right now, read once. `bases` is every
    meeting a reference may legitimately name — live, or archived (an archived
    meeting's files are still there, and a restore brings it back). `ids` is
    the id index AS STORED: read, never rebuilt, because a report must not
    write anything, not even a cache."""
    bases: frozenset
    ids: dict

    def alive(self, base, mid) -> bool:
        if base in self.bases:
            return True
        return bool(mid) and self.ids.get(mid) in self.bases


def _library(md: Path) -> Library:
    return Library(frozenset(config.meeting_bases(md)) | frozenset(
        config.archived_bases(md)), config.load_meetings_index()["ids"])


def _dead_ref(base, mid, lib: Library) -> bool:
    """A reference whose base AND id both resolve to nothing on disk. An empty
    or '?' base is not a reference at all."""
    if not base or base == "?":
        return False
    return not lib.alive(base, mid)


# ---------- the checks ----------

def _check_named(vp: Path, lib: Library) -> list:
    out = []
    reg = _read_store(vp / "registry.json", {})
    if reg is _UNREADABLE or not isinstance(reg, dict):
        return [_corrupt("registry.json", "enrolled person")]
    stacks = {}
    for name, meta in reg.items():
        why = _entry_problem(meta, "sources")
        if why:
            out.append(Finding("invalid", name, why, False))
            continue
        try:
            arr = _stack(vp / meta["file"])
        except Exception:
            out.append(Finding("invalid", name, f"voiceprint file {meta['file']} "
                               "cannot be read", False))
            continue
        if arr is None:
            out.append(Finding("invalid", name,
                               f"voiceprint file {meta.get('file')} missing", False))
            continue
        if meta.get("n_samples") not in (None, arr.shape[0]):
            out.append(Finding("invalid", name,
                               f"n_samples={meta['n_samples']} but {arr.shape[0]} "
                               "rows on disk", False))
        srcs = meta.get("sources")
        if srcs is not None and len(srcs) != arr.shape[0]:
            out.append(Finding("invalid", name,
                               f"{len(srcs)} sources for {arr.shape[0]} samples",
                               False))
        for i, row in enumerate(arr):
            if _bad_row(row):
                out.append(Finding("invalid", f"{name}[{i}]",
                                   "not a finite unit vector", False))
        for i in range(arr.shape[0]):
            for j in range(i + 1, arr.shape[0]):
                if cosine(arr[i], arr[j]) > STACK_DUP_SIM:
                    out.append(Finding("duplicate", name,
                                       f"samples {i} and {j} are duplicates", False))
        stacks[name] = arr
        if lib.bases:
            for base, mid in zip(srcs or [], identify.aligned_source_ids(meta)):
                if _dead_ref(base, mid, lib):
                    out.append(Finding("dangling", name,
                                       f"sample source '{base}' names no meeting "
                                       "on disk", True))
    names = sorted(stacks)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            worst = max(cosine(x, y) for x in stacks[a] for y in stacks[b])
            if worst >= DUPLICATE_SIM:
                # NOT fixable: two enrolled people sharing a voice is either a
                # mis-enrolled sample or the same person under two spellings,
                # and only the user knows which.
                out.append(Finding("duplicate", f"{a}/{b}",
                                   "two enrolled profiles share a voice "
                                   f"(cross cosine {worst:.3f})", False))
    return out


def _check_unknowns(vp: Path, lib: Library) -> list:
    out = []
    raw = _read_store(vp / "unknowns.json", {"speakers": {}})
    speakers = raw.get("speakers") if isinstance(raw, dict) else None
    if raw is _UNREADABLE or not isinstance(speakers, dict):
        return [_corrupt("unknowns.json", "unknown voice")]
    try:
        nxt = int(raw.get("next", 1))
    except (TypeError, ValueError):
        nxt = 1
    floor = unknowns.next_floor(raw)
    if nxt < floor:
        out.append(Finding("stale_next", "unknowns.json",
                           f"next={raw.get('next')} is below max+1={floor}, so a "
                           "number already handed out would be handed out again",
                           True))
    stacks = {}
    for uid, meta in speakers.items():
        why = _entry_problem(meta, "meetings")
        if why:
            out.append(Finding("invalid", uid, why, False))
            continue
        try:
            arr = _stack(vp / meta["file"])
        except Exception:
            out.append(Finding("invalid", uid, f"sample file {meta['file']} "
                               "cannot be read", False))
            continue
        if arr is None:
            out.append(Finding("invalid", uid,
                               f"sample file {meta.get('file')} missing", False))
            continue
        bad = False
        for i, row in enumerate(arr):
            if _bad_row(row):
                bad = True
                out.append(Finding("invalid", f"{uid}[{i}]",
                                   "not a finite unit vector", False))
        if lib.bases:
            for base, mid in zip(meta.get("meetings") or [],
                                 unknowns.aligned_meeting_ids(meta)):
                if _dead_ref(base, mid, lib):
                    out.append(Finding("dangling", uid,
                                       f"meeting '{base}' names no meeting on disk",
                                       True))
        if meta.get("dropped"):
            continue        # a tombstone is not a speaker: never merged, never split
        if bad:
            # a NaN or non-unit row scores -1.0 against everything, so the
            # stack also read as a "split" and as a "duplicate" of a healthy
            # voice. --fix then merged the bad row INTO that voice, and a
            # second run split it off again. The invalid finding is the one
            # report: a person repairs it, and no merge or split touches it.
            continue
        worst = unknowns.min_pairwise(arr)
        if worst < unknowns.LINK_STACK_MIN:
            out.append(Finding("split", uid,
                               f"stack spans two voices (min pairwise cosine "
                               f"{worst:.3f} < {unknowns.LINK_STACK_MIN})", True))
        stacks[uid] = arr
    uids = sorted(stacks)
    for i, a in enumerate(uids):
        for b in uids[i + 1:]:
            worst = max(cosine(x, y) for x in stacks[a] for y in stacks[b])
            if worst >= DUPLICATE_SIM:
                out.append(Finding("duplicate", f"{a}/{b}",
                                   "one voice under two global numbers "
                                   f"(cross cosine {worst:.3f})", True))
    return out


def _check_files(vp: Path) -> list:
    """A .npy no registry entry points at. After a lost or corrupt registry
    and one new enrollment the report said "clean" while five people's
    voiceprints sat unreferenced. Never fixable: the file may be the only
    copy of someone's voice, and only a person can say whose."""
    claimed = set()
    for path, key in (("registry.json", None), ("unknowns.json", "speakers")):
        d = _read_store(vp / path, {})
        if isinstance(d, dict) and key:
            d = d.get(key)
        if not isinstance(d, dict):
            continue
        for meta in d.values():
            if isinstance(meta, dict) and isinstance(meta.get("file"), str):
                claimed.add(meta["file"].casefold())
    try:
        files = sorted(vp.glob("*.npy"))
    except OSError:
        return []
    return [Finding("unreferenced_file", p.name,
                    "no registry entry points at this voiceprint file (a lost "
                    "registry, or a rename that stopped halfway); it may be "
                    "someone's only copy, so it is left alone", False)
            for p in files if p.name.casefold() not in claimed]


def check(voiceprints_dir=None, meetings_dir=None) -> list:
    """Every defect the registries currently carry. Reads only.

    The dangling-reference check is skipped when the meeting library holds
    nothing: with no meetings on disk every reference looks dead, and a report
    that says so about a library it simply cannot see is noise, not evidence.
    """
    vp = Path(voiceprints_dir) if voiceprints_dir is not None else config.VOICEPRINTS_DIR
    md = Path(meetings_dir) if meetings_dir is not None else config.meetings_dir()
    lib = _library(md)
    out = []
    out += _check_named(vp, lib)
    out += _check_unknowns(vp, lib)
    out += _check_files(vp)
    for f in config.orphan_lock_files(md):
        out.append(Finding("orphan_lock", f.name,
                           "lock file for a meeting that no longer locks under "
                           "that key", True))
    return out


# ---------- the repairs ----------

def _partition(arr) -> list:
    """Split a stack into groups that each agree with themselves: every pair
    inside a group is at or above LINK_STACK_MIN. Largest group first, and
    ties broken by the earliest row, so the repair is deterministic.

    Greedy on purpose. The honest partition of a drifted stack is ambiguous —
    that is what makes it drifted — and a clique-by-construction keep-group is
    the version the user can check by ear: the biggest set of samples that all
    sound like each other stays on the number, and the rest move off it."""
    n = int(np.asarray(arr).shape[0])
    rest = list(range(n))
    groups = []
    while rest:
        def mean_to(i):
            others = [j for j in rest if j != i]
            if not others:
                return 1.0
            return sum(cosine(arr[i], arr[j]) for j in others) / len(others)
        seed = max(rest, key=lambda i: (mean_to(i), -i))
        g = [seed]
        for j in rest:
            if j != seed and all(cosine(arr[j], arr[k]) >= unknowns.LINK_STACK_MIN
                                 for k in g):
                g.append(j)
        g.sort()
        groups.append(g)
        rest = [i for i in rest if i not in g]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _unlink_split(uid: str) -> list:
    """Move the samples that fall below the floor off `uid` and onto fresh
    global numbers, carrying their meetings and their link evidence with them.

    Samples are appended one per meeting and the stack is capped, so the rows
    align with the LAST n meetings — the same best-effort alignment promote()
    uses. A meeting older than the sample window cannot be attributed to any
    row and stays where it is."""
    done = []
    with identify.lock_registry():
        reg = unknowns.load()
        meta = reg["speakers"].get(uid)
        if meta is None:
            return done
        arr = unknowns._samples(reg, uid)
        if arr is None:
            return done
        groups = _partition(arr)
        if len(groups) < 2:
            return done
        mts = list(meta.get("meetings") or [])
        ids = unknowns.aligned_meeting_ids(meta)
        links = list(meta.get("links") or [])
        off = len(mts) - arr.shape[0]     # rows align to the last n meetings

        def meetings_of(rows):
            out = []
            for i in rows:
                k = off + i
                if 0 <= k < len(mts):
                    out.append(k)
            return out

        moved_idx = set()
        for g in groups[1:]:
            new_uid = unknowns.mint_uid(reg)
            fname = f"{new_uid}.npy"
            np.save(config.VOICEPRINTS_DIR / fname, arr[g])
            ks = meetings_of(g)
            moved_idx.update(ks)
            moved_names = {mts[k] for k in ks}
            reg["speakers"][new_uid] = {
                "file": fname,
                "meetings": [mts[k] for k in ks],
                "meeting_ids": [ids[k] for k in ks],
                "created": meta.get("created"),
                "unlinked_from": uid,
                "links": [ln for ln in links if ln.get("meeting") in moved_names],
            }
            done.append(f"split {uid}: {len(g)} sample(s) moved to {new_uid} "
                        f"({', '.join(sorted(moved_names)) or 'no meeting ref'})")
        keep = groups[0]
        np.save(config.VOICEPRINTS_DIR / meta["file"], arr[keep])
        kept_names = {mts[k] for k in range(len(mts)) if k not in moved_idx}
        meta["meetings"] = [m for k, m in enumerate(mts) if k not in moved_idx]
        meta["meeting_ids"] = [i for k, i in enumerate(ids) if k not in moved_idx]
        meta["links"] = [ln for ln in links if ln.get("meeting") in kept_names]
        unknowns.save(reg)
    return done


def _drop_dangling(subject: str, is_unknown: bool, lib: Library) -> list:
    """Remove every reference on one entry whose meeting is gone."""
    done = []
    with identify.lock_registry():
        if is_unknown:
            reg = unknowns.load()
            meta = reg["speakers"].get(subject)
            if meta is None:
                return done
            mts = list(meta.get("meetings") or [])
            ids = unknowns.aligned_meeting_ids(meta)
            keep = [k for k in range(len(mts)) if not _dead_ref(mts[k], ids[k], lib)]
            dead = [mts[k] for k in range(len(mts)) if k not in keep]
            if not dead:
                return done
            meta["meetings"] = [mts[k] for k in keep]
            meta["meeting_ids"] = [ids[k] for k in keep]
            meta["links"] = [ln for ln in (meta.get("links") or [])
                             if ln.get("meeting") not in set(dead)]
            unknowns.save(reg)
            done.append(f"{subject}: dropped dead meeting ref(s) {', '.join(dead)}")
        else:
            reg = identify.load_registry()
            meta = reg.get(subject)
            if meta is None:
                return done
            srcs = list(meta.get("sources") or [])
            ids = identify.aligned_source_ids(meta)
            dead = [k for k in range(len(srcs)) if _dead_ref(srcs[k], ids[k], lib)]
            if not dead:
                return done
            # the sources list is INDEX-ALIGNED with the sample rows: a sample
            # keeps its row, it just no longer claims to come from a meeting
            # that is not there.
            meta["sources"] = ["?" if k in dead else s for k, s in enumerate(srcs)]
            meta["source_ids"] = [None if k in dead else i for k, i in enumerate(ids)]
            identify.save_registry(reg)
            done.append(f"{subject}: blanked dead sample source(s) "
                        f"{', '.join(srcs[k] for k in dead)}")
    return done


# fix() is the only code in this process that repoints config.VOICEPRINTS_DIR,
# and two fix() calls on two threads must not interleave their swaps.
_REPOINT = threading.Lock()


@contextlib.contextmanager
def _voiceprints_at(voiceprints_dir):
    """Point every registry read and write at `voiceprints_dir` for the span
    of one fix(), and put the configured directory back in a finally.

    Why a swap and not a parameter: the repairs go through unknowns.merge,
    unknowns.load/save, identify.load_registry/save_registry and
    identify.lock_registry, and every one of them reads config.VOICEPRINTS_DIR
    at call time. `--fix --voiceprints DIR` used to check DIR and then repair
    the CONFIGURED registry — merging, splitting and healing numbers in a
    directory nobody asked it to touch. Under the swap, lock_registry() also
    takes DIR's own .registry.lock, which is the lock that guards DIR.

    The swap is process-global. The CLI is fix()'s only caller and runs one
    thread, so nothing else reads the global while it is swapped. A
    long-running process (the panel server) must call fix() with
    voiceprints_dir=None, which never swaps. A swap is also refused while
    this thread already holds the registry lock: lock_registry() is
    re-entrant by depth, so a nested acquire would skip DIR's lock file."""
    if voiceprints_dir is None:
        yield
        return
    target = Path(voiceprints_dir)
    if target.resolve() == Path(config.VOICEPRINTS_DIR).resolve():
        yield
        return
    if getattr(identify._lock_depth, "n", 0):
        raise RuntimeError("registry_doctor.fix() cannot repoint the voiceprints "
                           "directory while this thread holds the registry lock")
    with _REPOINT:
        prev = config.VOICEPRINTS_DIR
        config.VOICEPRINTS_DIR = target
        try:
            yield
        finally:
            config.VOICEPRINTS_DIR = prev


def fix(findings, voiceprints_dir=None, meetings_dir=None) -> list:
    """Apply the repairs the findings authorize, and report what was done.

    Every repair lands in `voiceprints_dir` — the directory check() read —
    and never in the configured one when the two differ (see
    _voiceprints_at).

    Order matters: duplicates merge before splits unlink (a merge changes a
    stack, so the split test must see the merged one), and `next` heals last
    so it covers every number minted along the way."""
    with _voiceprints_at(voiceprints_dir):
        return _fix(findings, meetings_dir)


def _fix(findings, meetings_dir) -> list:
    md = Path(meetings_dir) if meetings_dir is not None else config.meetings_dir()
    lib = _library(md)
    done = []

    for f in findings:
        if f.kind != "duplicate" or not f.fixable or "/" not in f.subject:
            continue
        a, b = f.subject.split("/", 1)
        # keep the OLDER (lower) number: it is the one other stores already
        # reference, and the monotonic counter means it is never reissued.
        src, dst = (b, a) if unknowns.uid_num(a) < unknowns.uid_num(b) else (a, b)
        if unknowns.merge(src, dst):
            done.append(f"merged {src} into {dst}")
        else:
            done.append(f"could NOT merge {src} into {dst} (already gone, or a "
                        "dropped tombstone on one side)")

    for f in findings:
        if f.kind == "split" and f.fixable:
            done += _unlink_split(f.subject)

    unknown_ids = set(unknowns.load()["speakers"])
    seen = set()
    for f in findings:
        if f.kind != "dangling" or not f.fixable or f.subject in seen:
            continue
        seen.add(f.subject)
        done += _drop_dangling(f.subject, f.subject in unknown_ids, lib)

    n = config.prune_lock_files(md)
    if n:
        done.append(f"pruned {n} orphan lock file(s)")

    with identify.lock_registry():
        # load() heals `next` in memory. A damaged file is None here and is
        # never written: the check already reported it as corrupt, and a
        # save would put an empty registry over every unknown voice.
        reg = unknowns.load_for_write()
        if reg is not None:
            before = _read_json(config.VOICEPRINTS_DIR / "unknowns.json", {}).get("next")
            if before != reg["next"]:
                unknowns.save(reg)
                done.append(f"healed unknowns.json next: {before} -> {reg['next']}")
    return done

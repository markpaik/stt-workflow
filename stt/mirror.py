"""A copy of each meeting's transcript in one more folder, STT_MIRROR_DIR,
that another program syncs (Google Drive for desktop, for one). The setting
is off by default.

The mirror folder holds one file per live meeting: "<base>.txt", a byte
copy of the meeting's current .txt, flat in the folder. It holds no audio,
no json and no caches. A strict meeting is never copied. A meeting whose
json cannot be read counts as strict, and so does a .txt whose header line
says STRICT, so a doubt keeps the text on this Mac.

reconcile() brings the folder in line with the library, and it is the only
writer:
- It writes a file only when the content differs (sha256), through a temp
  file in the mirror folder and a rename, so the sync sees one change.
- A renamed meeting's old file goes. A deleted, archived or strict
  meeting's file goes. A restored meeting's file comes back.
- It keeps its own record, mirror_state.json in the data home (mode 600):
  each file it wrote, with its hash. It never deletes or overwrites a file
  it did not write, or one that changed after it wrote it. A file someone
  puts in the folder by hand stays.
- A folder that is missing or not writable gives one log line, and the
  next run retries. Nothing here raises into the batch or a panel action.

touch(*bases) asks for the meetings that changed, in the background. The
batch reconciles the whole folder at the end of each run, so a missed
touch heals within one batch launch.
"""
import atexit
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import NamedTuple

from . import config

SETTING = "STT_MIRROR_DIR"
STATE_NAME = "mirror_state.json"
LOCK_NAME = "mirror.lock"
TMP_PREFIX = ".stt-mirror-"
# the mark output.txt_header puts on a strict meeting's header line
STRICT_MARK = b"  |  STRICT mode"
LOCK_WAIT_SEC = 60.0
EXIT_WAIT_SEC = 30.0


# ---------- the setting ----------

def _raw() -> str:
    """STT_MIRROR_DIR from stt.env (read fresh), then the environment."""
    v = config._env_file().get(SETTING) or os.environ.get(SETTING) or ""
    return str(v).strip()


def _fold(p) -> str:
    """A path as APFS compares names: letter case and Unicode composition
    do not matter."""
    return unicodedata.normalize("NFC", os.path.normpath(str(p))).casefold()


def _key(name: str) -> str:
    """A file name as APFS compares it: two names that differ only in
    letter case or Unicode form ('Café' composed or decomposed) are one
    file. A sync program may hand a name back in the other form."""
    return unicodedata.normalize("NFC", name).casefold()


def _same(a: str, b: str) -> bool:
    """The same name, exactly, up to its Unicode form."""
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


def _inside(p, root) -> bool:
    """True when `p` is `root` or a folder under it."""
    a, b = _fold(p), _fold(root)
    return a == b or a.startswith(b.rstrip("/") + "/")


def _guarded() -> list:
    """(label, folder) for each folder a mirror must never sit in: the
    library and the other private folders, and this checkout, because the
    repository is public and the mirror holds transcript text."""
    out = []
    for label, fn in (("the meetings library", config.meetings_dir),
                      ("the data home", lambda: config.PROJECT_DIR),
                      ("this checkout", lambda: config.CODE_DIR),
                      ("the voiceprints folder", lambda: config.VOICEPRINTS_DIR),
                      ("the recordings folder", config.recordings_dir),
                      ("the watched folder", config.source_dir)):
        try:
            out.append((label, Path(fn()).expanduser()))
        except Exception:
            pass
    return out


def setting_problem():
    """Why STT_MIRROR_DIR cannot be used, or None (unset is no problem: the
    mirror is off). The folder must be an absolute path, outside the
    library, the data home, this checkout and the other private folders.
    A folder that is missing is no setting problem: the doctor reports it
    as mirror_unavailable, and the next run retries."""
    raw = _raw()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return f"{raw!r} is not an absolute folder path; the mirror stays off"
    for label, root in _guarded():
        if _inside(p, root):
            return f"{raw!r} is inside {label}; the mirror stays off"
    return None


def mirror_dir():
    """The mirror folder, or None when STT_MIRROR_DIR is unset or cannot be
    used (setting_problem warns once per process)."""
    raw = _raw()
    if not raw:
        return None
    why = setting_problem()
    if why:
        config._warn_once((SETTING, why), f"{SETTING}={why}")
        return None
    return Path(os.path.normpath(Path(raw).expanduser()))


class Ctx(NamedTuple):
    """Where one reconcile reads and writes, fixed when it is asked for, so
    work that runs later in the background never follows a setting or a
    data home that changed in between."""
    mirror: Path
    store: Path
    home: Path


def context():
    """The Ctx for now, or None when the mirror is off."""
    m = mirror_dir()
    if m is None:
        return None
    return Ctx(m, Path(config.meetings_dir()), Path(config.PROJECT_DIR))


def folder_problem(p: Path):
    """Why the mirror folder cannot take a file now, or None. The folder is
    never created here: a missing one usually means the sync program is not
    running, and a folder made in its place would confuse it."""
    if not p.is_dir():
        return f"the mirror folder {p} is missing or not a folder"
    if not os.access(p, os.W_OK | os.X_OK):
        return f"the mirror folder {p} is not writable"
    return None


# ---------- the record ----------

def _sig(st) -> list:
    return [st.st_mtime_ns, st.st_size]


def _load(ctx: Ctx) -> dict:
    """The record: {"mirrors": {folder: {name: entry}}, "sources": {store:
    {base: facts}}}, with `files` (this folder's entries) and `cache` (this
    store's facts) pulled out. An unreadable record reads as empty, so every
    file in the folder is then someone else's and nothing is overwritten."""
    try:
        d = json.loads((ctx.home / STATE_NAME).read_text())
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    mirrors = d.get("mirrors") if isinstance(d.get("mirrors"), dict) else {}
    sources = d.get("sources") if isinstance(d.get("sources"), dict) else {}
    files = mirrors.get(str(ctx.mirror))
    files = {n: e for n, e in files.items() if isinstance(n, str) and isinstance(e, dict)
             and (isinstance(e.get("sha"), str) or isinstance(e.get("pending"), str))
             } if isinstance(files, dict) else {}
    cache = sources.get(str(ctx.store))
    cache = {b: c for b, c in cache.items() if isinstance(c, dict)
             } if isinstance(cache, dict) else {}
    return {"mirrors": mirrors, "sources": sources, "files": files, "cache": cache}


def _save(ctx: Ctx, st: dict):
    st["mirrors"][str(ctx.mirror)] = st["files"]
    st["sources"][str(ctx.store)] = st["cache"]
    p = ctx.home / STATE_NAME
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "mirrors": st["mirrors"],
                               "sources": st["sources"]}, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


_thread_lock = threading.Lock()


class Busy(Exception):
    """Another reconcile held the lock for LOCK_WAIT_SEC."""


@contextlib.contextmanager
def _locked(ctx: Ctx, wait=None):
    """One reconcile at a time across the panel, the batch and the tools."""
    wait = LOCK_WAIT_SEC if wait is None else wait
    deadline = time.monotonic() + wait
    if not _thread_lock.acquire(timeout=wait):
        raise Busy()
    try:
        with open(ctx.home / LOCK_NAME, "a") as fh:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy() from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        _thread_lock.release()


# ---------- what the library holds ----------

def _header_strict(data: bytes) -> bool:
    return STRICT_MARK in data.split(b"\n", 1)[0]


def _json_facts(path: Path, c: dict):
    """(sig, strict, meeting_id) of a meeting json. A json that cannot be
    read, or holds no object, is strict: a doubt never uploads."""
    try:
        with open(path, "rb") as fh:
            sig = _sig(os.fstat(fh.fileno()))
            if c.get("json") == sig and isinstance(c.get("strict"), bool):
                return sig, c["strict"], c.get("id")
            d = json.loads(fh.read())
    except OSError:
        return None, True, None
    except ValueError:
        return sig, True, None
    if not isinstance(d, dict):
        return sig, True, None
    mid = d.get("meeting_id")
    return sig, bool(d.get("strict")), mid if isinstance(mid, str) and mid else None


def _meeting(ctx: Ctx, base: str, cache: dict, want_bytes: bool = False):
    """{"base", "name", "sha", "strict", "id", "data"} for one live meeting,
    or None when it has no .txt. `data` is the .txt's bytes when they were
    read (always with want_bytes). A .txt or json whose size and time match
    the cache is not read again."""
    folder = ctx.store / base
    txt = folder / f"{base}.txt"
    c = dict(cache.get(base) or {})
    try:
        st = os.stat(txt)
    except OSError:
        cache.pop(base, None)
        return None
    data = None
    if (not want_bytes and c.get("txt") == _sig(st)
            and isinstance(c.get("sha"), str) and isinstance(c.get("hstrict"), bool)):
        sha, hstrict = c["sha"], c["hstrict"]
    else:
        try:
            with open(txt, "rb") as fh:
                tsig = _sig(os.fstat(fh.fileno()))
                data = fh.read()
        except OSError:
            cache.pop(base, None)
            return None
        sha, hstrict = hashlib.sha256(data).hexdigest(), _header_strict(data)
        c.update(txt=tsig, sha=sha, hstrict=hstrict)
    # the json AFTER the text. The pipeline writes the .txt first and
    # relabel writes the json first: a .txt read before a strict rewrite is
    # caught here by the json, and one read after it by its own header.
    jsig, strict, mid = _json_facts(folder / f"{base}.json", c)
    c.update(json=jsig, strict=strict, id=mid)
    cache[base] = c
    return {"base": base, "name": base + ".txt", "sha": sha,
            "strict": bool(strict or hstrict), "id": mid, "data": data}


def _file_sha(p: Path):
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _intact(ctx: Ctx, name: str, e: dict):
    """True when mirror file `name` still holds what the mirror wrote (its
    `sha`, or the `pending` write a crash may have finished), False when
    it changed after, and None when it is gone. A file whose size and time
    match the record is not read."""
    p = ctx.mirror / name
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    if e.get("sig") == _sig(st) and isinstance(e.get("sha"), str):
        return True
    sha = _file_sha(p)
    if sha is not None and sha == e.get("pending"):
        e["sha"] = e.pop("pending")
    if sha is not None and sha == e.get("sha"):
        e["sig"] = _sig(st)     # the same bytes with a new time: the sync touched it
        return True
    return False


# ---------- the plan ----------

def _op(kind: str, name: str, **kw) -> dict:
    return {"op": kind, "name": name, **kw}


def _plan(ctx: Ctx, st: dict, bases=None) -> list:
    """What the folder needs, as a list of ops. Reads only (it can refresh
    the cache and an entry's time stamp, which a dry run never saves).

    write    a file the folder lacks (new, or ours and gone)
    update   ours, with new content or a name that differs in case only
    remove   ours, for a meeting that is gone, archived or strict
    forget   a record entry whose file is gone
    keep     ours and current
    blocked  a file the mirror did not write holds the meeting's name
    changed  ours, changed after the mirror wrote it: left as it is
    strict   a strict meeting, which stays on this Mac (no file action)

    `bases` limits the pass to those meetings, matched by name and by
    meeting_id, so a rename finds the old file by the id. None is the
    whole folder."""
    disk = {_key(n): n for n in os.listdir(ctx.mirror)}
    files, cache = st["files"], st["cache"]
    if bases is None:
        scope = config.meeting_bases(ctx.store)
    else:
        # the live ones of these names, by meeting_bases' own rule (a folder
        # holding its json). The names come from the listing, never from a
        # path lookup: APFS finds 'Weekly' under 'weekly' too, and a rename
        # that changed only the case would then count one meeting twice.
        try:
            here = set(os.listdir(ctx.store))
        except FileNotFoundError:
            here = set()
        scope = sorted(b for b in set(bases)
                       if b in here and not b.startswith(".")
                       and (ctx.store / b / f"{b}.json").is_file())
    metas = [m for m in (_meeting(ctx, b, cache) for b in scope) if m]
    ops, desired = [], {}
    for m in metas:
        if m["strict"]:
            ops.append(_op("strict", m["name"], base=m["base"]))
            continue
        cf = _key(m["name"])
        if cf in desired:      # two names that differ in case only
            ops.append(_op("blocked", m["name"], base=m["base"]))
            continue
        desired[cf] = m
    if bases is None:
        scoped = dict(files)
    else:
        names = {_key(f"{b}.txt") for b in bases}
        ids = {m["id"] for m in metas if m["id"]}
        scoped = {n: e for n, e in files.items()
                  if _key(n) in names or (e.get("id") and e["id"] in ids)}
    owned = {_key(n): n for n in files}
    for cf, m in desired.items():
        name, rec = m["name"], owned.get(cf)
        if rec is None:
            if cf in disk:
                ops.append(_op("blocked", disk[cf], base=m["base"]))
            else:
                ops.append(_op("write", name, base=m["base"], sha=m["sha"]))
            continue
        e, cur = files[rec], disk.get(cf)
        ok = _intact(ctx, cur, e) if cur else None
        if ok is None:
            ops.append(_op("write", name, base=m["base"], sha=m["sha"], record=rec))
        elif not ok:
            ops.append(_op("changed", cur, base=m["base"]))
        elif e.get("sha") == m["sha"] and _same(cur, name) and _same(rec, name):
            ops.append(_op("keep", name, base=m["base"]))
        else:
            ops.append(_op("update", name, base=m["base"], sha=m["sha"],
                           record=rec, cur=cur))
    for n, e in scoped.items():
        cf = _key(n)
        if cf in desired:
            continue
        cur = disk.get(cf)
        ok = _intact(ctx, cur, e) if cur else None
        if ok is None:
            ops.append(_op("forget", n))
        elif not ok:
            ops.append(_op("changed", cur, base=e.get("base")))
        else:
            ops.append(_op("remove", cur, record=n))
    return ops


# ---------- doing it ----------

def _write(path: Path, data: bytes):
    """Bytes into `path` through a temp file beside it and one rename."""
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _apply(ctx: Ctx, st: dict, ops: list) -> dict:
    """Carry out the plan. Each write is marked `pending` in the record
    before it starts, so a crash between the file and the record leaves a
    file the next run still knows as its own. The first OSError ends the
    pass (one line, by the caller), and what was done stays recorded."""
    files, cache = st["files"], st["cache"]
    done = {"write": 0, "update": 0, "remove": 0}
    jobs = [o for o in ops if o["op"] in ("write", "update")]
    for o in jobs:
        # on the entry the file is known by now (a case-only rename keeps
        # its old entry until the move is done)
        e = files.setdefault(o.get("record") or o["name"], {"sha": None})
        e["pending"] = o["sha"]
        e.setdefault("base", o["base"])
    if jobs:
        _save(ctx, st)
    try:
        for o in ops:
            kind, name = o["op"], o["name"]
            if kind in ("write", "update"):
                key = o.get("record") or name
                # the bytes, read now: the plan may have used the cache, and
                # the meeting may have changed or turned strict since
                fresh = _meeting(ctx, o["base"], cache, want_bytes=True)
                if fresh is None or fresh["strict"]:
                    e = files.get(key) or {}
                    e.pop("pending", None)
                    if e.get("sha") is None:
                        files.pop(key, None)
                    continue
                prev = (files.get(key) or {}).get("sha")
                cur = o.get("cur")
                if cur and not _same(cur, name):
                    # a name that differs in case only: rename first, because
                    # a replace onto it keeps the old case on APFS
                    os.rename(ctx.mirror / cur, ctx.mirror / name)
                if not (kind == "update" and prev == fresh["sha"]):
                    _write(ctx.mirror / name, fresh["data"])
                files.pop(key, None)
                files[name] = {"sha": fresh["sha"],
                               "sig": _sig(os.stat(ctx.mirror / name)),
                               "id": fresh["id"], "base": fresh["base"]}
                done[kind] += 1
            elif kind == "remove":
                try:
                    os.unlink(ctx.mirror / name)
                    done["remove"] += 1
                except FileNotFoundError:
                    pass        # removed by someone else first: the same end
                files.pop(o.get("record") or name, None)
            elif kind == "forget":
                files.pop(name, None)
    finally:
        _save(ctx, st)
    return done


def _sweep_temps(ctx: Ctx):
    """Temp files a crashed write left. Only this module's prefix, and only
    under the lock, where no write of ours is in flight."""
    try:
        for n in os.listdir(ctx.mirror):
            if n.startswith(TMP_PREFIX) and n.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    os.unlink(ctx.mirror / n)
    except OSError:
        pass


def reconcile(bases=None, dry_run: bool = False, ctx=None) -> dict:
    """Bring the mirror folder in line with the library: the whole folder
    (bases=None), or just those meetings. Returns counts, the plan
    (`ops`) and `problem` (a missing or unwritable folder, a busy lock, a
    write that failed). Never logs. Raises only on a bug; every trigger
    catches that too."""
    ctx = ctx or context()
    out = {"ok": True, "off": ctx is None, "dry_run": dry_run, "problem": None,
           "ops": [], "write": 0, "update": 0, "remove": 0}
    if ctx is None:
        return out
    out["dir"] = str(ctx.mirror)
    why = folder_problem(ctx.mirror)
    if why:
        return {**out, "ok": False, "problem": why}
    try:
        if dry_run:
            st = _load(ctx)
            ops = _plan(ctx, st, bases)
            return {**out, "ops": ops,
                    **{k: sum(1 for o in ops if o["op"] == k)
                       for k in ("write", "update", "remove")}}
        with _locked(ctx):
            st = _load(ctx)
            ops = _plan(ctx, st, bases)
            out["ops"] = ops
            if bases is None:
                live = set(config.meeting_bases(ctx.store))
                st["cache"] = {b: c for b, c in st["cache"].items() if b in live}
                _sweep_temps(ctx)
            out.update(_apply(ctx, st, ops))
    except Busy:
        return {**out, "ok": False,
                "problem": "another mirror pass held the lock; this one waits for the next"}
    except OSError as e:
        return {**out, "ok": False,
                "problem": f"the mirror folder {ctx.mirror} could not be read or "
                           f"written ({e.strerror or type(e).__name__})"}
    return out


def counts(r: dict) -> dict:
    """How many ops of each kind a reconcile planned."""
    c = {}
    for o in r.get("ops") or []:
        c[o["op"]] = c.get(o["op"], 0) + 1
    return c


def summary_line(r: dict):
    """One log line for a reconcile, or None when it changed nothing and
    had no problem."""
    if r.get("off"):
        return None
    if r.get("problem"):
        return f"mirror: {r['problem']}; the next run retries"
    if not (r.get("write") or r.get("update") or r.get("remove")):
        return None
    c = counts(r)
    line = (f"mirror: {r['write']} written, {r['update']} updated, "
            f"{r['remove']} removed")
    left = [f"{c[k]} {k}" for k in ("blocked", "changed") if c.get(k)]
    if left:
        line += f" ({', '.join(left)}: tools/doctor.py check names them)"
    return line


# ---------- triggers ----------

_said = {"msg": None}


def _say(msg):
    """One stderr line per distinct problem: a panel that touches the
    mirror on every edit must not repeat the same line while Drive is
    away. A pass that works clears it, so the next problem is said."""
    if msg is None:
        _said["msg"] = None
        return
    if msg != _said["msg"]:
        _said["msg"] = msg
        print(msg, file=sys.stderr, flush=True)


_cv = threading.Condition()
_pending = {}
_worker = {"t": None}


def touch(*bases):
    """Mirror these meetings soon, in the background: the meetings a panel
    or tool action just changed, by every name they had (a rename passes
    the old and the new). Returns at once. Never raises."""
    try:
        bs = {b for b in bases if isinstance(b, str) and b}
        if not bs:
            return
        ctx = context()
        if ctx is None:
            return
        with _cv:
            _pending.setdefault(ctx, set()).update(bs)
            if _worker["t"] is None:
                t = threading.Thread(target=_drain, name="stt-mirror", daemon=True)
                _worker["t"] = t
                t.start()
    except Exception as e:
        _say(f"mirror: not updated now ({type(e).__name__}); the next batch run retries")


def _drain():
    try:
        while True:
            with _cv:
                if not _pending:
                    return
                ctx, bs = _pending.popitem()
            try:
                r = reconcile(sorted(bs), ctx=ctx)
                _say(f"mirror: {r['problem']}; the next batch run retries"
                     if r.get("problem") else None)
            except Exception as e:
                _say(f"mirror: not updated now ({type(e).__name__}); "
                     "the next batch run retries")
    finally:
        with _cv:
            _worker["t"] = None
            _cv.notify_all()
            if _pending:       # asked for while this thread was leaving
                t = threading.Thread(target=_drain, name="stt-mirror", daemon=True)
                _worker["t"] = t
                t.start()


def wait(timeout=None) -> bool:
    """Block until the background work is done. True when it is. A tool or
    a relabel run that exits right after a touch waits here (atexit), for
    at most EXIT_WAIT_SEC."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _cv:
        while _worker["t"] is not None or _pending:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            _cv.wait(left)
    return True


atexit.register(wait, EXIT_WAIT_SEC)


# ---------- the doctor ----------

def doctor_findings() -> list:
    """(kind, subject, detail) for tools/doctor.py check. Reads only.

    mirror_unavailable: STT_MIRROR_DIR is set, but the folder is missing
    or not writable. mirror_changed: a file the mirror wrote changed after,
    so the mirror leaves it as it is. mirror_blocked: a file the mirror did
    not write holds a meeting's name, so that meeting is not mirrored. A
    value that cannot be used is a bad_setting (setting_problem)."""
    ctx = context()
    if ctx is None:
        return []
    why = folder_problem(ctx.mirror)
    if why:
        return [("mirror_unavailable", SETTING,
                 f"{why}, so no transcript is copied there until it is back")]
    try:
        ops = reconcile(dry_run=True, ctx=ctx)["ops"]
    except Exception as e:
        return [("mirror_unavailable", SETTING,
                 f"the mirror folder {ctx.mirror} could not be read ({type(e).__name__})")]
    out = []
    for o in ops:
        if o["op"] == "changed":
            out.append(("mirror_changed", o["name"],
                        "changed after the mirror wrote it, so the mirror leaves "
                        "it as it is. Delete it, and the next run writes the "
                        "meeting's current transcript. Rename it to keep it as "
                        "your own file"))
        elif o["op"] == "blocked":
            out.append(("mirror_blocked", o["name"],
                        "a file the mirror did not write holds this meeting's "
                        "name, so the meeting is not mirrored. Rename or remove "
                        "that file"))
    return out

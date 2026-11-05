"""Voice Memos sync: bring finished meeting memos from the Voice Memos
library on this Mac into the batch, with no action on the phone (unified plan
Phase 2 step 4).

Counsel approved the sync and the iCloud storage, and the owner turned it
on. It runs at the start of every batch run.

What it reads, and how:

- The container `~/Library/Group Containers/group.com.apple.VoiceMemos.shared
  /Recordings`. Nothing here ever writes to it, deletes from it, or opens a
  file in it for writing. The database is read from a private COPY of
  `CloudRecordings.db` and its `-wal` and `-shm` files, because SQLite's
  immutable mode skips the write-ahead log and the newest memos live there.
- Table ZCLOUDRECORDING: ZUNIQUEID (the memo's id), ZPATH (its file, named
  'YYYYMMDD HHMMSS-XXXXXXXX.m4a' with the local start time), ZDATE (Core
  Data seconds since 2001-01-01), ZDURATION, and ZENCRYPTEDTITLE (the title
  as shown, in plain text).

The allow-list. A memo is copied only when all of these hold:

- it was recorded on or after GO_LIVE (2026-11-02);
- it runs at least MIN_SECONDS (3 minutes);
- its file has not changed for STABLE_SECONDS (5 minutes), and the
  recording ended at least that long ago, so a memo in progress is left
  alone;
- it is not in Voice Memos' Recently Deleted;
- it was never copied before, and no purge blocked it.

No capture marker is required. Every other memo counts as skipped by reason.

A schema change stops ingest. When the table or a needed column is missing,
or a start time cannot be read, the sync copies nothing, records the reason,
and the doctor reports it. A memo then waits on the phone, and nothing slips
through under a rule the code no longer understands.

The copy lands in a staging folder that the batch watches, with a `.vm.json`
sidecar (stt.provenance reads it): the true start, the title, whether a
person typed that title, and the default place name as a location note. The
batch deletes the staged copy after a verified store, never the original.

Full Disk Access: macOS guards this container. A launchd job reads it only
when the interpreter it runs holds Full Disk Access. A denied read is
recorded as `denied`, and the doctor reports it.
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, dates, titles

GO_LIVE = "2026-11-02"
MIN_SECONDS = 180.0
STABLE_SECONDS = 300.0
TABLE = "ZCLOUDRECORDING"
NEEDED_COLUMNS = ("ZUNIQUEID", "ZPATH", "ZDATE", "ZDURATION", "ZENCRYPTEDTITLE")
DB_NAME = "CloudRecordings.db"
STATE_NAME = "voicememos_state.json"
SIDECAR_SUFFIX = ".vm.json"
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# the file name Voice Memos gives a recording: its local start time, then
# the first 8 hex digits of an id. A duplicated memo gets another name.
_PATH_RE = re.compile(r"^(\d{8}) (\d{6})-[0-9A-Fa-f]{4,}\.m4a$")
_REAL = (Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared"
         / "Recordings")


# ---------- settings ----------

def _env(name, default=None):
    return config._env_file().get(name) or os.environ.get(name) or default


def enabled() -> bool:
    """On unless stt.env (or the environment) sets STT_VOICEMEMOS_SYNC=0."""
    return str(_env("STT_VOICEMEMOS_SYNC", "1")).strip() != "0"


def container_dir() -> Path:
    """The Recordings folder. STT_VOICEMEMOS_DIR wins. Under STT_HOME the
    default is a folder inside that home, so a demo or a test never reads
    the real library."""
    env = _env("STT_VOICEMEMOS_DIR")
    if env:
        return Path(env).expanduser()
    if config.STT_HOME:
        return Path(config.STT_HOME).expanduser() / "voicememos_container"
    return _REAL


def staging_dir() -> Path:
    env = _env("STT_VOICEMEMOS_STAGING")
    return Path(env).expanduser() if env else config.PROJECT_DIR / "voicememos"


def state_path() -> Path:
    return config.PROJECT_DIR / STATE_NAME


def go_live() -> str:
    return str(_env("STT_VOICEMEMOS_SINCE", GO_LIVE))


def place_names() -> set:
    """Place names Voice Memos uses as default titles here (STT_VOICEMEMOS_
    PLACES, comma-separated). A bare place name looks like a typed one-word
    title, so only a name on this list, or one that repeats with a count,
    reads as a default."""
    raw = _env("STT_VOICEMEMOS_PLACES", "") or ""
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


# ---------- state ----------

def load_state() -> dict:
    try:
        d = json.loads(state_path().read_text())
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("staged", {})
    d.setdefault("blocked", [])
    d.setdefault("last", {})
    return d


def _save_state(d: dict):
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def block(uuid: str) -> bool:
    """Never copy this memo again (tools/purge.py calls it). Returns True
    when the id was newly blocked."""
    if not uuid:
        return False
    d = load_state()
    if uuid in d["blocked"]:
        return False
    d["blocked"].append(uuid)
    _save_state(d)
    return True


# ---------- reading the database ----------

class SchemaChanged(Exception):
    pass


def _copy_db(src_dir: Path, into: Path) -> Path:
    """Copy the database and its WAL and shared-memory files into a private
    folder. Reading only: shutil.copyfile opens each source for reading."""
    dst = into / DB_NAME
    shutil.copyfile(src_dir / DB_NAME, dst)
    for ext in ("-wal", "-shm"):
        s = src_dir / (DB_NAME + ext)
        if s.exists():
            shutil.copyfile(s, into / (DB_NAME + ext))
    return dst


def check_schema(conn) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    if not cols:
        raise SchemaChanged(f"table {TABLE} is missing")
    missing = [c for c in NEEDED_COLUMNS if c not in cols]
    if missing:
        raise SchemaChanged("missing column(s): " + ", ".join(missing))


def read_rows(container: Path = None) -> list:
    """Every memo row as a dict. Raises SchemaChanged on a schema it does
    not understand, PermissionError when macOS denies the read, and
    FileNotFoundError when there is no library."""
    container = Path(container or container_dir())
    with tempfile.TemporaryDirectory(prefix="stt-vm-") as tmp:
        os.chmod(tmp, 0o700)
        db = _copy_db(container, Path(tmp))
        conn = sqlite3.connect(str(db))
        try:
            check_schema(conn)
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
            # ZEVICTIONDATE is set while a memo sits in Recently Deleted.
            # Optional: an older schema without it reads every memo as live.
            ev = "ZEVICTIONDATE" if "ZEVICTIONDATE" in cols else "NULL"
            rows = conn.execute(
                f"SELECT ZUNIQUEID, ZPATH, ZDATE, ZDURATION, ZENCRYPTEDTITLE, {ev} "
                f"FROM {TABLE}").fetchall()
        finally:
            conn.close()
    return [{"uuid": r[0], "path": r[1], "zdate": r[2], "duration": r[3],
             "title": r[4], "deleted": r[5] is not None} for r in rows]


def start_of(row: dict):
    """(aware local start, from_path). The file name holds the true local
    start. A duplicated memo carries another name, so its ZDATE is used and
    flagged as less certain. None when neither can be read."""
    m = _PATH_RE.match(os.path.basename(str(row.get("path") or "")))
    if m:
        try:
            t = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return t.astimezone(), True
        except ValueError:
            pass
    z = row.get("zdate")
    if isinstance(z, (int, float)):
        return (_CORE_DATA_EPOCH + timedelta(seconds=float(z))).astimezone(), False
    return None, False


# ---------- titles ----------

_COUNT = re.compile(r"^(.*\S)\s+(\d{1,3})$")


def classify_title(title: str, all_titles=()) -> tuple:
    """(typed, location_note).

    A default title is not a typed title: 'New Recording' names, timestamps
    and other generic names, a street address, and a place name on the
    owner's list, each with or without the count Voice Memos adds to a
    repeat. A street address or a place name is kept as the location note.

    A numbered name whose base is none of those stays typed, even when
    another memo carries the base name: people number their own series
    ('Cohort 3'). `all_titles` is kept for callers and no longer decides."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return False, None
    bare = t
    m = _COUNT.match(t)
    if m and (titles.is_address(m.group(1))
              or m.group(1).lower() in place_names()):
        bare = m.group(1)
    if titles.is_address(t) or titles.is_address(bare):
        return False, bare
    if bare.lower() in place_names():
        return False, bare
    if not titles.is_label(t):
        return False, None
    return True, None


def _safe(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s)).strip().lstrip(".")
    return re.sub(r"\s+", " ", s)[:100].strip()


# ---------- the sync ----------

def _write_state_last(d, status, detail=None, counts=None, app=None):
    d["last"] = {"at": datetime.now().isoformat(timespec="seconds"),
                 "status": status, "detail": detail, "counts": counts or {}}
    if app is not None:
        d["last"]["app_present"] = bool(app)
    _save_state(d)


def _app_present(container: Path) -> bool:
    """True when the Voice Memos group container (the parent of Recordings)
    exists on this Mac. A missing library there means the app moved or
    renamed its database. A denied stat counts as present."""
    try:
        return Path(container).parent.exists()
    except PermissionError:
        return True


def record_failure(exc: BaseException) -> None:
    """Record a sync that raised, so the doctor reports it. The batch calls
    this when sync() itself fails. A denial is recorded as `denied`."""
    try:
        d = load_state()
        if isinstance(exc, PermissionError):
            _write_state_last(d, "denied", exc.__class__.__name__)
        else:
            _write_state_last(d, "error", exc.__class__.__name__)
    except Exception:
        pass


def eligible(row: dict, now: float, since: str, container: Path):
    """(ok, reason, start, from_path)."""
    if not row.get("uuid") or not row.get("path"):
        return False, "no_file", None, False
    if row.get("deleted"):
        # the owner deleted it in Voice Memos (Recently Deleted): never import
        return False, "deleted", None, False
    start, from_path = start_of(row)
    if start is None:
        return False, "no_start", None, False
    if start.date().isoformat() < since:
        return False, "before_go_live", start, from_path
    try:
        dur = float(row.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur < MIN_SECONDS:
        return False, "short", start, from_path
    f = container / row["path"]
    try:
        st = f.stat()
    except FileNotFoundError:
        return False, "not_local", start, from_path
    if now - st.st_mtime < STABLE_SECONDS:
        return False, "in_progress", start, from_path
    if now - (start.timestamp() + dur) < STABLE_SECONDS:
        return False, "in_progress", start, from_path
    if st.st_size == 0 or st.st_blocks * 512 < st.st_size:
        return False, "not_local", start, from_path
    return True, "ok", start, from_path


def _staged_name(row, start, typed, stage: Path) -> str:
    stem = _safe(row.get("title")) if typed else ""
    stem = stem or "Voice Memo"
    # a typed title often carries its own 8-digit date. The name holds ONE
    # date, the true start: a trailing typed date is dropped, a date inside
    # the title is replaced in place, else the start is appended. So the
    # folder name and the stored date always agree.
    stem = dates.strip_stamp(stem)
    new = start.strftime("%m%d%Y")
    name = None
    for m in dates._DIGITS8.finditer(stem):
        if dates._run_date(m.group(1)) is not None:
            name = stem[:m.start()] + new + stem[m.end():]
            break
    name = name or f"{stem} {new}"
    cand, i = name, 2
    while ((stage / f"{cand}.m4a").exists()
           or (stage / f"{cand}{SIDECAR_SUFFIX}").exists()):
        cand = f"{name} ({i})"
        i += 1
    return cand + ".m4a"


def sync(now: float = None, container: Path = None, dry_run: bool = False) -> dict:
    """Copy every memo that passes the allow-list into the staging folder.
    Returns {status, staged: [names], counts: {reason: n}, detail}.

    status is one of: ok, disabled, missing (no library on this Mac),
    denied (macOS refused the read), schema (stops ingest), error."""
    now = time.time() if now is None else now
    container = Path(container or container_dir())
    state = load_state()
    if not enabled():
        if not dry_run:
            _write_state_last(state, "disabled")
        return {"status": "disabled", "staged": [], "counts": {}, "detail": None}
    # Path.exists() re-raises PermissionError (Python 3.12), and macOS can
    # refuse the stat itself inside the guarded container. Every probe here
    # is caught, so a denial is recorded for the doctor and never escapes.
    try:
        has_db = (container / DB_NAME).exists()
    except PermissionError:
        has_db = None
    if not has_db:
        status = "missing" if has_db is False else "denied"
        if status == "missing":
            try:
                container.stat()
                next(container.iterdir(), None)
            except PermissionError:
                status = "denied"
            except OSError:
                pass
        if not dry_run:
            _write_state_last(state, status, None, None,
                              app=_app_present(container))
        return {"status": status, "staged": [], "counts": {}, "detail": None}
    try:
        rows = read_rows(container)
    except SchemaChanged as e:
        if not dry_run:
            _write_state_last(state, "schema", str(e))
        return {"status": "schema", "staged": [], "counts": {}, "detail": str(e)}
    except PermissionError as e:
        if not dry_run:
            _write_state_last(state, "denied", e.__class__.__name__)
        return {"status": "denied", "staged": [], "counts": {}, "detail": None}
    except (OSError, sqlite3.Error) as e:
        # a copy taken while the app wrote can be torn: try again next run
        if not dry_run:
            _write_state_last(state, "error", e.__class__.__name__)
        return {"status": "error", "staged": [], "counts": {},
                "detail": e.__class__.__name__}

    since = go_live()
    stage = staging_dir()
    counts, staged = {}, []
    all_titles = [r.get("title") for r in rows]
    blocked = set(state["blocked"])
    for row in rows:
        if row.get("uuid") in blocked:
            counts["blocked"] = counts.get("blocked", 0) + 1
            continue
        if row.get("uuid") in state["staged"]:
            counts["already"] = counts.get("already", 0) + 1
            continue
        try:
            ok, why, start, from_path = eligible(row, now, since, container)
        except PermissionError:
            if not dry_run:
                _write_state_last(state, "denied", "file stat", counts)
            return {"status": "denied", "staged": staged, "counts": counts,
                    "detail": None}
        if not ok:
            counts[why] = counts.get(why, 0) + 1
            continue
        typed, note = classify_title(row.get("title"), [t for t in all_titles
                                                        if t != row.get("title")])
        if dry_run:
            counts["would_copy"] = counts.get("would_copy", 0) + 1
            continue
        stage.mkdir(parents=True, exist_ok=True)
        os.chmod(stage, 0o700)
        name = _staged_name(row, start, typed, stage)
        dst = stage / name
        side = {"vm_uuid": row["uuid"], "vm_path": row["path"],
                "duration": row.get("duration"),
                "title": row.get("title") if typed else None,
                "title_typed": bool(typed), "location_note": note}
        if from_path:
            side["recorded_at"] = start.isoformat(timespec="seconds")
        else:
            # a duplicated memo: its stored date is the best evidence, and
            # the panel shows it as uncertain
            side["date"] = start.date().isoformat()
            side["date_source"] = "mtime"
        tmp = dst.with_name("." + dst.name + ".part")
        try:
            shutil.copyfile(container / row["path"], tmp)
            st = (container / row["path"]).stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))
            os.chmod(tmp, 0o600)
            sp = dst.with_name(dst.stem + SIDECAR_SUFFIX)
            sp.write_text(json.dumps(side, indent=2))
            os.chmod(sp, 0o600)
            os.replace(tmp, dst)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            counts["copy_failed"] = counts.get("copy_failed", 0) + 1
            if isinstance(e, PermissionError):
                _write_state_last(state, "denied", "file read")
                return {"status": "denied", "staged": staged, "counts": counts,
                        "detail": None}
            continue
        state["staged"][row["uuid"]] = {
            "name": name, "at": datetime.now().isoformat(timespec="seconds")}
        staged.append(name)
        counts["copied"] = counts.get("copied", 0) + 1
    if not dry_run:
        _write_state_last(state, "ok", None, counts)
    return {"status": "ok", "staged": staged, "counts": counts, "detail": None}


def doctor_findings() -> list:
    """(kind, detail) pairs for the doctor, from the last sync's record.
    Counts and reasons only, never a title."""
    last = load_state().get("last") or {}
    st = last.get("status")
    out = []
    if st == "schema":
        out.append(("voicememos_schema",
                    "Voice Memos database changed; sync stopped: "
                    + str(last.get("detail") or "")))
    elif st == "denied":
        out.append(("voicememos_denied",
                    "macOS denied the Voice Memos read; grant Full Disk Access"))
    elif st == "error":
        out.append(("voicememos_error",
                    "the last Voice Memos read failed: " + str(last.get("detail") or "")))
    elif st == "missing" and enabled() and last.get("app_present"):
        # the app's group container is here but its database is not: an
        # update most likely moved or renamed it, and nothing is copied
        out.append(("voicememos_missing",
                    "the Voice Memos library database was not found; "
                    "sync copies nothing until the reader is updated"))
    counts = last.get("counts") or {}
    skipped = {k: v for k, v in counts.items()
               if k in ("short", "in_progress", "not_local", "no_start", "no_file",
                        "copy_failed")}
    if skipped and st == "ok":
        out.append(("voicememos_skipped",
                    ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))))
    return out

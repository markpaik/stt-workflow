"""Voice Memos sync: bring finished meeting memos from the Voice Memos
library on this Mac into the batch, with no action on the phone (unified plan
Phase 2 step 4).

Counsel approved the sync and the iCloud storage, and the owner turned it
on. It runs at the start of every batch run.

What it reads, and how:

- The container `~/Library/Group Containers/group.com.apple.VoiceMemos.shared
  /Recordings`. Nothing here ever writes a file in it, deletes a file from
  it, or opens a file in it for writing. The one change this module can make
  to the library is the optional delete after ingest (below), and the Voice
  Memos app makes that change itself, through a Shortcut. The database is
  read from a private COPY of
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

Delete after ingest (STT_VM_DELETE_AFTER_INGEST=1, off by default). Once per
batch run, after the descriptions are written, the batch moves each stored
memo to Voice Memos' Recently Deleted. It never erases a memo for good. A
memo is a candidate only when all of these hold:

- the sync staged it (its id is in the state's "staged" map);
- the batch stored its staged copy (run_batch notes it right after the
  manifest records it; a memo stored before that note existed is proved by
  its manifest record, whose meeting carries the memo's id);
- that meeting is still in the library, its json carries the memo's id, and
  its stored audio is there and runs within DURATION_SLACK_SEC of the memo's
  ZDURATION;
- the memo is still live in the database (not in Recently Deleted).

Before each delete, a fresh private copy of the database is read again. The
Shortcut runs only when the memo is still live, its title is not blank, and
no other memo, live or in Recently Deleted, has the same title (compared
without case or extra spaces). Otherwise the memo is recorded as "needs a
manual delete" with the reason. The Shortcut (STT_VM_DELETE_SHORTCUT, by
default "STT Delete Voice Memo", built once by the owner) finds the
recordings whose name is the title, deletes them only when it finds exactly
one, and outputs the count. The code runs it with `/usr/bin/shortcuts run`
and a timeout. No UI scripting, no AppleScript, and no write to the database.

After the Shortcut runs, a fresh copy is read again. Success means the
memo's row now has ZEVICTIONDATE set (Recently Deleted), and no other memo
that was live has left the live list, and no row left the database. Any
other outcome is recorded with the time and backs the memo off
(BACKOFF_SEC). If another memo left the live list, or a row left the
database, every delete stops (`delete_halt`) until the owner clears it with
tools/vm_delete.py --clear-halt. The real `shortcuts` binary acts on the
real library, so it runs only when the sync reads the real container.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
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
    for k, v in (("staged", {}), ("blocked", []), ("last", {}), ("delete", {})):
        if not isinstance(d.get(k), type(v)):
            d[k] = v
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


def read_rows(container: Path = None, require_eviction: bool = False) -> list:
    """Every memo row as a dict. Raises SchemaChanged on a schema it does
    not understand, PermissionError when macOS denies the read, and
    FileNotFoundError when there is no library. The delete step passes
    require_eviction: without ZEVICTIONDATE it cannot tell a memo in
    Recently Deleted from a live one, so it stops."""
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
            if "ZEVICTIONDATE" not in cols and require_eviction:
                raise SchemaChanged("missing column(s): ZEVICTIONDATE")
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
    """A memo title as a staged file name: no character a file name cannot
    hold, at most 100 characters, cut between words."""
    from . import names as _names
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s)).strip().lstrip(".")
    return _names.cut_words(re.sub(r"\s+", " ", s), 100).strip()


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
    # ZPATH is a name relative to the container. An absolute or ../ value
    # (a tampered database) must never make the sync copy an outside file.
    try:
        if not f.resolve().is_relative_to(Path(container).resolve()):
            return False, "outside_library", start, from_path
    except (OSError, RuntimeError, ValueError):
        return False, "outside_library", start, from_path
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
    # a typed title often carries its own date. The name holds ONE date,
    # the true start, in front ('YYYY-MM-DD <title>'): a typed stamp (either
    # form) or a date inside the title is removed, and the start goes in
    # front. So the folder name and the stored date always agree.
    name = dates.restamp(stem, start.date().isoformat())
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
                        "copy_failed", "outside_library")}
    if skipped and st == "ok":
        out.append(("voicememos_skipped",
                    ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))))
    return out + delete_doctor_findings()


# ---------- delete after ingest ----------
#
# The module docstring states the rules. The state file keeps one record per
# memo under "delete" (keyed by the memo's id):
#
#   running  the Shortcut was started for it (written BEFORE the run, so a
#            killed run is still known to have tried)
#   deleted  verified in Recently Deleted. Never touched again
#   owner    found in Recently Deleted before any try of ours (the owner
#            deleted it, or did the manual delete). Never touched again
#   gone     no longer in the database at all. Never touched again
#   failed   a try that did not verify; retried after `next_at`
#   manual   needs a manual delete in Voice Memos, with the reason. A title
#            problem is checked again on every pass (retry=True). A memo that
#            failed MAX_DELETE_TRIES times, or whose try stopped every delete,
#            waits for the owner (retry=False)
#
# `delete_halt` (top level) stops every delete until the owner clears it.

DELETE_SETTING = "STT_VM_DELETE_AFTER_INGEST"
SHORTCUT_SETTING = "STT_VM_DELETE_SHORTCUT"
SHORTCUT_DEFAULT = "STT Delete Voice Memo"
_REAL_SHORTCUTS = Path("/usr/bin/shortcuts")
# the binary that runs the Shortcut. Tests point this at a fake. The real one
# acts on the REAL Voice Memos library, so _sandbox_refusal lets it run only
# when the sync reads the real container.
SHORTCUTS_BIN = _REAL_SHORTCUTS
SHORTCUT_TIMEOUT_SEC = 60.0
LIST_TIMEOUT_SEC = 20.0
DURATION_SLACK_SEC = 2.0
# Voice Memos can write the database a moment after the Shortcut returns:
# the check reads a fresh copy up to VERIFY_POLLS times, VERIFY_WAIT_SEC apart
VERIFY_POLLS = 6
VERIFY_WAIT_SEC = 2.0
# one pass never holds the batch long: at most this many deletes, no new
# delete after PASS_BUDGET_SEC, and the pass ends at the first failure
MAX_DELETES_PER_PASS = 5
PASS_BUDGET_SEC = 180.0
# the wait after the 1st, 2nd and 3rd failed try. The next failure makes the
# memo a manual delete.
BACKOFF_SEC = (1800.0, 7200.0, 28800.0)
MAX_DELETE_TRIES = len(BACKOFF_SEC) + 1
DELETE_LOCK_NAME = "voicememos_delete.lock"
_DONE = ("deleted", "owner", "gone")
_TRIED = ("running", "failed")
PANEL_REMOVED_DAYS = 14


def delete_setting_problem():
    """Why STT_VM_DELETE_AFTER_INGEST cannot be read, or None. Only "0",
    "1" or empty are read. Any other value means off, because a delete
    must never follow from a typo."""
    raw = _env(DELETE_SETTING)
    v = str(raw or "").strip()
    if v in ("", "0", "1"):
        return None
    return f"{raw!r} is not 0 or 1; using 0, so memos stay in Voice Memos"


def delete_enabled() -> bool:
    """True only for STT_VM_DELETE_AFTER_INGEST=1. An unreadable value
    warns once and reads as off."""
    why = delete_setting_problem()
    if why:
        config._warn_once((DELETE_SETTING, why), f"{DELETE_SETTING}={why}")
        return False
    return str(_env(DELETE_SETTING) or "").strip() == "1"


def shortcut_name() -> str:
    return str(_env(SHORTCUT_SETTING) or "").strip() or SHORTCUT_DEFAULT


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_title(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().casefold()


def note_ingested(uuid, key, base, meeting_id=None, source="batch") -> bool:
    """Record that the batch stored this staged memo. run_batch calls it
    right after the manifest records the file, for a run into the library
    only. The delete step needs this proof, and keeps one it found in the
    manifest the same way (source="manifest"). Returns True when it was
    recorded."""
    if not uuid:
        return False
    d = load_state()
    ent = d["staged"].get(uuid)
    if not isinstance(ent, dict):
        return False
    ent["ingested"] = {"at": _now_iso(), "key": key, "base": base,
                       "meeting_id": meeting_id, "from": source}
    _save_state(d)
    return True


def _set_delete(uuid, rec: dict):
    d = load_state()
    d["delete"][uuid] = rec
    _save_state(d)


def _set_halt(reason: str, uuid=None):
    d = load_state()
    d["delete_halt"] = {"at": _now_iso(), "reason": reason, "uuid": uuid}
    _save_state(d)


def clear_halt():
    """Clear a stop (tools/vm_delete.py --clear-halt). Returns the record
    that was cleared, or None."""
    d = load_state()
    was = d.pop("delete_halt", None)
    if was is not None:
        _save_state(d)
    return was


def _write_delete_last(status, counts=None, detail=None):
    d = load_state()
    d["delete_last"] = {"at": _now_iso(), "status": status,
                        "counts": counts or {}, "detail": detail}
    _save_state(d)


def shortcut_status() -> tuple:
    """(ok, why): can the delete Shortcut run here? why is "ok",
    "no_shortcuts" (no shortcuts binary), "list_failed", or "missing" (the
    name is not in `shortcuts list`). Never runs the Shortcut itself."""
    b = Path(SHORTCUTS_BIN)
    try:
        if not (b.is_file() and os.access(b, os.X_OK)):
            return False, "no_shortcuts"
    except OSError:
        return False, "no_shortcuts"
    try:
        r = subprocess.run([str(b), "list"], capture_output=True, text=True,
                           timeout=LIST_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False, "list_failed"
    if r.returncode != 0:
        return False, "list_failed"
    names = {ln.strip() for ln in (r.stdout or "").splitlines()}
    return (True, "ok") if shortcut_name() in names else (False, "missing")


def _sandbox_refusal(container):
    """None when a delete may run, else why not. The real shortcuts binary
    acts on the real library, so it runs only against the real container:
    a demo or a test (STT_HOME, STT_VOICEMEMOS_DIR) reads a synthetic
    library, and a title there must never delete a real memo."""
    try:
        real_bin = Path(SHORTCUTS_BIN).resolve() == _REAL_SHORTCUTS.resolve()
    except OSError:
        real_bin = True
    if not real_bin:
        return None
    return None if _same_path_text(container, _REAL) else "sandbox"


def _same_path_text(a, b) -> bool:
    """Two paths compared as text, with no stat: a sandbox never touches
    the real container. APFS folds letter case, so the text does too."""
    def norm(p):
        return os.path.normpath(os.path.abspath(os.path.expanduser(str(p)))).casefold()
    return norm(a) == norm(b)


def _meeting_json(base, store):
    try:
        d = json.loads(config.meeting_file(base, ".json", store).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _in_store(json_path: Path, store: Path) -> bool:
    """True when json_path is <store>/<base>/<base>.json."""
    try:
        folder = json_path.parent
        if json_path.name != folder.name + ".json":
            return False
        a, b = folder.parent, Path(store)
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _ingest_proof(uuid, ent: dict, store: Path, man: dict):
    """The proof that the batch stored this memo, or None. The batch's own
    note first. Else the manifest record under the staged name, when the
    meeting it names is in the library and carries this memo's id (a memo
    stored before the note existed)."""
    ing = ent.get("ingested")
    if isinstance(ing, dict) and (ing.get("base") or ing.get("meeting_id")):
        return ing
    rec = (man.get("processed") or {}).get(ent.get("name"))
    if not isinstance(rec, dict):
        return None
    for o in rec.get("outputs") or []:
        jp = Path(str(o))
        if jp.suffix != ".json" or not _in_store(jp, store):
            continue
        d = _meeting_json(jp.parent.name, store)
        if d and d.get("vm_uuid") == uuid:
            return {"at": rec.get("processed_at"), "key": ent.get("name"),
                    "base": jp.parent.name, "meeting_id": d.get("meeting_id"),
                    "from": "manifest", "keep": True}
    return None


def _locate(uuid, proof: dict, store: Path):
    """The library meeting that holds this memo: its base, or None. Its
    stable id finds it after a rename. The json must carry the memo's id."""
    bases = []
    mid = proof.get("meeting_id")
    if mid:
        b = config.base_of(mid)
        if b:
            bases.append(b)
    if proof.get("base"):
        bases.append(proof["base"])
    for b in dict.fromkeys(bases):
        d = _meeting_json(b, store)
        if d and d.get("vm_uuid") == uuid:
            return b
    return None


def _stored_duration(path):
    """The stored audio's length in seconds from ffprobe, or None."""
    from . import provenance
    return provenance.probe_tags(path).get("duration")


def _title_problem(row: dict, rows: list):
    """Why the Shortcut must not run for this memo's title, or None. It
    finds recordings by name, so the title must be one only this memo
    holds. A memo in Recently Deleted counts too: a Shortcut that reached
    it could erase it for good."""
    t = _norm_title(row.get("title"))
    if not t:
        return "blank_title"
    same = [r for r in rows if r.get("uuid") != row.get("uuid")
            and _norm_title(r.get("title")) == t]
    if any(not r.get("deleted") for r in same):
        return "duplicate_title"
    if same:
        return "title_in_recently_deleted"
    return None


MANUAL_REASONS = {
    "blank_title": "the memo has no title, so the Shortcut cannot find it",
    "duplicate_title": "another live memo has the same title",
    "title_in_recently_deleted": "a memo in Recently Deleted has the same title",
    "tries": "the Shortcut did not move it after several tries",
    "halt": "deletes stopped after its try; check it in Voice Memos",
}


def _evaluate(uuid, ent, rec, row, store, man, now):
    """One staged memo against the candidate rules, with no write. Returns
    a plan item {uuid, name, title, state, reason, base, detail, proof}.

    state: done (handled before), late (we ran the Shortcut and the memo is
    in Recently Deleted now), owner (in Recently Deleted with no try of
    ours), gone (not in the database), manual (waiting for the owner),
    backoff, waiting (a candidate rule fails: reason says which), or
    candidate."""
    rec = rec if isinstance(rec, dict) else {}
    item = {"uuid": uuid, "name": ent.get("name"), "title": None, "state": None,
            "reason": None, "base": None, "detail": None, "proof": None}
    status = rec.get("status")
    if status in _DONE:
        item.update(state="done", reason=status, title=rec.get("title"),
                    base=rec.get("base"))
        return item
    if row is None:
        item.update(state="gone", title=rec.get("title"))
        return item
    item["title"] = row.get("title")
    if row.get("deleted"):
        item["state"] = "late" if status in _TRIED else "owner"
        item["base"] = rec.get("base")
        return item
    if status == "manual" and not rec.get("retry"):
        item.update(state="manual", reason=rec.get("reason"), detail=rec.get("detail"))
        return item
    if status in _TRIED and now < float(rec.get("next_at") or 0):
        item.update(state="backoff", reason=rec.get("reason"),
                    detail=datetime.fromtimestamp(float(rec["next_at"]))
                    .isoformat(timespec="minutes"))
        return item
    proof = _ingest_proof(uuid, ent, store, man)
    if proof is None:
        item.update(state="waiting", reason="not_ingested")
        return item
    item["proof"] = proof
    base = _locate(uuid, proof, store)
    if base is None:
        item.update(state="waiting", reason="meeting_gone")
        return item
    item["base"] = base
    audio = config.meeting_audio(base, store)
    if audio is None:
        item.update(state="waiting", reason="no_audio")
        return item
    try:
        want = float(row.get("duration"))
    except (TypeError, ValueError):
        want = None
    got = _stored_duration(audio)
    if want is None or got is None or abs(float(got) - want) > DURATION_SLACK_SEC:
        item.update(state="waiting", reason="duration_mismatch",
                    detail=f"stored {got if got is None else round(got, 1)} s, "
                           f"memo {want if want is None else round(want, 1)} s")
        return item
    item["state"] = "candidate"
    return item


def _run_shortcut(title: str) -> dict:
    """Run the delete Shortcut once with the title as its input file.
    Returns {outcome, output, detail}: outcome is ok (it output "1"),
    output (it output something else), exit (non-zero exit), timeout, or
    no_shortcuts (the binary could not start)."""
    with tempfile.TemporaryDirectory(prefix="stt-vmdel-") as tmp:
        os.chmod(tmp, 0o700)
        inp, outp = Path(tmp) / "title.txt", Path(tmp) / "count.txt"
        inp.write_text(str(title), encoding="utf-8")
        os.chmod(inp, 0o600)
        cmd = [str(SHORTCUTS_BIN), "run", shortcut_name(),
               "--input-path", str(inp), "--output-path", str(outp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SHORTCUT_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"outcome": "timeout", "output": "",
                    "detail": f"no answer in {SHORTCUT_TIMEOUT_SEC:g} s"}
        except OSError as e:
            return {"outcome": "no_shortcuts", "output": "", "detail": e.__class__.__name__}
        try:
            text = outp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    text = text.strip().lstrip("\ufeff").strip()
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return {"outcome": "exit", "output": text[:40],
                "detail": f"exit {r.returncode}" + (f": {err[0][:200]}" if err else "")}
    if text != "1":
        return {"outcome": "output", "output": text[:40],
                "detail": f"the Shortcut output {text[:40]!r}, not '1'"}
    return {"outcome": "ok", "output": "1", "detail": ""}


def _verify(uuid, before: list, container, wait: bool):
    """(verdict, detail) after a Shortcut run, from fresh copies of the
    database. verdict: deleted, not_deleted, unreadable, or halt (another
    live memo left the live list, or a row left the database)."""
    before_live = {r["uuid"] for r in before if not r.get("deleted")}
    before_all = {r["uuid"] for r in before}
    after = None
    for i in range(VERIFY_POLLS if wait else 1):
        if i:
            time.sleep(VERIFY_WAIT_SEC)
        try:
            after = read_rows(container, require_eviction=True)
        except (SchemaChanged, OSError, sqlite3.Error):
            continue
        ours = next((r for r in after if r["uuid"] == uuid), None)
        if ours is not None and ours.get("deleted"):
            break
    if after is None:
        return "unreadable", "the database could not be read after the run"
    by = {r["uuid"]: r for r in after}
    after_live = {u for u, r in by.items() if not r.get("deleted")}
    left_live = sorted((before_live - {uuid}) - after_live)
    erased = sorted(before_all - set(by))
    if erased or left_live:
        parts = []
        if uuid in erased:
            parts.append("this memo left the database instead of moving to "
                         "Recently Deleted")
        others = [u for u in erased if u != uuid]
        if others:
            parts.append(f"{len(others)} other memo(s) left the database")
        if left_live:
            parts.append(f"{len(left_live)} other live memo(s) left the live list")
        return "halt", "; ".join(parts)
    if by[uuid].get("deleted"):
        return "deleted", ""
    return "not_deleted", "the memo is still live after the run"


class _PassLock:
    """One delete pass at a time across processes (the batch and
    tools/vm_delete.py --apply). Non-blocking: a held lock skips the pass."""

    def __enter__(self):
        import fcntl
        p = config.PROJECT_DIR / DELETE_LOCK_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(p, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
        return self.fh is not None

    def __exit__(self, *exc):
        if self.fh is not None:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


def _failed_record(tries: int, reason: str, detail: str, title, now):
    if tries >= MAX_DELETE_TRIES:
        return {"status": "manual", "retry": False, "reason": "tries",
                "detail": f"{reason}: {detail}", "tries": tries, "at": _now_iso(),
                "title": title}
    wait = BACKOFF_SEC[min(tries, len(BACKOFF_SEC)) - 1]
    return {"status": "failed", "reason": reason, "detail": detail, "tries": tries,
            "at": _now_iso(), "next_at": now + wait, "title": title}


def delete_pass(store=None, dry_run=False, force=False, now=None,
                log=None, warn=None) -> dict:
    """One delete-after-ingest pass. The batch calls it once per run, after
    the descriptions. tools/vm_delete.py calls it with dry_run (no write,
    and the Shortcut never runs) or force (one pass with the setting off).

    Returns {status, plan, deleted, manual, failed, detail}. status: off,
    not_library, halted, busy (another pass holds the lock), idle (no memo
    waits), sandbox, schema, denied, error, no_shortcuts, missing (no such
    Shortcut), list_failed, or ok. Never raises for a missing Shortcut, a
    timeout, or a bad output: those are recorded."""
    log = log or (lambda s: None)
    warn = warn or log
    now = time.time() if now is None else now
    out = {"status": None, "plan": [], "deleted": [], "manual": [], "failed": [],
           "detail": None}
    if not force and not (enabled() and delete_enabled()):
        out["status"] = "off"
        return out
    store = Path(store) if store else config.meetings_dir()
    if not config.is_live_store(store):
        out["status"] = "not_library"
        return out
    if dry_run:
        return _pass(out, store, True, now, log, warn)
    with _PassLock() as got:
        if not got:
            out["status"] = "busy"
            return out
        return _pass(out, store, False, now, log, warn)


def _pass(out, store, dry_run, now, log, warn):
    """The body of delete_pass. A dry run reads, judges every staged memo,
    and returns the plan with what blocks the pass (out["blocked"]). It
    writes nothing and never runs the Shortcut."""
    t0 = time.monotonic()
    state = load_state()
    out["blocked"] = []
    halt = state.get("delete_halt")
    if isinstance(halt, dict):
        out["detail"] = str(halt.get("reason") or "")
        if not dry_run:
            out["status"] = "halted"
            warn("ERROR: Voice Memos deletes are STOPPED: "
                 f"{out['detail']}. Check Voice Memos and its Recently Deleted, "
                 "then run ./run.sh py tools/vm_delete.py --clear-halt")
            return out
        out["blocked"].append("halted")
    pending = [(u, e) for u, e in state["staged"].items() if isinstance(e, dict)
               and (state["delete"].get(u) or {}).get("status") not in _DONE]
    if not pending and not dry_run:
        out["status"] = "idle"
        return out
    container = container_dir()
    why = _sandbox_refusal(container)
    if why:
        if not dry_run:
            out.update(status=why, detail="the sync reads a library other than "
                       "this Mac's own, so the real Shortcut must not run")
            _write_delete_last(why, detail=out["detail"])
            return out
        out["blocked"].append(why)
    try:
        rows = read_rows(container, require_eviction=True)
    except SchemaChanged as e:
        out.update(status="schema", detail=str(e))
    except PermissionError:
        out.update(status="denied", detail="PermissionError")
    except (OSError, sqlite3.Error) as e:
        out.update(status="error", detail=e.__class__.__name__)
    if out["status"]:
        if not dry_run:
            _write_delete_last(out["status"], detail=out["detail"])
        return out
    from . import manifest
    man = manifest.load()
    by = {r["uuid"]: r for r in rows}
    entries = [(u, e) for u, e in state["staged"].items() if isinstance(e, dict)] \
        if dry_run else pending
    entries.sort(key=lambda kv: str(kv[1].get("at") or ""))
    plan = []
    for u, ent in entries:
        item = _evaluate(u, ent, state["delete"].get(u), by.get(u), store, man, now)
        if item["state"] == "candidate":
            why = _title_problem(by[u], rows)
            item["would"] = f"manual:{why}" if why else "delete"
        plan.append(item)
    out["plan"] = plan
    if dry_run:
        out["status"] = "ok"
        return out

    # resolutions: no Shortcut runs for these
    for item in plan:
        u, st = item["uuid"], item["state"]
        prev = state["delete"].get(u) or {}
        if st == "late":
            rec = {"status": "deleted", "at": _now_iso(), "title": item["title"],
                   "base": prev.get("base") or item["base"], "tries": prev.get("tries"),
                   "late": True}
            _set_delete(u, rec)
            out["deleted"].append(rec)
            log(f"Voice Memos: moved \"{item['title']}\" to Recently Deleted "
                "(confirmed on a later pass)")
        elif st == "owner":
            _set_delete(u, {"status": "owner", "at": _now_iso(), "title": item["title"],
                            "was": prev.get("status")})
            if prev.get("status") == "manual":
                log(f"Voice Memos: \"{item['title']}\" is in Recently Deleted now; "
                    "no manual delete is needed")
        elif st == "gone":
            _set_delete(u, {"status": "gone", "at": _now_iso(),
                            "title": item["title"], "was": prev.get("status")})
        elif st == "waiting" and prev.get("status") == "manual" and prev.get("retry"):
            # a title problem no longer matters: a candidate rule fails now
            # (the meeting left the library, say), so the memo must stay
            _forget_delete(u)
        elif (item.get("proof") or {}).get("keep"):
            # keep a proof found in the manifest: a later memo staged under
            # the same name would replace the record it came from
            p = item["proof"]
            note_ingested(u, p.get("key"), p.get("base"), p.get("meeting_id"),
                          source="manifest")

    counts = {}
    for item in plan:
        k = item["state"] if item["state"] != "waiting" else f"waiting:{item['reason']}"
        counts[k] = counts.get(k, 0) + 1
    shortcut_ok = None
    n = 0
    status = "ok"
    for item in [i for i in plan if i["state"] == "candidate"]:
        u = item["uuid"]
        prev = load_state()["delete"].get(u) or {}
        if item["would"] != "delete":
            # a title problem in the pass's own read: no delete, so no
            # fresh read is needed to decide it
            _manual_title(u, prev, item["would"].split(":", 1)[1], item["title"],
                          item["base"], out, warn)
            continue
        if n >= MAX_DELETES_PER_PASS or time.monotonic() - t0 > PASS_BUDGET_SEC:
            continue
        # a fresh copy right before the delete: the pass's read can be old
        try:
            fresh = read_rows(container, require_eviction=True)
        except (SchemaChanged, OSError, sqlite3.Error) as e:
            status = "error"
            out["detail"] = e.__class__.__name__
            break
        row = next((r for r in fresh if r["uuid"] == u), None)
        if row is None:
            _set_delete(u, {"status": "gone", "at": _now_iso(), "title": item["title"],
                            "was": prev.get("status")})
            continue
        if row.get("deleted"):
            _set_delete(u, {"status": "owner", "at": _now_iso(), "title": row.get("title"),
                            "was": prev.get("status")})
            continue
        title = row.get("title")
        why = _title_problem(row, fresh)
        if why:
            _manual_title(u, prev, why, title, item["base"], out, warn)
            continue
        if shortcut_ok is None:
            shortcut_ok, why = shortcut_status()
            if not shortcut_ok:
                status = why
                out["detail"] = (f"the Shortcut \"{shortcut_name()}\" is not installed"
                                 if why == "missing" else why)
                warn(f"Voice Memos: no delete ran: {out['detail']}")
                break
        tries = int(prev.get("tries") or 0) + 1
        # written BEFORE the run: a run killed mid-Shortcut still counts as
        # a try, and waits out the first backoff
        _set_delete(u, {"status": "running", "at": _now_iso(), "tries": tries,
                        "title": title, "base": item["base"],
                        "next_at": now + BACKOFF_SEC[0]})
        res = _run_shortcut(title)
        verdict, vdetail = _verify(u, fresh, container,
                                   wait=res["outcome"] in ("ok", "timeout"))
        if verdict == "halt":
            _set_halt(vdetail, u)
            _set_delete(u, {"status": "manual", "retry": False, "reason": "halt",
                            "detail": vdetail, "tries": tries, "at": _now_iso(),
                            "title": title, "base": item["base"]})
            warn(f"ERROR: Voice Memos deletes are STOPPED after \"{title}\": "
                 f"{vdetail}. Check Voice Memos and its Recently Deleted now, then "
                 "run ./run.sh py tools/vm_delete.py --clear-halt")
            status = "halted"
            out["detail"] = vdetail
            break
        if verdict == "deleted":
            rec = {"status": "deleted", "at": _now_iso(), "title": title,
                   "base": item["base"], "tries": tries}
            if res["outcome"] != "ok":
                rec["note"] = res["detail"]
            _set_delete(u, rec)
            out["deleted"].append(rec)
            n += 1
            log(f"Voice Memos: moved \"{title}\" to Recently Deleted "
                f"(stored as {item['base']})")
            continue
        if res["outcome"] == "no_shortcuts":
            # the binary could not start: not this memo's failure
            if prev:
                _set_delete(u, prev)
            else:
                _forget_delete(u)
            status = "no_shortcuts"
            out["detail"] = res["detail"]
            warn(f"Voice Memos: no delete ran: {SHORTCUTS_BIN} did not start "
                 f"({res['detail']})")
            break
        ok_run = res["outcome"] == "ok"
        reason = f"verify_{verdict}" if ok_run else res["outcome"]
        detail = vdetail if ok_run else res["detail"]
        rec = _failed_record(tries, reason, detail, title, now)
        rec["base"] = item["base"]
        _set_delete(u, rec)
        out["failed"].append(rec)
        nxt = ("it now needs a manual delete" if rec["status"] == "manual" else
               "next try after " + datetime.fromtimestamp(rec["next_at"])
               .isoformat(timespec="minutes"))
        warn(f"Voice Memos: \"{title}\" was not moved to Recently Deleted: "
             f"{detail} ({nxt})")
        # one failure ends the pass: the next memo would most likely fail
        # the same way, and the batch must not wait on it
        break
    counts.update({"deleted": len(out["deleted"]), "manual_new": len(out["manual"]),
                   "failed": len(out["failed"])})
    out["status"] = status
    _write_delete_last(status, counts, out["detail"])
    return out


def _manual_title(u, prev, why, title, base, out, warn):
    rec = {"status": "manual", "retry": True, "reason": why,
           "detail": MANUAL_REASONS[why], "at": _now_iso(), "title": title,
           "base": base}
    if prev.get("status") != "manual" or prev.get("reason") != why:
        warn(f"Voice Memos: \"{title}\" needs a manual delete: {MANUAL_REASONS[why]}")
        out["manual"].append(rec)
    else:
        rec["at"] = prev.get("at") or rec["at"]
    _set_delete(u, rec)


def _forget_delete(uuid):
    d = load_state()
    if d["delete"].pop(uuid, None) is not None:
        _save_state(d)


def delete_doctor_findings() -> list:
    """(kind, detail) pairs for the doctor: a stop, a missing Shortcut
    while the setting is on, and memos waiting for a manual delete.
    Counts and reasons only, never a title."""
    out = []
    st = load_state()
    halt = st.get("delete_halt")
    if isinstance(halt, dict):
        out.append(("voicememos_delete_halted",
                    "Voice Memos deletes are stopped: " + str(halt.get("reason") or "")
                    + ". Check Voice Memos and its Recently Deleted, then run "
                      "tools/vm_delete.py --clear-halt"))
    on = enabled() and delete_enabled()
    if on:
        ok, why = shortcut_status()
        if not ok:
            what = {"missing": f"the Shortcut \"{shortcut_name()}\" is not in "
                               "`shortcuts list`",
                    "no_shortcuts": f"{SHORTCUTS_BIN} is not here",
                    "list_failed": "`shortcuts list` failed"}.get(why, why)
            out.append(("voicememos_shortcut_missing",
                        f"{DELETE_SETTING} is on, but {what}; memos stay in "
                        "Voice Memos until it is installed (README: Voice Memos)"))
        manual = [r for r in st["delete"].values()
                  if isinstance(r, dict) and r.get("status") == "manual"]
        if manual:
            c = {}
            for r in manual:
                c[r.get("reason") or "?"] = c.get(r.get("reason") or "?", 0) + 1
            out.append(("voicememos_manual_delete",
                        f"{len(manual)} memo(s) to delete by hand in Voice Memos ("
                        + ", ".join(f"{k} {v}" for k, v in sorted(c.items()))
                        + "); tools/vm_delete.py --dry-run lists them"))
    return out


def delete_panel_state() -> dict:
    """For the panel: is the delete on, the memos it moved in the last
    PANEL_REMOVED_DAYS (newest first, at most 8), how many wait for a
    manual delete, and whether deletes are stopped."""
    st = load_state()
    cutoff = (datetime.now() - timedelta(days=PANEL_REMOVED_DAYS)).isoformat()
    removed = sorted(({"title": r.get("title"), "at": r.get("at"), "base": r.get("base")}
                      for r in st["delete"].values()
                      if isinstance(r, dict) and r.get("status") == "deleted"
                      and str(r.get("at") or "") >= cutoff),
                     key=lambda r: str(r["at"]), reverse=True)[:8]
    manual = sum(1 for r in st["delete"].values()
                 if isinstance(r, dict) and r.get("status") == "manual")
    return {"delete": bool(enabled() and delete_enabled()), "removed": removed,
            "manual": manual, "halted": isinstance(st.get("delete_halt"), dict)}

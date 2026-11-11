"""Local control panel for the STT pipeline.

A tiny stdlib HTTP server bound to 127.0.0.1 only, started as a thread inside the
menu-bar app. Serves one polished HTML page + a JSON API. All actions shell out to
the same entrypoints the CLI uses, so the panel can never bypass the pipeline's
locks and safety rails.
"""
import json
import math
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gui import jobqueue
from stt import (config, control, dates, dupes, export, groups, identify, jobs,
                 manifest, names, output, queue_order, rates, recorder, review,
                 search, series, status, titles, unknowns)
from stt.errors import UserError

PORT = 8737
# A voice with less talk than this hides behind a count instead of asking to
# be named (plan Phase 1 step 4): most of these are a cough, a greeting, or a
# one-line visitor, and naming them is not worth a person's time.
VOICE_ASK_MIN_SECS = 60.0
# The one relabel worker for this panel process, started by start_server(). None
# means "no queue in this process" -- the CLI-only headless mode and the tests
# that drive Handler directly -- and every caller falls back to the spawn path
# it used before the queue existed. See gui/jobqueue.py.
RELABEL_QUEUE = None
# The batch agent's launchd plist. Sandboxed like every other state path: under
# an STT_HOME override (demo/QA homes, tools/demo_seed.py) it resolves to
# $STT_HOME/LaunchAgents/... so /api/schedule and /api/automation from a QA
# server can never mutate the REAL machine's agent (which is exactly what
# happened on 2026-07-12). Unset, it is byte-identical to what it always was.
AGENT = Path(config._home_default(
    "LaunchAgents/com.stt-workflow.batch.plist",
    Path.home() / "Library/LaunchAgents/com.stt-workflow.batch.plist"))
LABEL = "com.stt-workflow.batch"
RUN_SH = config.CODE_DIR / "run.sh"     # code, never the data home

_SAME_ORIGIN = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)")


def _origin_allowed(headers) -> bool:
    """This panel is unauthenticated and binds 127.0.0.1, but that alone
    doesn't stop a malicious page open in the same browser from driving it —
    any site can fetch()/POST to localhost with no CORS preflight blocking a
    simple request. A direct request (curl, CLI, typing the URL) carries no
    Origin/Referer at all and is allowed; a request carrying either header
    from anywhere else is rejected."""
    for h in ("Origin", "Referer"):
        v = headers.get(h)
        if v and not _SAME_ORIGIN.match(v):
            return False
    return True


_LOOPBACK_HOST = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", re.I)


def _host_allowed(headers) -> bool:
    """The Host header must name this machine's loopback. A page on an
    attacker domain whose DNS rebinds to 127.0.0.1 is same-origin to the
    panel, and its same-origin GET (with no-referrer) carries no Origin or
    Referer, so _origin_allowed lets it through. The browser still sends
    the attacker's name as Host. A request with no Host (HTTP/1.0) is not
    a browser and is allowed. Every Host header must pass, and more than
    one is refused: a proxy or server that reads the last copy must never
    see a name this check did not read."""
    vs = headers.get_all("Host") if hasattr(headers, "get_all") else None
    if vs is None:
        v = headers.get("Host")
        vs = [] if v is None else [v]
    if len(vs) > 1:
        return False
    return all(_LOOPBACK_HOST.match(v.strip()) for v in vs)


def _known_base(base) -> bool:
    """A meeting basename must name a real transcript on disk — the only
    defense against a path-traversal `base` like '../../../etc/passwd' reaching
    a file path. Deliberately a plain membership test (not path resolution):
    glob() can only ever return names of files actually inside meetings_dir."""
    return (isinstance(base, str) and bool(base)
            and base in set(config.meeting_bases()))

ASR_CHOICES = [
    {"id": "parakeet", "label": "Parakeet TDT 0.6B v3", "note": "fastest · local · keeps fillers"},
    # cloud engines: shown only once their key is set; words are transcribed
    # off-device, but diarization + speaker naming stay local — and strict
    # mode ALWAYS forces the local engine (sensitive audio never uploads)
    {"id": "cloud:scribe", "label": "ElevenLabs Scribe ☁", "cloud": "scribe",
     "note": "audio uploads to ElevenLabs · strict mode stays local"},
    {"id": "cloud:openai", "label": "OpenAI Whisper API ☁", "cloud": "openai",
     "note": "audio uploads to OpenAI · strict mode stays local"},
    {"id": "cloud:voxtral", "label": "Mistral Voxtral ☁", "cloud": "voxtral",
     "note": "audio uploads to Mistral · strict mode stays local"},
]
MODEL_REPOS = {
    "Parakeet TDT 0.6B v3": "mlx-community/parakeet-tdt-0.6b-v3",
    "pyannote community-1": "pyannote/speaker-diarization-community-1",
    "Qwen3-8B (rename/summary)": "mlx-community/Qwen3-8B-4bit",
}


# ---------- helpers ----------

def _env_file_get():
    """(settings dict, raw lines). Parsing lives in config._env_file — one parser
    for the whole app; the raw lines are only needed by the comment-preserving
    writer below."""
    envp = config.PROJECT_DIR / "stt.env"
    lines = envp.read_text().splitlines() if envp.exists() else []
    return config._env_file(), lines


_env_lock = threading.Lock()


def _env_file_set(updates: dict, remove=()):
    # A value is written into stt.env as one KEY=VALUE line. A newline or
    # carriage return inside it therefore INJECTS a whole extra STT_*=... line
    # into the file that stores the API keys -- and config re-reads that file on
    # every call, so the injected line takes effect on the next poll with no
    # restart. Refuse the write outright: no field this endpoint family accepts
    # (a model id, a person's name, an API key) has any use for a line break.
    for k, v in updates.items():
        if any(c in str(k) or c in str(v) for c in ("\n", "\r")):
            raise ValueError("settings values cannot contain line breaks")
    # serialize the read-modify-write: settings POSTs each land on their own
    # ThreadingHTTPServer thread, so two concurrent saves would otherwise read
    # the same snapshot and the later write would silently drop the earlier key
    with _env_lock:
        envp = config.PROJECT_DIR / "stt.env"
        kv, lines = _env_file_get()
        out = []
        seen = set()
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k in remove:  # drop the line entirely (clear a saved key)
                    continue
                if k in updates:
                    out.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            out.append(ln)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        envp.write_text("\n".join(out) + "\n")
        envp.chmod(0o600)


def current_model():
    return rates.current_asr_key()  # single source of truth for the model key


def _verify_engine():
    """The local engine a verify run would compare with under the current
    engine setting, or None (stt.verify.secondary_engine). With Parakeet,
    the only local engine, it is None."""
    from stt import verify
    return verify.secondary_engine(current_model())


def read_schedule():
    """The agent's automation triggers, straight from the plist: `watch`
    (event-triggered runs when a file lands) and `nightly` (the scheduled
    run) are INDEPENDENT — either can be off while the other stays on."""
    try:
        d = plistlib.loads(AGENT.read_bytes())
        sci = d.get("StartCalendarInterval") or {}
        return {"hour": sci.get("Hour"), "minute": sci.get("Minute", 0) or 0,
                "nightly": bool(sci), "watch": bool(d.get("WatchPaths")),
                "installed": True}
    except Exception:
        return {"hour": None, "minute": 0, "nightly": False, "watch": False,
                "installed": AGENT.exists()}


def _agent_reload():
    """launchd caches plists — every edit must bootout+bootstrap to apply.
    Under STT_HOME the plist is a sandbox copy launchd has never heard of:
    write the file only, never launchctl — a QA server registering (or worse,
    booting out) the operator's real agent is the incident this guard exists
    to prevent."""
    if config.STT_HOME:
        return
    import os
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], capture_output=True)


def _watch_paths():
    """Both folders the batch agent watches: the primary source AND the local
    recordings staging folder (the meeting recorder's output). Every place that
    rewrites WatchPaths uses this, so changing the source folder or toggling the
    watch never silently drops the recorder's trigger."""
    out = [str(config.source_dir()), str(config.recordings_dir())]
    # the Voice Memos library (read, never written): setup.sh watches it, so
    # a rewrite here keeps it, or the panel's Watch switch would drop it
    try:
        from stt import voicememos
        if voicememos.enabled():
            vm = str(voicememos.container_dir())
            if vm not in out:
                out.append(vm)
    except Exception:
        pass
    return out


# a run every 10 minutes while Watch is on: a Voice Memos memo becomes
# eligible 5 minutes after it ends, and no folder event fires then
WATCH_INTERVAL = 600


def write_automation(watch=None, nightly=None, hour=None, minute=None) -> dict:
    """Rewrite the agent's triggers; None leaves that trigger as it is.
    Disabling nightly remembers its time (stt.env) so re-enabling restores
    it; enabling watch stamps the CURRENT source folder, healing a stale
    WatchPaths after a folder change. Login catch-up (RunAtLoad) stays on
    only while some automatic trigger exists — 'manual' means manual."""
    if not AGENT.exists():
        return {"ok": False,
                "error": "automation agent not installed — run ./setup.sh install-agent"}
    d = plistlib.loads(AGENT.read_bytes())
    if watch is not None:
        if watch:
            d["WatchPaths"] = _watch_paths()
            d["StartInterval"] = WATCH_INTERVAL
        else:
            d.pop("WatchPaths", None)
            d.pop("StartInterval", None)   # 'manual' means manual
    if hour is not None:  # setting a time implies the nightly run is wanted
        nightly = True
    if nightly is not None:
        if nightly:
            if hour is None:
                saved = _env_file_get()[0].get("STT_SCHEDULE_SAVED", "2:0")
                try:
                    hour, minute = (int(x) for x in saved.split(":"))
                except ValueError:
                    hour, minute = 2, 0
            d["StartCalendarInterval"] = {"Hour": int(hour), "Minute": int(minute or 0)}
        else:
            sci = d.pop("StartCalendarInterval", None)
            if sci:
                _env_file_set({"STT_SCHEDULE_SAVED":
                               f"{sci.get('Hour', 2)}:{sci.get('Minute', 0)}"})
    d["RunAtLoad"] = bool(d.get("WatchPaths")) or "StartCalendarInterval" in d
    AGENT.write_bytes(plistlib.dumps(d))
    _agent_reload()
    out = {"ok": True, **read_schedule()}
    if config.STT_HOME:  # additive: unset behavior stays byte-identical
        out["note"] = "sandboxed by STT_HOME: plist written, launchctl skipped"
    return out


def write_schedule(hh, mm):
    write_automation(hour=hh, minute=mm)


def _est_duration(p: Path):
    """Audio seconds for a queued file: stt.queue_order.duration, the same
    helper run_batch orders its picks by (exact when local, size-based for
    dataless iCloud files, cached by path, mtime, size and presence)."""
    return queue_order.duration(p)


_meet_cache = {}
_jobs_kicked = {"at": 0.0}
# the batch _kick_jobs spawned last. Until it holds batch.lock the process
# table shows no batch, so a burst of Run clicks (each one clears the
# cooldown) spawned one batch per click: 120 of them, all but one exiting
# at once. While the last spawn is still starting, it takes the queue.
_kicked_proc = {"proc": None, "at": 0.0}
_SPAWN_STARTUP_SEC = 60.0


def _kick_jobs():
    """Start the head queued job if nothing is running. The job is removed from
    the queue only by the run_batch that wins the lock for it, so a lost race
    just leaves it queued for the next kick (cooldown stops kick storms)."""
    import time as _time
    nxt = jobs.items()
    if not nxt or control.snapshot()["pids"]:
        return
    if control.stopping_recently():
        # a Stop just happened — do NOT respawn a job a killed run may have
        # re-queued from its SIGTERM handler; that was the "Starting… ↔
        # Stopped" flicker. An explicit new run clears the flag (see /api/run).
        return
    if _time.monotonic() - _jobs_kicked["at"] < 15:
        return
    p = _kicked_proc["proc"]
    if (p is not None and p.poll() is None
            and _time.monotonic() - _kicked_proc["at"] < _SPAWN_STARTUP_SEC):
        return      # still starting up: it claims the head job itself
    _jobs_kicked["at"] = _time.monotonic()
    _kicked_proc.update(proc=_spawn(jobs.spawn_args(nxt[0])), at=_time.monotonic())


# ---------- duplicate transcripts: computed OFF the poll thread ----------
# Comparing every transcript against every other one reads the whole library.
# That is milliseconds per meeting, but on a first run over hundreds of them it
# is seconds — far too long to sit inside a 2s status poll. So the poll only ever
# READS the cache (dupes.cached_pairs) and, when the library has changed since
# the cached answer, kicks one worker to recompute. The worker is exactly one at
# a time and swallows its own errors: a duplicate scan must never take the panel
# down with it.
_dupe_worker = {"busy": False}
# guards the busy flag's check-then-set: ThreadingHTTPServer polls concurrently,
# and the eligibility checks below do file I/O, plenty of room for two polls to
# both pass a bare flag test and start two full-library scans
_dupe_lock = threading.Lock()


def _dupe_refresh():
    """Recompute the duplicate-transcript pairs. Synchronous: the thread below
    calls it, and so do the tests."""
    try:
        dupes.similar_meetings(dest_dir=config.meetings_dir())
    except Exception:
        pass


def _kick_dupe_scan():
    with _dupe_lock:
        if _dupe_worker["busy"]:
            return
        try:
            if len(config.meeting_bases(config.meetings_dir())) < 2:
                return      # nothing can pair up: never even open the cache
            if dupes.cache_is_current(config.meetings_dir()):
                return
        except Exception:
            return
        _dupe_worker["busy"] = True

    def run():
        try:
            _dupe_refresh()
        finally:
            with _dupe_lock:
                _dupe_worker["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def _force_dupe_scan() -> bool:
    """The drawer's Rescan: recompute NOW, but through the same single-flight
    guard as the poll's kick -- an unguarded path here was how two scans could
    run at once. Returns False when one is already running (the caller answers
    with the current cache and scanning=True; the drawer keeps polling)."""
    with _dupe_lock:
        if _dupe_worker["busy"]:
            return False
        _dupe_worker["busy"] = True
    try:
        _dupe_refresh()
    finally:
        with _dupe_lock:
            _dupe_worker["busy"] = False
    return True


def _dupe_pair_rows(dst_dir=None):
    """The review list the drawer shows: each pair with both sides resolved to
    what a human needs to tell them apart (title, date, length, speakers)."""
    dst_dir = dst_dir or config.meetings_dir()
    rows = []
    for p in dupes.cached_pairs(dst_dir):
        sides = []
        for base in (p["a"], p["b"]):
            j = config.meeting_file(base, ".json", dst_dir)
            meta = _meeting_meta(j, dst_dir)
            if not meta:
                break
            sides.append({"base": base, "title": meta["title"], "date": meta["date"],
                          "minutes": meta["minutes"], "seconds": meta["seconds"],
                          "speakers": meta["speakers"],
                          "source_file": meta.get("source_file")})
        if len(sides) == 2:
            rows.append({"a": sides[0], "b": sides[1], "score": p["score"]})
    return rows


def _watched_dirs():
    """The watched folders, in THE resolution order -- the iCloud source first,
    then the recorder's staging dir. The queue shows one row per NAME, so every
    by-name endpoint has to walk them in this same order or it acts on a file
    the row never showed. The Voice Memos staging copies are a source too
    (config.source_dirs), so they show as queued like any other arrival."""
    return tuple(config.source_dirs())


def _queue_files(name):
    """EVERY waiting source file called `name`, in resolution order.

    A name can exist in both watched folders with different bytes behind it.
    The queue lists only the first, so a delete that ignores the second leaves
    a file that instantly becomes a new row under the same name -- inheriting,
    silently, whatever hold the deleted one carried. Callers that act on a file
    take the first entry (the one the row showed) and REPORT the rest.

    Queue items have no meeting yet, so there is no base to gate on — this is
    their equivalent of _known_base. A name is accepted ONLY if it lands, after
    resolution, directly inside one of the watched folders and is a real audio
    file. Resolving first and then re-checking the parent is what defeats
    traversal ('../../etc/passwd') and symlinks pointing out of the folder;
    dotfiles are refused so an in-progress .rec-*.caf capture can't be served
    or deleted out from under the recorder."""
    if not name or not isinstance(name, str):
        return []
    if "/" in name or "\\" in name or name.startswith("."):
        return []
    out = []
    for folder in _watched_dirs():
        try:
            p = (folder / name).resolve()
            if p.parent != folder.resolve() or not p.is_file():
                continue
            if p.suffix.lower() not in config.AUDIO_EXTS:
                continue
            if p not in out:
                out.append(p)
        except OSError:
            continue
    return out


def _queue_file(name):
    """The waiting source file called `name` -- the one the queue row shows --
    or None. See _queue_files for the resolution rule."""
    found = _queue_files(name)
    return found[0] if found else None


def _stored_audio(base):
    """The meeting's own audio on disk -- live or archived -- or None.

    drop_audio() deletes it and keeps the transcript, and the manifest's
    recorded fingerprint goes on matching the original source afterwards. So
    "the meeting still exists" is NOT enough to prove a waiting file is a
    second copy: without this check the sweep deletes the last copy of that
    recording anywhere."""
    if not base or not isinstance(base, str):
        return None
    for d in (config.meetings_dir(), config.archive_dir()):
        try:
            p = config.meeting_audio(base, d)
        except OSError:
            continue
        if p is not None:
            return p
    return None


_UPLOAD_MAX = 2 * 1024 ** 3  # 2 GB — refuse anything larger outright
# serialize uniquify->replace: two concurrent uploads of the same name on
# separate server threads must not both pick the same free target
_upload_lock = threading.Lock()


def _upload_name(raw) -> str | None:
    """The filename an uploaded file may land as, or None to refuse.

    Uploads are the one place a client-supplied name BECOMES a file on disk,
    so it goes through the same character rules as every name the app mints
    (summarize._sanitize_name, mirroring recorder.final_name): only the last
    path component is kept, path/wildcard/control chars are dropped, and
    leading dots are stripped — no traversal, no dotfile (the watcher must
    keep skipping in-progress captures), no '..'. The extension allowlist is
    AUDIO_EXTS — the SAME set the folder watcher picks up, so anything
    droppable is anything watchable — checked case-insensitively and
    normalized to lower."""
    if not isinstance(raw, str) or not raw:
        return None
    from stt import summarize
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]  # basename, either separator
    ext = Path(name).suffix.lower()
    if ext not in config.AUDIO_EXTS:
        return None
    stem = summarize._sanitize_name(name[:-len(ext)])
    if not stem:
        return None
    return stem + ext


def _upload_target(dest: Path, name: str) -> Path:
    """Where an upload called `name` may land: the house ' (N)' uniquify
    (recorder._uniquify's rule). Never overwrite a file in EITHER watched
    folder (a basename shared across them collides on one meeting folder and
    one manifest key — run_batch would skip one), and never mint a stem that
    collides with an existing or archived meeting (registries reference
    meetings by name; a redo goes through the explicit Redo path, not a
    filename collision)."""
    stem, ext = os.path.splitext(name)

    def taken(s):
        return ((dest / f"{s}{ext}").exists()
                or any((d / f"{s}{ext}").exists() for d in config.source_dirs())
                or config.meeting_dir(s).exists()
                or (config.archive_dir() / s).exists())

    if not taken(stem):
        return dest / name
    i = 2
    while taken(f"{stem} ({i})"):
        i += 1
    return dest / f"{stem} ({i}){ext}"


def _display_title(base: str) -> str:
    """The folder name minus its date stamp, so '2026-07-10 Weekly Check-in'
    (or the older 'Weekly Check-in 07102026') shows as 'Weekly Check-in' (the
    date is shown separately). The date lives in
    the FILENAME to keep recurring meetings unique on disk; the panel shows the
    clean name. dates.strip_stamp is the single definition of that rule — the
    pipeline, rename, the date editor, and the recorder all stamp through the same
    helpers, so what the panel strips is exactly what they add."""
    return dates.strip_stamp(base)


def _shown_title(d: dict, base: str) -> str:
    """_display_title(base), or the stored title as typed when the folder
    holds it in its safe form (dates.shown_title): '1:1' on '... 1-1'."""
    shown = dates.shown_title(d.get("title") if isinstance(d, dict) else None, base)
    if shown != dates.title_of(base):
        return shown + dates.twin_suffix(base)
    return _display_title(base)


def _archived_title(base: str) -> str:
    """The display title of an ARCHIVED meeting, read from its own folder.

    The live title map is built from live metas only, so a duplicate source
    whose match is archived (the duplicate gate deliberately spans live +
    archived) fell back to the raw stamped base -- and the row then linked that
    base into the meeting view, which renders live meetings only. Resolve the
    title here and let the row say "archived" instead of offering a dead link.
    """
    try:
        d = json.loads((config.archive_dir() / base / f"{base}.json").read_text())
    except (OSError, ValueError):
        d = {}
    return d.get("title") or _display_title(base)


def _meeting_meta(j: Path, dst_dir: Path):
    """Metadata for one meeting JSON, cached by mtime — the panel polls every 2s
    and transcripts run to hundreds of KB; parse each file once per change."""
    try:
        key = (str(j), j.stat().st_mtime)
    except FileNotFoundError:
        return None
    if key in _meet_cache:
        return _meet_cache[key]
    try:
        d = json.loads(j.read_text())
        # voices a human marked "not a real speaker" IN THIS MEETING (grumbling,
        # music, an echo). Their lines keep their attribution; they just stop
        # being naming targets. This set is the ONE gate: every "?" affordance
        # downstream reads unnamed_clusters / speaker_options, so subtracting it
        # here silences the prompt everywhere at once.
        dismissed = {str(x) for x in (d.get("dismissed_voices") or [])}
        audio_p = config.meeting_audio(j.stem, dst_dir)
        from datetime import date as _date
        from datetime import datetime as _dt
        audio_mtime = (audio_p or j).stat().st_mtime
        # stored date wins (stamped at process time, human-editable);
        # filename/mtime derivation only for pre-migration jsons
        iso = (d.get("date") or dates.meeting_date(j.stem)
               or _date.fromtimestamp(audio_mtime).isoformat())
        unnamed = {s["display"]: s["id"]
                   for s in d.get("speakers", [])
                   if not s.get("name")
                   and s.get("id") != s.get("display")
                   and s.get("id") not in dismissed}
        # talk seconds per cluster, over the lines that carry text (the same
        # lines /api/voice_lines counts): the voice queue starts with the
        # unnamed voice that talked the most
        talk = {}
        for seg in d.get("segments", []):
            if seg.get("text", "").strip():
                talk[seg.get("speaker")] = talk.get(seg.get("speaker"), 0.0) + max(
                    0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
        # the NAMED people, each once (a split voice once named is two
        # clusters with one name), the most talk first, ties in roster order.
        # A To finish row lists up to three of these and never a raw
        # "Speaker N" / "Voice N" label: a board meeting listed 22 of them.
        named = {}
        for s in d.get("speakers", []):
            if s.get("name"):
                named[s["display"]] = named.get(s["display"], 0.0) + talk.get(s["id"], 0.0)
        # the voice exemptions (plan Phase 1 step 4). A public download asks
        # nothing: its voices are not the owner's working meetings. A voice
        # under VOICE_ASK_MIN_SECS of talk hides behind a count. A split
        # voice (two clusters, one display) counts its clusters' talk
        # together. `unnamed_clusters` itself stays whole: the reader's
        # legend still names any voice a person chooses to.
        route = d.get("route") or titles.route_for(d.get("source_file") or "")
        unnamed_talk = {}
        for s in d.get("speakers", []):
            if (not s.get("name") and s.get("id") != s.get("display")
                    and s.get("id") not in dismissed):
                unnamed_talk[s["display"]] = (unnamed_talk.get(s["display"], 0.0)
                                              + talk.get(s.get("id"), 0.0))
        if route == "public_download":
            ask = {}
        else:
            ask = {disp: cid for disp, cid in unnamed.items()
                   if unnamed_talk.get(disp, 0.0) >= VOICE_ASK_MIN_SECS}
        meta = {"base": j.stem,
                # the immutable id: group corrections and saved Ask
                # citations key on it, so they survive a rename
                "meeting_id": d.get("meeting_id"),
                "date": iso,
                # display name: the folder carries the date so recurring
                # meetings stay unique on disk, but the panel shows the clean
                # name (the date is shown separately). A title typed with a
                # ':' or '/' shows as typed ('1:1' on a '... 1-1' folder)
                "title": _shown_title(d, j.stem),
                "minutes": round(d.get("duration_sec", 0) / 60),
                # whole seconds: a clip under a minute rounds to 0 minutes,
                # and the page shows '22 sec' for it, never '0 min'
                "seconds": round(float(d.get("duration_sec") or 0)),
                "speakers": [s["display"] for s in d.get("speakers", [])],
                "named": sorted(named, key=lambda k: -named[k]),
                # {display: cluster id} for the voices this meeting heard but
                # never named. The unknown registry does not track all of them
                # (the minting floor keeps one-off voices transcript-local, and
                # a "not a real speaker" tombstone drops others), so a caller
                # holding only uids cannot offer naming for those. This map is
                # the cluster-side naming path, the same one the transcript
                # legend uses.
                "unnamed_clusters": unnamed,
                # the longest-talking unnamed voice's seconds, for the queue
                "voice_secs": round(max((unnamed_talk.get(dp, 0.0) for dp in ask),
                                        default=0.0), 1),
                # the voices that ask (not public, at least a minute of talk),
                # their talk seconds, and how many short ones hide behind a count
                "ask_clusters": ask,
                "ask_talk": {dp: round(unnamed_talk.get(dp, 0.0), 1) for dp in ask},
                "short_voices": (0 if route == "public_download"
                                 else len(unnamed) - len(ask)),
                # the dismissed ids themselves, so a row or page that wants to
                # SAY so (or offer an undo) can, without re-reading the json
                "dismissed_voices": sorted(dismissed),
                "strict": d.get("strict", False),
                "flagged": sum(1 for s in d.get("segments", [])
                               if s.get("flags") and not review.is_minor(s)),
                "flagged_minor": sum(1 for s in d.get("segments", [])
                                     if s.get("flags") and review.is_minor(s)),
                "summary": d.get("ai_summary", ""),
                # the description's parts (stt/summarize.py): the gist the
                # list row shows, the sections and tags the meeting page
                # shows. Empty on a description written before them, where
                # the panel falls back to `summary`.
                "gist": d.get("ai_gist") or "",
                "decisions": d.get("ai_decisions") or [],
                "open_questions": d.get("ai_open_questions") or [],
                "tags": [series.TAG_LABELS[t] for t in (d.get("ai_tags") or [])
                         if t in series.TAG_LABELS],
                "series": d.get("ai_series"),
                # a Redo left the description out of date; the batch
                # regenerates it when idle
                "summary_stale": bool(d.get("ai_stale")),
                "category": d.get("category"),  # work | personal | None
                # ONLY an explicit False means "not yet named/dated by a human".
                # Meetings processed before the inbox existed carry no key at all,
                # and must not retroactively flood it — so a missing key = reviewed.
                "needs_review": d.get("reviewed") is False,
                # the title rule (stt/titles.py). A missing state is a meeting
                # from before the rule, and its needs_review decides alone.
                "title_state": d.get("title_state"),
                "title_source": d.get("title_source"),
                "route": route,
                "no_speech": bool(d.get("no_speech")),
                # where the date came from (stt/provenance.py). The page
                # shows it, and a file-time or short typed date wears the
                # Date uncertain chip. None on a meeting before the key.
                "date_source": d.get("date_source"),
                "recorded_at": d.get("recorded_at"),
                # the place Voice Memos named, kept when the title chain
                # passed over a default address title. The meeting page
                # shows it on the meta line. Absent on most meetings.
                "location_note": d.get("location_note"),
                "duration_sec": d.get("duration_sec"),
                # the Needs you window's key: the STORED arrival stamp only.
                # A meeting without one falls outside the window, so a
                # backfill write can never bring an old meeting back.
                # `arrived_at` is written once at first arrival and carried
                # across a Redo (which re-stamps processed_at). A meeting
                # from before that key arrived at its processed_at.
                "arrived_at": d.get("arrived_at") or d.get("processed_at"),
                "suggested": d.get("ai_title") or "",
                # when transcription last ran (new or redo). Older transcripts
                # predate this field — fall back to generated_at, then mtime.
                "processed_at": (d.get("processed_at") or d.get("generated_at")
                                 or _dt.fromtimestamp(
                                     j.stat().st_mtime).isoformat(timespec="seconds")),
                # the SAVE clock, re-stamped by output.write_json on every
                # write — a relabel, a review edit, a summary. processed_at
                # deliberately does not move for those, so this is the one
                # field that says "this transcript changed". The open meeting
                # page watches its own row's copy and reloads when it moves.
                "generated_at": d.get("generated_at"),
                # the originating filename, so the One Timeline can morph a
                # queued source row into this meeting's row in place (the source
                # gained an announced base mid-run) instead of flickering
                "source_file": d.get("source_file"),
                "audio": str(audio_p) if audio_p else None}
    except Exception:
        meta = None
    _meet_cache[key] = meta
    if len(_meet_cache) > 400:  # drop stale mtime generations
        for k in list(_meet_cache)[:200]:
            _meet_cache.pop(k, None)
    return meta


def _recording_state():
    """The live capture for the panel: the recorder's own view, plus the captured
    seconds resolved here (paused spans already excluded) so the browser never
    re-derives that arithmetic and drifts from the menu bar."""
    rec = recorder.live_recording()
    if not rec:
        return None
    return {**rec, "elapsed_secs": recorder.elapsed_seconds(rec),
            "paused": bool(rec.get("paused")),
            # header-only CAF ~10s in = TCC denial: the panel shows a red strip
            # with the Fix-permissions button WHILE the meeting can still be saved
            "stalled": recorder.capture_stalled(rec)}


# ---------- One Timeline: one row per meeting, changing state in place ----------
#
# The panel renders every recording/file as a SINGLE row that changes state
# (recording -> waiting/held -> processing -> needs_name -> ready, or failed) in
# place, instead of separate Queue / Processing / Recent / naming-inbox cards.
# `_timeline_tray` folds the structures gather_state has ALREADY built (the
# queue listing, the meeting metas, the live active-run stages, the unknown
# registry) into that one feed plus a ranked attention tray — no extra directory
# scans, and the only new disk read is the mtime-cached results log below.

# newest-poll pinning: a live capture and an in-flight run stay at the top of the
# feed regardless of their timestamp; everything else sorts purely by `when`.
_STATE_RANK = {"recording": 3, "processing": 2}

_results_cache = {"key": None, "rows": None}


def _results_rows():
    """The permanent results log parsed to dicts, cached by (path, mtime). The
    log only grows when a file finishes, so between runs this cache hits on every
    2s poll — the join never re-reads it just because the panel polled again."""
    p = status.HISTORY_LOG
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return []
    if _results_cache["key"] == key:
        return _results_cache["rows"]
    # status' own reader: a line of the wrong shape, or a byte that is not
    # UTF-8, is skipped (the failure list raised on either)
    rows = status.history_rows()
    _results_cache["key"], _results_cache["rows"] = key, rows
    return rows


def _failed_sources(st):
    """{source_file: error_text} for every source whose MOST RECENT result was a
    failure. Merges the mtime-cached results log with the live status ring
    (newest wins, matching status.history), so a source that later succeeded —
    its newest outcome an ok — never lingers as failed. The original stays in the
    watched folder, so a failed source keeps re-running until it lands or is
    removed."""
    rows = list(_results_rows())  # oldest-first on disk
    seen = {(r.get("name"), r.get("at")) for r in rows}
    recent = st.get("recent")
    rows += [r for r in (recent if isinstance(recent, list) else [])
             if status.is_result_row(r) and (r.get("name"), r.get("at")) not in seen]
    latest = {}
    for r in rows:
        nm = r.get("name")
        if nm is None:
            continue
        prev = latest.get(nm)
        if prev is None or (r.get("at") or "") >= (prev.get("at") or ""):
            latest[nm] = r
    dism = _dismissed_failures()
    return {nm: (r.get("summary") or "failed")
            for nm, r in latest.items()
            if not r.get("ok") and (r.get("at") or "") > (dism.get(nm) or "")}


def _dismissed_failures() -> dict:
    """{source_file: dismissed_at} — failures a human said to stop showing.
    A file of another shape ([], null, a string) or a time that is not text
    silences nothing: the failure list raised AttributeError on it, and a
    dismiss raised TypeError. The doctor's _stale_dismissals checks the
    same way."""
    try:
        d = json.loads((config.PROJECT_DIR / "dismissed_failures.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, str)}


def _voice_ids(b):
    """The clusters a per-meeting voice request acts on: the body's
    `speakers` list (every cluster of one split voice) when it sends one,
    else its single `speaker`, as before the list existed."""
    sids = b.texts("speakers", None)
    if sids:
        return sids
    return b.text("speaker", "")


def _folder_kind(b) -> str:
    """`which` for the folder pickers. Anything but "source" used to mean
    the transcripts folder, so a typo moved the meeting store."""
    which = b.text("which")
    if which not in ("source", "dest"):
        raise BadRequest("'which' must be source or dest")
    return which


def _handle(b, key):
    """One side of a merge: ("uid", "U003") or ("name", "Jordan")."""
    kind, sep, value = b.text(key).partition(":")
    if not sep or kind not in ("uid", "name") or not value:
        raise BadRequest(f"'{key}' must look like uid:U003 or name:Jordan")
    return kind, value


_dismiss_lock = threading.Lock()


def dismiss_failure(name: str) -> bool:
    """Silence every PAST failure for `name`. A failure newer than the
    dismissal surfaces again: this means "stop telling me about that one",
    never "never tell me about this file". Exists because a history-only
    failure (the source file is gone) has no other exit — its X used to call
    the file-delete endpoint, which refused because there was no file, so the
    dead row was permanent.

    The read-modify-write runs under a lock (ThreadingHTTPServer handles
    requests concurrently, and an unguarded RMW silently drops the losing
    dismissal), with a unique tmp per writer, and the map self-caps at the
    newest 200 entries so a years-old panel never carries an unbounded file
    of dead filenames."""
    nm = str(name or "").strip()
    if not nm or "/" in nm or "\\" in nm or nm.startswith("."):
        return False
    p = config.PROJECT_DIR / "dismissed_failures.json"
    with _dismiss_lock:
        d = _dismissed_failures()
        d[nm] = status._now()
        if len(d) > 200:
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-200:])
        fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                                   prefix="dismissed_failures.",
                                   suffix=".json.tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d))
        os.replace(tmp, p)
    return True


def _preview(text, sentences=2, cap=320):
    """A short preview of a summary — the first `sentences` sentences when they
    fit within `cap` chars (roughly two lines' worth), else the text hard-capped
    with an ellipsis. Runs of whitespace collapse to single spaces first, so the
    preview is always one clean line."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    end = 0
    for _ in range(sentences):
        nxt = min((i for i in (t.find(m, end) for m in (". ", "? ", "! ")) if i >= 0),
                  default=-1)
        if nxt < 0:
            end = len(t)      # the final sentence runs to the end of the text
            break
        end = nxt + 1         # keep the period, drop its trailing space
    if end <= cap:
        return t[:end]
    return t[:cap - 1].rstrip() + "…"


def _finish(meta, uid_of=None) -> dict:
    """{title, voices, flags, done, voice_secs, voice_uids} for one meeting's
    metadata. voice_secs is the talk time of the meeting's longest-talking
    unnamed voice (0 with none): "Name N voices" opens the meeting that holds
    the voice with the most talk time first. voice_uids are the unknown-
    registry uids among those unnamed voices, matched the way the page's
    mUnknownUid matches them (display, and this meeting in the voice's
    meetings); the rest are transcript-local. One uid heard in three meetings
    is ONE voice to the queue, because naming it once names it everywhere, so
    "Name N voices" counts each uid once and each local voice once per
    meeting: the number of voices the queue will actually ask about."""
    title = not titles.needs_title(meta)
    ask = meta.get("ask_clusters")
    unnamed = ask if ask is not None else (meta.get("unnamed_clusters") or {})
    voices = len(unnamed)
    flags = int(meta.get("flagged") or 0)
    uids = sorted({uid_of[(d, meta["base"])] for d in unnamed
                   if (d, meta["base"]) in (uid_of or {})})
    # plan row 8: done unless the title is still needed. Voices and flags
    # stay on the record as counts, and they block nothing.
    return {"title": title, "voices": voices, "flags": flags,
            "done": title,
            "voice_secs": float(meta.get("voice_secs") or 0) if voices else 0.0,
            "voice_uids": uids,
            "short_voices": int(meta.get("short_voices") or 0)}


def _run_order(queue, pending, sweep):
    """{name: position} for the waiting files, in the order the batch will
    run them: shortest first by stt.queue_order's length, ties by name. A run
    that sweeps the folders (and the next automatic run, when idle) re-picks
    the shortest file left before every file, so every waiting file takes its
    place by length. A run over its own list (--files, a queued job) runs
    that list first, in the order status.json's `pending` gives."""
    at = {n: i for i, n in enumerate(pending or [])}

    def key(f):
        d = f.get("dur_sec")
        by_length = (d if d is not None else float("inf"), f["name"])
        if not sweep and f["name"] in at:
            return (0, at[f["name"]], by_length)
        return (1, 0, by_length)
    waiting = [f for f in queue if not f.get("processed") and not f.get("held")
               and not f.get("parked")]
    return {f["name"]: i for i, f in enumerate(sorted(waiting, key=key))}


def _timeline_tray(st, queue, meetings, active_out, unknown_list, rec, order=None):
    """(timeline, tray) — the unified per-meeting feed (newest first) and the
    ranked attention tray, built entirely from gather_state's own locals.
    `order` is _run_order's answer: each waiting row carries its place in
    it as `run_order`, and Now lists the waiting rows by it."""
    from datetime import datetime as _dt
    timeline, failed_entries = [], []
    # sources already owned by another row: never emit a duplicate history-only
    # failure for a file that is queued, in flight, or already a meeting
    emitted = set()
    # (display, meeting) -> the unknown-registry uid heard there, hidden ones
    # included: a hidden voice keeps its naming chip, so it is still a voice
    # the meeting's finish record counts and the voice queue asks about
    uid_of = {(u["display"], m): u["uid"]
              for u in unknown_list for m in u.get("meetings") or []}

    # --- recording: the live capture, always one row, pinned to the top ---
    if rec:
        cap = Path(rec.get("caf", "recording")).stem or "recording"
        timeline.append({
            "id": f"rec:{cap}", "state": "recording", "generated_at": None,
            "title": "Recording", "date": (rec.get("started_at") or "")[:10] or None,
            "when": rec.get("started_at") or status._now(),
            "elapsed_secs": rec.get("elapsed_secs", 0),
            "paused": bool(rec.get("paused")), "stalled": bool(rec.get("stalled"))})

    # --- processing: the in-flight run. Keyed by SOURCE filename; the run has
    # already announced its resolved (date-stamped) base via set_stage, so the
    # row's id is that base and source_file lets the client morph the queued
    # `src:<file>` row into it in place. prev_id spells that flip out. ---
    for name, e in active_out.items():
        base = e.get("base")
        emitted.add(name)
        timeline.append({
            "id": base or f"src:{name}", "state": "processing",
            "generated_at": None,
            "source_file": name, "prev_id": f"src:{name}",
            "title": _display_title(base) if base else name,
            "date": dates.meeting_date(base or name),
            "when": e.get("since") or status._now(),
            "stage": e.get("stage"), "pct": e.get("pct"), "eta": e.get("eta_sec"),
            "over": e.get("over_sec")})

    # --- meetings: needs_name (awaiting a human name/date) or ready. A meeting
    # being reprocessed shows as its processing row above, not twice. ---
    active_bases = {e.get("base") for e in active_out.values() if e.get("base")}
    # names sitting in the watched folder RIGHT NOW, unprocessed. When a meeting
    # shares one, the file was re-dropped (new mtime, so the manifest reads it as
    # brand new) and the batch really will transcribe it again, so its waiting
    # row -- and the duplicate chip on it -- has to show. Claiming the name here
    # hid work that was about to happen.
    unprocessed = {f["name"] for f in queue if not f["processed"]}
    for meta in meetings:
        if meta["base"] in active_bases:
            continue
        if meta.get("source_file") and meta["source_file"] not in unprocessed:
            # a PROCESSED source still lingering in the folder is this meeting
            emitted.add(meta["source_file"])
        row = {"source_file": meta.get("source_file"),
               "title": meta["title"], "date": meta["date"],
               "when": meta["processed_at"],
               # the transcript's own save stamp: the open meeting page reloads
               # itself when ITS row's stamp moves, instead of guessing from a
               # library-wide "a relabel is running" boolean
               "generated_at": meta.get("generated_at"),
               # the finish record: what is left before this meeting is done,
               # from the counts already on hand. `title` is the human's name
               # and date accepted (reviewed); `voices` the unnamed clusters
               # still asking (dismissed ones excluded, the same set every "?"
               # reads); `flags` the substantial flagged lines. The meeting
               # page's finish strip walks these in order.
               "finish": _finish(meta, uid_of)}
        if titles.needs_title(meta):
            # the naming row opens only for title_state=needs_title, or for a
            # meeting from before the title rule that still needs review.
            # the naming row is a review card, not a bare text box: the same
            # evidence the ready row shows (length, who was heard, what the
            # meeting was about) is exactly what a human needs to decide on a
            # name, and the unnamed voices are nameable from here so the one
            # pass over a fresh meeting finishes the job.
            row.update({"id": meta["base"], "state": "needs_name",
                        "suggested_title": meta["suggested"],
                        "suggested_date": meta["date"],
                        "has_audio": bool(meta["audio"]),
                        "minutes": meta["minutes"], "seconds": meta["seconds"],
                        "speakers": meta["speakers"],
                        "named": meta["named"],
                        "unnamed_clusters": meta["unnamed_clusters"],
                        "summary": _preview(meta["summary"])})
        else:
            row.update({"id": meta["base"], "state": "ready",
                        "minutes": meta["minutes"], "seconds": meta["seconds"],
                        "speakers": meta["speakers"],
                        "named": meta["named"],
                        "category": meta["category"],
                        "review_substantial": meta["flagged"],
                        "review_minor": meta["flagged_minor"],
                        "has_summary": bool(meta["summary"]),
                        "summary": _preview(meta["summary"])})
        timeline.append(row)

    # --- queue sources: waiting / held / failed. A source in flight is already a
    # processing row above; a processed one is already its meeting. ---
    failed_map = _failed_sources(st)
    for f in queue:
        name = f["name"]
        if f["processed"] or name in emitted:
            continue
        emitted.add(name)
        row = {"id": f"src:{name}", "source_file": name, "generated_at": None,
               "title": name, "date": dates.meeting_date(name),
               "when": _dt.fromtimestamp(f["mtime"]).isoformat(timespec="seconds"),
               "size_mb": f["size_mb"], "est_minutes": f.get("est_min"),
               # the split the total hides (transcription vs speaker
               # separation): computed per poll for every queued file, so it
               # rides the row the shell renders rather than being thrown away
               "est_detail": f.get("est_detail")}
        if f.get("dup_of"):
            # the row says so before the batch ever picks the file up
            row.update({"dup_of": f["dup_of"], "dup_title": f.get("dup_title"),
                        "dup_reason": f.get("dup_reason"),
                        "dup_archived": bool(f.get("dup_archived"))})
        if f["held"]:
            row.update({"state": "held", "held": True})
        elif name in failed_map:
            row.update({"state": "failed", "error": failed_map[name],
                        "retry_note": "still in the watched folder — it re-runs on the next batch"})
            if f.get("parked"):
                row.update({"parked": True,
                            "retry_note": "set aside after failing — automatic runs skip it "
                                          "until the file changes; Retry runs it now"})
            failed_entries.append(row)
        elif f.get("parked"):
            # a set-aside failure the user dismissed: no automatic run picks
            # it, so it must not read as waiting
            continue
        else:
            row["state"] = "waiting"
            if order and name in order:
                row["run_order"] = order[name]
        timeline.append(row)

    # --- history-only failures: a file that failed and is no longer queued (and
    # never became a meeting). It stays visible so the failure is not silently
    # lost the moment the source leaves the folder. ---
    for name, err in failed_map.items():
        if name in emitted:
            continue
        emitted.add(name)
        row = {"id": f"src:{name}", "state": "failed", "source_file": name,
               "generated_at": None,
               "title": name, "date": dates.meeting_date(name),
               "when": _failed_at(st, name) or status._now(),
               "error": err, "gone": True,
               "retry_note": "the original is gone — re-add it to the watched folder to retry"}
        timeline.append(row)
        failed_entries.append(row)

    timeline.sort(key=lambda r: (_STATE_RANK.get(r["state"], 1), r["when"]),
                  reverse=True)

    # --- tray: what needs the user, in rank order ---
    tray = []
    if rec and rec.get("stalled"):
        tray.append({"kind": "recorder_stall", "title": "Recording",
                     "detail": "no audio is being captured — check microphone access",
                     "target": f"rec:{Path(rec.get('caf', 'recording')).stem or 'recording'}",
                     "count": 1})
    # the last recording FAILED ("captured NO audio ..."): the classic page
    # showed it with Fix permissions. It stays until the next capture
    # starts, as in the menu bar.
    note = status.recorder_note()
    if isinstance(note, dict) and note.get("ok") is False:
        tray.append({"kind": "recorder_note", "title": "Last recording",
                     "detail": str(note.get("text") or "the recording failed"),
                     "target": f"note:{note.get('at') or ''}", "count": 1})
    for row in failed_entries:
        # `gone` rides the tray entry too: the timeline row uses it to swap
        # Retry for a dismiss, and a tray line that kept offering Retry posted
        # /api/run for a file that is no longer on disk
        tray.append({"kind": "failed", "title": row["title"],
                     "detail": row["error"], "target": row["id"], "count": 1,
                     "gone": bool(row.get("gone"))})
    # waiting files that were already processed. Ranked here, above reviews: the
    # next automatic run spends real hours transcribing them again, so it is
    # worth deciding before that happens. `count` is what a bulk delete would
    # take: the content-identical ones, never a name-only guess.
    dups = [f for f in queue if f.get("dup_of") and not f["processed"]
            and f["name"] not in active_out]
    if dups:
        exact = [f for f in dups if f.get("dup_reason") == "identical" and not f["held"]]
        one = dups[0]
        tray.append({"kind": "dupe_files",
                     "title": "Already processed" if len(dups) > 1 else one["name"],
                     "detail": (f"{len(dups)} waiting files were already processed"
                                if len(dups) > 1
                                else f"already processed as {one.get('dup_title')}"),
                     "target": f"src:{one['name']}",
                     "exact": len(exact), "count": len(dups)})
    # transcripts that look like the same meeting twice. Read from the cache
    # only: the comparison itself runs off the poll thread (see _dupe_refresh).
    try:
        pair_n = len(dupes.cached_pairs(config.meetings_dir()))
    except Exception:
        pair_n = 0
    if pair_n:
        tray.append({"kind": "dupe_meetings", "title": "Duplicate transcripts",
                     "detail": f"{pair_n} pair{'s' if pair_n != 1 else ''} "
                               f"look like the same meeting",
                     "target": "dupes", "count": pair_n})
    # flagged lines left this list (plan row 8): they stay as quiet markers
    # in the reader, and the Has flags chip still finds them
    public = {m["base"] for m in meetings if m.get("route") == "public_download"}
    for u in unknown_list:
        if u.get("archived"):  # hidden one-time voices never nag
            continue
        if u.get("meetings") and all(m in public for m in u["meetings"]):
            continue           # heard only in public downloads: they ask nothing
        n = len(u.get("meetings", []))
        tray.append({"kind": "unknown_voice", "title": u["display"],
                     "detail": f"heard in {n} meeting{'s' if n != 1 else ''} — who is this?",
                     "target": u["uid"], "count": n})
    return timeline, tray


# ---------- Needs you: the one short list (plan rows 10, 11, 12) ----------
#
# Computed on read from the meeting metas and the unknown registry, so it adds
# no store and no stored expiry. It holds only what a person must settle, for
# meetings that ARRIVED in the last NEEDS_YOU_DAYS days (by the stored
# processed_at, so a memo recorded weeks ago and transferred today appears, and
# a meeting with no stored processed_at never does). At most NEEDS_YOU_MAX
# rows and no "N older" line: a count that can reach zero gets used. Older
# items leave the list and stay recorded on the meeting. Flags never appear.
# A failed file and a duplicate pair stay in Now, where their verbs live.
NEEDS_YOU_DAYS = 14
NEEDS_YOU_MAX = 5
_NY_RANK = {"no_speech": 0, "duration_mismatch": 1, "needs_title": 2, "voice": 3}


def _arrived_within(meta, now, days=NEEDS_YOU_DAYS) -> bool:
    from datetime import datetime as _dt
    at = meta.get("arrived_at")
    if not at:
        return False
    try:
        t = _dt.fromisoformat(str(at))
    except ValueError:
        return False
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return (now - t).total_seconds() <= days * 86400


def _audio_short_by(meta):
    """Minutes the audio falls short of the stored duration, when it is over
    a minute short (audio may be lost), else None."""
    if not meta.get("audio") or not meta.get("duration_sec"):
        return None
    try:
        secs = queue_order.duration(Path(meta["audio"]))
    except Exception:
        return None
    if secs is None:
        return None
    gap = float(meta["duration_sec"]) - float(secs)
    return round(gap / 60) if gap > 60 else None


def _needs_you(meetings, active_bases, unknown_list, now=None) -> list:
    """The ranked Needs you items: silent recordings, audio shorter than its
    transcript, titles with only the model's draft, then ONE voice row: the
    unnamed voice with the most talk in non-public meetings, among the voices
    heard in a meeting inside the window."""
    from datetime import datetime as _dt
    now = now or _dt.now()
    items = []
    window = set()
    for meta in meetings:
        if meta["base"] in active_bases or not _arrived_within(meta, now):
            continue
        window.add(meta["base"])
        kind, detail = None, ""
        short_by = None if meta.get("no_speech") else _audio_short_by(meta)
        if meta.get("no_speech"):
            kind, detail = "no_speech", "No speech found in this recording"
        elif short_by:
            kind = "duration_mismatch"
            detail = (f"The audio is {short_by} minute{'s' if short_by != 1 else ''} "
                      f"shorter than the transcript: audio may be lost")
        elif titles.needs_title(meta):
            kind = "needs_title"
            detail = (f"Draft title: {meta['suggested']}" if meta.get("suggested")
                      else "No title yet")
        if kind:
            items.append({"kind": kind, "target": meta["base"], "title": meta["title"],
                          "detail": detail, "when": meta.get("arrived_at")})
    items.sort(key=lambda x: x["when"] or "", reverse=True)
    items.sort(key=lambda x: _NY_RANK[x["kind"]])
    v = _top_voice(meetings, unknown_list, window)
    if v:
        # the voice row keeps its place: meeting rows fill the rest, so a
        # day of new titles never pushes the one voice row out (plan row 12)
        return items[:NEEDS_YOU_MAX - 1] + [v]
    return items[:NEEDS_YOU_MAX]


def _top_voice(meetings, unknown_list, window):
    """The one voice row (plan row 12): ranked by unnamed talk across every
    non-public meeting, offered only for a voice heard in a meeting inside the
    window. A registry voice is one voice across meetings. A transcript-local
    voice is one voice in its own meeting. Public downloads and voices under
    the one-minute floor never count (their ask_talk is empty)."""
    uid_of = {(u["display"], m): u["uid"]
              for u in unknown_list if not u.get("archived")
              for m in u.get("meetings") or []}
    talk, heard, best = {}, {}, {}
    for meta in meetings:
        for disp, secs in (meta.get("ask_talk") or {}).items():
            uid = uid_of.get((disp, meta["base"]))
            key = uid or f"{meta['base']}\x1f{disp}"
            talk[key] = talk.get(key, 0.0) + float(secs or 0)
            heard.setdefault(key, set()).add(meta["base"])
            if meta["base"] in window and secs > best.get(key, ("", -1.0))[1]:
                best[key] = (meta["base"], float(secs), disp)
    cands = [k for k in best if talk.get(k, 0) > 0]
    if not cands:
        return None
    k = max(cands, key=lambda c: (talk[c], c))
    base, _, disp = best[k]
    n = len(heard[k])
    mins = max(1, round(talk[k] / 60))
    title = next((m["title"] for m in meetings if m["base"] == base), base)
    return {"kind": "voice", "target": base, "title": disp,
            "uid": k if "\x1f" not in k else None,
            "detail": (f"{mins} min of unnamed talk in {n} meeting{'s' if n != 1 else ''}"
                       f", most recently {title}"),
            "secs": round(talk[k], 1), "when": None}


def _failed_at(st, name):
    """When `name`'s most recent (failed) result landed — for the history-only
    failure row's sort timestamp."""
    at = None
    for r in _results_rows() + list(st.get("recent", [])):
        if r.get("name") == name and (at is None or (r.get("at") or "") >= at):
            at = r.get("at")
    return at


def _queue_and_dupes():
    """The queue (with duplicate flags), the meeting metas, and the set of bases
    that still exist. PURE: no spawns, no kicks, no cache writes. gather_state
    layers its side effects on top; /api/queue_delete_dupes calls ONLY this, so
    asking for a delete can never be the thing that starts a batch run."""
    from stt import holds
    _held = holds.items()
    m = manifest.load()
    dst_dir = config.meetings_dir()
    queue = []
    # BOTH watched folders: the iCloud source AND the recorder's staging dir.
    # Only the source used to be listed, so a finished recording waited for the
    # batch completely invisibly — "I stopped, where did it go?"
    # _watched_dirs is the ONE resolution order: the row this loop emits and the
    # path every by-name endpoint acts on must be the same file
    _qfiles, _qseen, _qpaths = [], set(), {}
    for _qd in _watched_dirs():
        try:
            for p in sorted(_qd.iterdir()):
                if p.name not in _qseen:
                    _qseen.add(p.name)
                    _qfiles.append(p)
        except OSError:
            pass
    try:
        for p in _qfiles:
            if config.is_source_audio(p):
                done = manifest.is_processed(m, p.name, p.stat().st_mtime)
                dur = None if done else _est_duration(p)
                est_min, est_detail = None, None
                if dur:
                    ests = status.stage_estimates(dur)
                    # FLOOR at one minute: a short file still carries a fixed
                    # per-file overhead, and round() turned that into 0 -- which
                    # every downstream truthiness check ("if est_min") reads as
                    # NO estimate at all, so a running batch with only short
                    # files pending reported no ETA anywhere on screen.
                    est_min = max(1, round(sum(ests.values()) / 60))
                    # the split the total hides: transcription and speaker
                    # separation dominate and scale differently per file
                    est_detail = " · ".join(
                        f"{label} ~{max(1, round(ests[k] / 60))}m"
                        for k, label in (("transcribing", "transcribe"),
                                         ("diarizing", "speakers"))
                        if k in ests)
                queue.append({"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1),
                              "video": p.suffix.lower() in config.VIDEO_EXTS,
                              "processed": done, "est_min": est_min,
                              "mtime": p.stat().st_mtime,  # timeline sort key
                              # the length the batch orders its picks by
                              # (shortest first): the run order below
                              "dur_sec": dur,
                              "held": p.name in _held, "est_detail": est_detail,
                              # set aside after failing: automatic runs skip it
                              "parked": (not done) and manifest.is_parked(m, p.name, p)})
                if not done:
                    _qpaths[p.name] = p    # for the duplicate check below
    except Exception:
        pass
    meetings = []
    # capture each mtime under try/except: a meeting folder can vanish
    # mid-request (a concurrent /api/rename), and a bare stat() in the sort key
    # would 500 the whole poll instead of just dropping that one item
    dated = []
    for b in config.meeting_bases(dst_dir):
        j = config.meeting_file(b, ".json", dst_dir)
        try:
            dated.append((j.stat().st_mtime, j))
        except OSError:
            continue
    for _, j in sorted(dated, key=lambda t: t[0], reverse=True):
        meta = _meeting_meta(j, dst_dir)
        if meta:
            meetings.append(meta)
    # the bases that still EXIST, live or archived: the duplicate gate and the
    # unknowns section both need exactly this membership, derived once so the
    # poll and the sweep endpoint can never disagree about it
    resolvable = set(config.meeting_bases(dst_dir)) | set(config.archived_bases(dst_dir))
    # already processed, and back in the folder anyway: a copy under a new name,
    # an iCloud re-sync, a second export. The manifest keys on name + mtime and
    # cannot see it, so without this the file is transcribed a second time and
    # the library grows a twin. Only waiting files are checked (a processed one
    # is already its meeting), and only a SIZE COLLISION is ever hashed, so an
    # idle poll does no reading at all.
    dup_map = {}
    if _qpaths:
        try:
            dup_map = dupes.source_duplicates(_qpaths.values(), meetings, m,
                                              dst_dir, live=resolvable)
        except Exception:
            dup_map = {}
    _titles = {meta["base"]: meta["title"] for meta in meetings}
    for f in queue:
        d = dup_map.get(f["name"])
        if d:
            f["dup_of"] = d["base"]
            f["dup_reason"] = d["reason"]
            if d["base"] in _titles:
                f["dup_title"] = _titles[d["base"]]
            else:
                # the match is ARCHIVED: it has no row and no page in the live
                # view, so the row must name it without linking to one
                f["dup_title"] = _archived_title(d["base"])
                f["dup_archived"] = True
            if d["reason"] == "identical":
                # the fingerprint the match was made against, taken here and
                # carried on the row, so the bulk delete can re-take it at
                # unlink time and prove the file on disk is still that file.
                # dupes caches per (path, size, mtime), so this costs no I/O:
                # source_duplicates just took it.
                fp_now = dupes.fingerprint(_qpaths[f["name"]])
                if fp_now:
                    f["dup_fp"] = fp_now
    return queue, meetings, resolvable


def _with_groups(meetings: list) -> list:
    """Each meeting row with its computed group, when it has one (plan Phase
    4E). The rows are shared cache entries, so a grouped row is a copy."""
    try:
        gm = groups.membership()
    except Exception as e:
        print(f"   groups: not computed ({e})", file=sys.stderr)
        return meetings
    return [dict(m, group=gm[m["base"]][0], group_name=gm[m["base"]][1])
            if m["base"] in gm else m for m in meetings]


def _search_filters(q: dict) -> dict:
    """The /api/search filters from query parameters. `dated` is the short
    name for `date_source`, as typed in the palette."""
    out = {k: q[k] for k in ("after", "before", "route", "date_source", "from", "in")
           if q.get(k)}
    if q.get("dated") and "date_source" not in out:
        out["date_source"] = q["dated"]
    return out


def gather_state():
    from stt import summarize
    st = status.read()
    queue, meetings, resolvable = _queue_and_dupes()
    src_dir, dst_dir = config.source_dir(), config.meetings_dir()
    reg = unknowns.load()
    unknown_list = []
    # the enrolled voiceprints, loaded at most ONCE per poll and only when some
    # unknown's looks-like answer is actually stale (see _unknown_looks_like)
    _prints = {}

    def _prints_once():
        if "v" not in _prints:
            _prints["v"] = identify.load_voiceprints()
        return _prints["v"]
    # "heard in N meetings" may only count meetings that still EXIST — live
    # (the same membership the clip endpoints gate on) or archived (restorable,
    # so their refs deliberately survive). Deletes scrub refs at the source,
    # but refs can predate the scrub, so liveness (`resolvable`, from the
    # helper above) is re-derived every poll rather than trusted from the raw
    # arrays: the count and the ▶ playback can then never disagree.
    for uid, meta in sorted(reg["speakers"].items()):
        if meta.get("dropped"):
            continue  # tombstoned "not a real speaker" — never surfaces again
        mts = [m for m in unknowns.resolved_meetings(meta) if m in resolvable]
        if not mts:
            # zero live/archived refs: nothing to play, nothing to name against
            # — hide it rather than nag "heard in 0 meetings" forever. NOT
            # deleted: the registry entry and its .npy embedding stay (same
            # tombstone spirit as dropped/archived), so the voice keeps its
            # global number and resurfaces on its own the moment assign()
            # hears it in a new meeting. Reclaiming the entry and sample file
            # is an explicit GC decision, never a side effect of a poll.
            continue
        unknown_list.append({"uid": uid, "display": unknowns.display(uid),
                             "archived": bool(meta.get("archived")),
                             "meetings": mts,
                             # two kinds of "this may already be someone": a
                             # near miss against another unknown (phase 4 wrote
                             # it when the cluster minted) and a near miss
                             # against an enrolled person (the margin rule's
                             # nineteen). The drawer offers a Link for the
                             # first and a prefilled Name for the second.
                             "suggested": _suggested(meta, reg["speakers"]),
                             "looks_like": _unknown_looks_like(
                                 uid, meta, _prints_once)})
    # sources resolved through each sample's meeting id: a rename whose
    # registry chase failed used to leave a source naming a folder that no
    # longer exists, and its ▶ dead. The id still points at the meeting.
    enrolled = [{"name": n, "samples": meta.get("n_samples", 1),
                 "sources": [s for s in identify.resolved_sources(meta) if s and s != "?"]}
                for n, meta in sorted(identify.load_registry().items(),
                                      key=lambda t: t[0].lower())]  # by first name;
    # unknowns render in their own section below, so they stay at the bottom
    battery = ""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
        mo = re.search(r"(\d+)%.*?(discharging|charging|charged|AC attached)", out)
        if mo:
            battery = f"{mo.group(1)}% {mo.group(2)}"
    except Exception:
        pass
    procs = control.snapshot()
    running = bool(procs["pids"])
    # a queued relabel: `relabel.py` writes this flag when a CLI pass loses the
    # lock to another one, and run_batch's end-of-run pass can leave it too. The
    # poll used to respawn `relabel --all` on a 60s cooldown and hope. Now the
    # flag is CONSUMED once -- unlinked, then handed to the queue, which
    # coalesces it with anything already waiting -- so the follow-up can neither
    # be dropped nor run twice. With no queue (CLI-only mode) the flag is left
    # exactly where it is, for a panel that does have one.
    pending = config.PROJECT_DIR / "relabel_pending.flag"
    if RELABEL_QUEUE is not None and pending.exists():
        pending.unlink(missing_ok=True)
        RELABEL_QUEUE.submit_all("queued follow-up")
    if not running:
        _kick_jobs()  # self-heal: idle with queued jobs -> start the next one
        # the duplicate-transcript scan reads the whole library, so it waits for
        # an idle machine and runs in its own thread; the poll itself only ever
        # reads the cached answer
        _kick_dupe_scan()
    active = st.get("active", {}) if running else {}
    n_active = max(1, len(active))
    active_out, active_eta_sum = {}, 0.0
    for name, entry in active.items():
        det = status.progress_detail(entry, n_active)
        e = dict(entry)
        if det is not None:
            e["pct"] = round(det["pct"] * 100)
            e["eta_sec"] = round(det["eta"])
            # past the stage's estimate: the row and the pill say "running
            # long · +Xm" instead of counting down, and eta_sec is the tail
            if det["over"] is not None:
                e["over_sec"] = round(det["over"])
            active_eta_sum += det["eta"]
        bd = status.stage_breakdown(entry, n_active)
        if bd:
            e["stages"] = bd
        active_out[name] = e
    overall_eta = None
    if running:
        pend_secs = 0.0
        pend_names = set(st.get("pending", []))
        # a run into the library writes a description after each file: one
        # summary for every waiting file, and for every active file whose
        # summary has not started (the summarizing stage counts down itself)
        per_summary = status.summary_estimate(st) if st.get("summarizes") else 0.0
        for f in queue:
            if f["name"] in pend_names and f.get("est_min"):
                pend_secs += f["est_min"] * 60 + per_summary
        pend_secs += per_summary * sum(1 for e in active.values()
                                       if e.get("stage") != "summarizing")
        overall_eta = round((active_eta_sum + pend_secs) / n_active) if (active_eta_sum or pend_secs) else None
    rec_state = _recording_state()  # resolved once: reused by the live banner AND the timeline
    order = _run_order(queue, st.get("pending", []) if running else [],
                       sweep=not running or bool(st.get("sweep")))
    timeline, tray = _timeline_tray(st, queue, meetings, active_out, unknown_list,
                                    rec_state, order)
    active_bases = {e.get("base") for e in active_out.values() if e.get("base")}
    needs_you = _needs_you(meetings, active_bases, unknown_list)
    # the meeting rows the list holds carry their place in it, so the shell's
    # Needs you section is exactly this list and nothing else
    ny_rows = {x["target"]: (i, x) for i, x in enumerate(needs_you) if x["kind"] != "voice"}
    for r in timeline:
        hit = ny_rows.get(r.get("id"))
        if hit and r.get("state") in ("ready", "needs_name"):
            r["needs_you"], r["needs_you_rank"] = hit[1]["kind"], hit[0]
            r["needs_you_detail"] = hit[1]["detail"]
    relabel_jobs = (RELABEL_QUEUE.snapshot() if RELABEL_QUEUE is not None
                    else jobqueue.empty_snapshot())
    return {"running": running,
            "mem_mb": procs["mem_mb"],
            "active": active_out,
            "overall_eta_sec": overall_eta,
            "pending": st.get("pending", []) if running else [],
            "queued_jobs": [{"at": j.get("at"), "label": j.get("label") or "run",
                             "strict": j.get("strict"), "verify": j.get("verify")}
                            for j in jobs.items()],
            "recent": st.get("recent", [])[:8],
            "paused": control.is_paused(),
            # NOT the raw status key: that outlives the capture until finalize()
            # completes, so the panel kept showing "Recording" after the recorder
            # had already stopped. Same source of truth the menu bar uses, with
            # the captured-seconds clock resolved server-side so both readouts
            # agree and neither has to re-derive the paused-span arithmetic.
            "recording": rec_state,
            "recorder_note": status.recorder_note(),  # expires a success on its own
            "queue": queue, "meetings": _with_groups(meetings),
            # the One Timeline: one row per meeting/file, changing state in place
            # (recording -> waiting/held -> processing -> needs_name -> ready |
            # failed), newest first, archived excluded; plus a ranked tray of
            # what needs the user. Both are strictly additive — every key above
            # keeps its shape so the current panel keeps working unchanged.
            "timeline": timeline, "tray": tray,
            # Needs you: the one short list, computed on read (_needs_you)
            "needs_you": needs_you,
            "archived_count": len(config.archived_bases(dst_dir)),
            "enrolled": enrolled, "unknowns": unknown_list,
            "max_samples": identify.MAX_SAMPLES,
            "schedule": read_schedule(), "model": current_model(),
            # the second engine a verify run would use with the current
            # engine, or None: the Activity page hides Verify while it is None
            "verify_engine": _verify_engine(),
            "asr_choices": ASR_CHOICES, "battery": battery,
            "cloud_keys": _cloud_key_status(),
            "paths": {"source": str(src_dir), "dest": str(dst_dir),
                      "source_exists": _dir_present(src_dir)},
            "punctuate": _env_file_get()[0].get("STT_PUNCTUATE", "1") == "1",
            "mic_speaker": config.mic_speaker(),
            "recorder_ready": recorder.available(),
            "rates": rates.summary(),
            "relabel_pending": (config.PROJECT_DIR / "relabel_pending.flag").exists(),
            # the relabel queue: what is running, what is waiting behind it, and
            # how the last job ended. The pill reads jobs.running for
            # "naming: 3 of 7"; see gui/jobqueue.py for the shape.
            "jobs": relabel_jobs,
            # a relabel ACTUALLY running right now (naming a voice starts one).
            # relabel_pending only ever meant "a second pass is queued behind a
            # first", so an unblocked relabel -- the normal case after a save --
            # gave the panel no signal at all and the names appeared out of
            # nowhere minutes later. Read-only probe (see control.relabel_running)
            # OR our own worker, which is in-process and therefore does not have
            # to be discovered through the marker at all.
            "relabel_running": control.relabel_running() or bool(relabel_jobs["running"]),
            # Voice Memos sync, for the /next Settings page: whether it is on
            # and the go-live day. Read only; nothing here touches the library.
            "voicememos": _voicememos_state(),
            "llm_available": summarize.available(),
            "llm_backend": summarize.llm_backend(),
            "llm_backends": {b: summarize.backend_available(b)
                             for b in summarize.LLM_BACKENDS}}


def _voicememos_state() -> dict:
    """The sync (on or off, its go-live day) and the delete after ingest
    (on or off, the memos it moved lately, the manual deletes waiting, and
    a stop), for Settings and Activity."""
    try:
        from stt import voicememos
        return {"enabled": voicememos.enabled(), "since": voicememos.go_live(),
                **voicememos.delete_panel_state()}
    except Exception:
        return {"enabled": None, "since": None, "delete": None, "removed": [],
                "manual": 0, "halted": False}


def _osascript(script: str, timeout=120, args=None):
    # `args` are handed to osascript as `on run argv` items (after `--`), never
    # spliced into the source string, so client-supplied text can't be parsed
    # as AppleScript/shell code.
    cmd = ["/usr/bin/osascript", "-e", script]
    if args:
        cmd += ["--", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def pick_folder(prompt: str):
    # prompt is client-supplied and MUST NOT be interpolated into the script —
    # a `"` would close the string literal and let the rest run as code (e.g.
    # `do shell script "rm -rf …"`). Pass it as an inert argv item instead.
    code, out = _osascript(
        'on run argv\n'
        'POSIX path of (choose folder with prompt (item 1 of argv))\n'
        'end run',
        args=[prompt])
    return None if code != 0 else out.rstrip("/")


def pick_files():
    code, out = _osascript(
        'set fs to choose file with prompt "Choose audio or video recordings" '
        'with multiple selections allowed\n'
        'set res to ""\nrepeat with f in fs\n'
        'set res to res & POSIX path of f & linefeed\nend repeat\nreturn res')
    if code != 0:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def _merge_warning(dst: str, rows, prints: dict, exclude=()):
    """The need_confirm reply when the voice rows being folded into `dst`
    do not match its saved samples (identify.sample_check), else None.
    `prints` is read once by the caller, not once per row."""
    warns = [w for w in (identify.sample_check(dst, r, exclude=exclude, prints=prints)
                         for r in (rows if rows is not None else [])) if w]
    if not warns:
        return None
    w = min(warns, key=lambda x: x["own"])
    msg = f"This voice matches {dst}'s saved samples at only {w['own']:.2f}"
    if w["cross_name"] is not None and w["cross"] > w["own"]:
        msg += f" — and matches {w['cross_name']} better ({w['cross']:.2f})"
    msg += ". It may be the wrong person. Confirm to merge anyway."
    return {"ok": False, "warn": "suspect_sample", "need_confirm": True,
            "error": msg, **w}


def set_folder(which: str, path: str, confirm: bool = False):
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"ok": False, "error": f"not a folder: {p}"}
    if which != "source" and not confirm:
        # a transcripts folder that holds other files, or flat meetings the
        # migration would move, is adopted only after the owner confirms
        warn = config.adopt_folder_warning(p)
        if warn:
            return {"need_confirm": True, "which": which, "path": str(p), "warn": warn}
    key = "STT_ICLOUD_DIR" if which == "source" else "STT_MEETINGS_DIR"
    _env_file_set({key: str(p)})
    # No os.environ write: every child reads stt.env (run.sh, and the
    # batch's --source and --dest defaults). run.sh keeps a variable the
    # shell already set, so a value written here went stale when stt.env
    # changed later, and a spawned batch wrote into the old folder.
    if which == "source":
        config.ICLOUD_DIR = p
        # the agent's WatchPaths must follow the new source folder
        for pl in (AGENT,):
            if pl.exists():
                d = plistlib.loads(pl.read_bytes())
                d["WatchPaths"] = _watch_paths()
                pl.write_bytes(plistlib.dumps(d))
        if AGENT.exists():
            _agent_reload()  # same bootout+bootstrap; skipped under STT_HOME
    else:
        config.MEETINGS_DIR = p
        p.mkdir(parents=True, exist_ok=True)
        try:
            config.migrate_flat_meetings(p)  # a newly-picked folder may hold flat-layout meetings
        except Exception:
            pass
    return {"ok": True, "path": str(p)}


def _meeting_audio(base: str):
    return config.meeting_audio(base)


def _listen_engines() -> list:
    """The second listen's engine menu, the default first: [{"id", "label",
    "note"}] in stt.retranscribe.offered() order. Local engines only."""
    from stt import retranscribe
    return [{"id": e, "label": e.capitalize(), "note": "Runs on this Mac."}
            for e in retranscribe.offered()]


def _cloud_key_status() -> dict:
    """{provider: bool} — key presence only; actual keys never reach the page.
    'anthropic' is the assistant (summaries/Ask), not a transcription engine."""
    try:
        from stt import asr_cloud, summarize
        return {**{prov: asr_cloud.available(prov) for prov in asr_cloud.PROVIDERS},
                "anthropic": summarize.backend_available("anthropic")}
    except Exception:
        return {}


def _snippet_for(meeting: str, speaker_key: str, secs: float = 30.0):
    """Extract an audio snippet of `speaker_key` (display, id, name, or uid),
    up to `secs` seconds of their longest turn — never past the turn's end, so
    a longer request can't bleed into another person's voice mid-identification.
    Locating the right stretch — including the voiceprint fallback for people
    named before their transcripts were relabeled — lives in stt.review."""
    clips = review.find_voice_clips(speaker_key, meeting or None, n=1)
    if not clips:
        return None
    c = clips[0]
    base, start = c["base"], c["start"]
    dur = min(max(2.0, min(45.0, secs)), c["dur"])
    audio_f = _meeting_audio(base)
    if audio_f is None:
        return None
    # a UNIQUE path per request: a single fixed snippet.wav lets two concurrent
    # /api/snippet extractions overwrite each other and return the wrong voice.
    import tempfile
    work = config.PROJECT_DIR / "work"
    work.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="snippet_", suffix=".wav", dir=str(work))
    os.close(fd)
    out = Path(tmp)
    from stt.audio import FFMPEG
    subprocess.run([FFMPEG, "-y", "-ss", str(start), "-t", str(max(2.0, dur)),
                    "-i", str(audio_f), "-ar", "22050", "-ac", "1", str(out)],
                   check=True, capture_output=True)
    return out


def _relabel_now(base: str) -> bool:
    """Relabel ONE meeting here and now, synchronously, under the same
    single-flight guard a spawned pass takes. True when this call did the work.

    A meeting-local name spawns `relabel <base>`, which finishes in well under
    one 2s poll -- so no poll ever observed relabel_running, the panel's
    falling-edge rebuild never fired, and the transcript kept showing "Voice 1"
    with its "?" chip after a save the server had already applied. Doing the
    work inside the request lets the response mean "applied", and the client
    reloads against the new roster.

    False means another relabel already owns the guard. The caller then falls
    back to spawning and SAYS so, rather than blocking the request behind a
    library-wide pass.
    """
    import fcntl

    try:
        fd = open(control.relabel_lock_path(), "w")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            from relabel import relabel_one
            with control.relabel_marker():
                relabel_one(base)
        except Exception:
            # the NAME is already stored; a relabel that failed here is picked
            # up by the next pass, and must never turn a successful save into
            # a 500 carrying the server's own traceback
            pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()
    return True


def _spawn(args):
    # `with` so the parent releases its copy of the log fd once the child has
    # its own dup — the long-lived panel process otherwise leaks one fd per spawn.
    with open(config.PROJECT_DIR / "logs" / "spawned.log", "a") as log:
        log.write(f"\n--- {' '.join(str(a) for a in args)}\n")
        log.flush()
        return subprocess.Popen(args, cwd=str(config.PROJECT_DIR),
                                stdout=log, stderr=log, start_new_session=True)


def _relabel_all_background(reason: str):
    """A whole-library pass, on the queue when there is one and as a spawned
    `relabel --all` when there is not (the CLI-only panel mode)."""
    if RELABEL_QUEUE is not None:
        RELABEL_QUEUE.submit_all(reason)
    else:
        _spawn([str(RUN_SH), "relabel", "--all"])


def _relabel_bases_background(bases, reason: str):
    """A targeted pass over `bases`.

    The queue owns this now: one worker thread inside this process, with the
    models already warm, coalescing back-to-back identity changes into one job
    (see gui/jobqueue.py). The spawn below is the FALLBACK for a process with
    no queue -- `python -m gui.server` without start_server, and tests that
    drive Handler directly -- where dropping the work would mean a save that
    silently never applied."""
    if RELABEL_QUEUE is not None:
        RELABEL_QUEUE.submit(bases, reason)
        return
    d = config.PROJECT_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    # a file, not argv: a rename or a merge can legitimately target most of
    # the library, and meeting names are long
    fd, path = tempfile.mkstemp(prefix="relabel_targets_", suffix=".txt",
                                dir=str(d))
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(bases) + "\n")
    _spawn([str(RUN_SH), "relabel", "--bases-from", path])


def _relabel_targets(vector=None, names=(), uids=(), first=None,
                     reason="identity change", bases=()) -> dict:
    """Apply an identity change to exactly the meetings it can affect.

    Every naming, rename, merge and sample edit used to spawn `relabel --all`:
    99 meetings at ~7s each, and the meeting the user was looking at was not
    done first. stt.centroids.targets resolves the change to a short list; the
    meeting in the request (`first`) relabels HERE, inside the response, so the
    panel can reload against the new roster on its very next poll, and the rest
    go to the relabel queue as one job.

    `bases` are meetings the CALLER already knows the change reaches, unioned
    into that list: a retired unknown's own `meetings`, read before promote()
    deleted the uid. Nothing else finds a meeting whose row left the uid's
    MAX_SAMPLES window, since the uid is gone and its cluster may no longer
    score against the rows that were kept.

    Returns {"applied": the base done synchronously or None,
             "targets": the bases handed to the background pass}."""
    try:
        from stt import centroids
        # only meetings a relabel pass can rebuild, the same filter
        # centroids.targets applies to its own answer: a reference to a
        # meeting that was deleted, or never diarized, is not a target
        lib = set(config.meeting_bases())
        known = {str(x) for x in (bases or [])
                 if x and str(x) in lib
                 and config.meeting_file(str(x), ".diar.npz").exists()}
        rest = sorted(known | set(centroids.targets(
            vector=vector, names=names, uids=uids)))
    except Exception:
        # the index is a cache, never the source of truth: if it cannot be
        # built, the change still has to land everywhere it might matter, so
        # fall back to the full pass this helper replaced -- never to silence
        # after a save that SUCCEEDED
        if first and _relabel_now(first):
            _relabel_all_background(reason)
            return {"applied": first, "targets": ["--all"]}
        _relabel_all_background(reason)
        return {"applied": None, "targets": ["--all"]}
    applied = None
    if first:
        # the meeting in the request is ALWAYS done, index or no index: the
        # user is looking at it, and its own cluster is the one that changed
        if first in rest:
            rest.remove(first)
        if _relabel_now(first):
            applied = first
        else:
            # another pass owns the relabel lock: this meeting joins the
            # spawned list instead of blocking the request behind it
            rest.insert(0, first)
    if rest:
        _relabel_bases_background(rest, reason)
    return {"applied": applied, "targets": rest}


# ---------- who a voice already looks like ----------

# The audit of 2026-09-13 found nineteen unnamed clusters scoring 0.61 to 0.84
# against an enrolled print and sitting under the automatic margin -- one
# enrolled voice at 0.84 in a data review meeting among them. Refusing them
# automatically is right: a margin that narrow is how a voiceprint learns the
# wrong person. Showing the human nothing is not, and the same cluster came back
# unnamed every week. These two helpers are that evidence, and nothing more:
# they fill the name box, they never save.
LOOKS_LIKE_MAX = 3
_LOOKS_CACHE = {}


def _rows_of(vector):
    """One embedding or a stack of them, as a list of rows."""
    if vector is None:
        return []
    if getattr(vector, "ndim", 1) > 1:
        return list(vector)
    return [vector]


def _looks_like(vector, prints=None, limit=LOOKS_LIKE_MAX) -> list:
    """[{name, score}] for the enrolled people this voice resembles at or above
    unknowns.MATCH_MIN, best first, at most `limit` of them."""
    rows = _rows_of(vector)
    if not rows:
        return []
    if prints is None:
        prints = identify.load_voiceprints()
    out = []
    for name, stack in prints.items():
        best = max((identify.score_against(r, stack) for r in rows), default=-1.0)
        if best >= unknowns.MATCH_MIN:
            out.append({"name": name, "score": round(float(best), 3)})
    # best first, then by name: two equal scores must not swap places between
    # two polls of a list the user is reading
    out.sort(key=lambda d: (-d["score"], d["name"]))
    return out[:limit]


def _unknown_looks_like(uid, meta, prints) -> list:
    """`_looks_like` for one unknown voice, cached on the two files that can
    change the answer: its own sample stack and the enrolled registry.

    The score is an all-vs-all over every enrolled print, and the panel polls
    every two seconds, so recomputing per poll is the entire cost of this
    feature. The mtimes make it once per actual change instead. `prints` is a
    zero-argument callable so a poll where every unknown is cached reads no
    voiceprint files at all."""
    sample = config.VOICEPRINTS_DIR / str(meta.get("file") or "")
    try:
        key = (str(sample), sample.stat().st_mtime,
               identify._registry_path().stat().st_mtime)
    except OSError:
        return []   # no sample stack, or nobody enrolled: nothing to resemble
    hit = _LOOKS_CACHE.get(uid)
    if hit is not None and hit[0] == key:
        return hit[1]
    out = _looks_like(unknowns.samples_of(uid), prints())
    _LOOKS_CACHE[uid] = (key, out)
    return out


def _suggested(meta, speakers) -> list:
    """This unknown's near-miss candidates, as the drawer renders them.

    Phase 4 writes `suggested: [{uid, score}]` when a cluster misses the link
    bar and mints its own number instead. A uid that has since been named,
    merged or dropped is gone from the registry, and offering a Link that
    cannot land is worse than offering nothing."""
    out = []
    for s in (meta.get("suggested") or []):
        if not isinstance(s, dict):
            continue
        uid = str(s.get("uid") or "")
        ent = speakers.get(uid)
        if not isinstance(ent, dict) or ent.get("dropped"):
            continue
        try:
            score = round(float(s.get("score") or 0.0), 3)
        except (TypeError, ValueError):
            continue
        out.append({"uid": uid, "display": unknowns.display(uid), "score": score})
    return out


# ---------- request validation ----------
# One layer between the client and every handler. A request the client got
# wrong -- a body that is not one JSON object, a missing field, a field of the
# wrong type -- answers HTTP 400 with a plain message that names the field.
# A planned failure whose message was written for a person (stt.errors.
# UserError: "Google Chrome not found -- export Word instead") answers with
# that message. Anything else that goes wrong answers 500 with one fixed
# sentence, and the detail (route, exception, traceback) goes to the panel's
# log instead: exception text carries server paths and Python internals, and
# the panel shows the `error` text to the user as it is.

class BadRequest(Exception):
    """The client's mistake. Answered with HTTP `code` (400 unless given) and
    `msg` as the `error` text. `msg` is written for a person: it never holds
    exception text or a filesystem path."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.msg, self.code = msg, code


INTERNAL_ERROR = ("Something went wrong in the panel. The details are in the "
                  "panel log.")
# no JSON body the panel sends comes near this; a file goes to /api/upload
_BODY_MAX = 8 << 20
_REQUIRED = object()


def _no_constant(name):
    # Python's json reads NaN, Infinity and -Infinity, which are not JSON. A
    # NaN time compares as nothing (`end <= start` is False for it), and a
    # stored one writes a bare NaN token that no browser JSON.parse reads.
    raise ValueError(f"{name} is not a JSON number")


class Fields(dict):
    """A request's parameters with typed reads: the JSON object body of a
    POST, or the query string of a GET (`query=True`: every value arrives as
    text, and a number is parsed from it).

    It is still a dict, so `.get()` and `in` work as before, and `f["key"]`
    on a key the client left out raises BadRequest, not KeyError.

    Each reader takes `default`. Left out, the field is required. Given, a
    missing or null field reads as the default. A field that is present with
    the wrong type is always a 400: a mistake is never read as the default."""

    def __init__(self, data=(), query=False):
        super().__init__(data)
        self.query = query

    def __missing__(self, key):
        raise BadRequest(f"missing field '{key}'")

    def _value(self, key, default):
        """(absent, value): the default when the field is missing or null."""
        v = dict.get(self, key)
        if v is None:
            if default is _REQUIRED:
                raise BadRequest(f"missing field '{key}'")
            return True, default
        return False, v

    def text(self, key, default=_REQUIRED, max_len=None):
        absent, v = self._value(key, default)
        if absent:
            return v
        if not isinstance(v, str):
            raise BadRequest(f"'{key}' must be text")
        if max_len is not None and len(v) > max_len:
            raise BadRequest(f"'{key}' is too long ({max_len} characters at most)")
        return v

    def _range(self, key, v, lo, hi):
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            if lo is not None and hi is not None:
                raise BadRequest(f"'{key}' must be from {lo} to {hi}")
            if lo is not None:
                raise BadRequest(f"'{key}' must be {lo} or more")
            raise BadRequest(f"'{key}' must be {hi} or less")
        return v

    def number(self, key, default=_REQUIRED, lo=None, hi=None):
        """A finite number. NaN and the infinities are refused."""
        absent, v = self._value(key, default)
        if absent:
            return v
        if self.query and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                raise BadRequest(f"'{key}' must be a number") from None
        if (isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v)):
            raise BadRequest(f"'{key}' must be a number")
        return self._range(key, v, lo, hi)

    def integer(self, key, default=_REQUIRED, lo=None, hi=None):
        """A whole number. 3.0 reads as 3, and 3.5 is refused."""
        absent, v = self._value(key, default)
        if absent:
            return v
        if self.query and isinstance(v, str):
            try:
                v = int(v.strip())
            except ValueError:
                raise BadRequest(f"'{key}' must be a whole number") from None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int):
            raise BadRequest(f"'{key}' must be a whole number")
        return self._range(key, v, lo, hi)

    def flag(self, key, default=False):
        """A JSON true or false, and nothing else. The string "false" is
        truthy in Python: a delete that read it as a yes deleted the
        meeting."""
        absent, v = self._value(key, default)
        if absent:
            return v
        if not isinstance(v, bool):
            raise BadRequest(f"'{key}' must be true or false")
        return v

    def texts(self, key, default=_REQUIRED):
        """A list of text values. A bare string is refused: iterated, it is
        a list of its characters."""
        absent, v = self._value(key, default)
        if absent:
            return v
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise BadRequest(f"'{key}' must be a list of text values")
        return v

    def obj(self, key, default=_REQUIRED):
        absent, v = self._value(key, default)
        if absent:
            return v
        if not isinstance(v, dict):
            raise BadRequest(f"'{key}' must be an object")
        return v


def parse_body(raw: bytes) -> Fields:
    """A POST body as Fields. An empty body is {}: several buttons post none.
    Anything that is not one JSON object is the client's mistake."""
    if not raw.strip():
        return Fields()
    try:
        obj = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise BadRequest("the request body is not valid JSON") from None
    if not isinstance(obj, dict):
        raise BadRequest("the request body must be a JSON object")
    return Fields(obj)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Fields:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise BadRequest("Content-Length must be a number") from None
        if n < 0:
            raise BadRequest("Content-Length must be a number")
        if n > _BODY_MAX:
            raise BadRequest("the request body is too large", 413)
        return parse_body(self.rfile.read(n) if n else b"")

    def _failed(self, route):
        """An error nobody planned for. The route, the exception and its
        traceback go to the panel log (stderr, which launchd writes to
        logs/gui.err.log). The client gets INTERNAL_ERROR and no detail."""
        print(f"panel: {self.command} {route} failed", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        try:
            self._json({"error": INTERNAL_ERROR}, 500)
        except OSError:
            pass    # the client is gone

    def _serve_audio(self, audio_f):
        """Serve an audio file with byte-range support (a <audio> element seeks
        with Range requests). Shared by the meeting player and the queue preview
        so the range/416 edge cases have exactly one implementation."""
        data = audio_f.read_bytes()
        ctype = {"m4a": "audio/mp4", "mp4": "audio/mp4", "wav": "audio/wav",
                 "mp3": "audio/mpeg", "aiff": "audio/aiff", "caf": "audio/x-caf",
                 "flac": "audio/flac", "aac": "audio/aac"}.get(
                     audio_f.suffix[1:].lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        mo = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if mo and (mo.group(1) or mo.group(2)):
            total = len(data)
            if not mo.group(1):
                # suffix form: bytes=-N means the LAST N bytes, not 0..N
                n = int(mo.group(2))
                a, z = max(0, total - n), total - 1
            else:
                a = int(mo.group(1))
                z = int(mo.group(2)) if mo.group(2) else total - 1
                z = min(z, total - 1)
            if a > z or a >= total:  # unsatisfiable range
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = data[a:z + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{z}/{total}")
        else:
            chunk = data
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def _upload(self, q):
        """POST /api/upload?name=<filename> — land one audio file in the primary
        watched folder (source_dir), where the folder watcher/queue picks it up
        like any other drop-in (held/pause semantics unchanged: with automation
        off it simply shows as waiting). The raw request body IS the file — this
        server has no multipart parser — and the filename travels as the `name`
        query param. The body streams in chunks to a dot-prefixed .part tmp in
        the SAME directory and is os.replace'd into its final name, so the
        watcher (which skips dotfiles and never matches .part) cannot see a
        partial file, and a broken upload leaves nothing behind."""
        final = _upload_name(q.get("name"))
        if final is None:
            self._json({"error": "filename must end in one of: "
                                 + ", ".join(sorted(config.AUDIO_EXTS))}, 400)
            return
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json({"error": "Content-Length required"}, 400)
            return
        if n <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        if n > _UPLOAD_MAX:
            self._json({"error": "file too large (2 GB max)"}, 413)
            return
        import tempfile
        dest = config.source_dir()
        dest.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=str(dest))
        tmp = Path(tmp)
        try:
            remaining = n
            with os.fdopen(fd, "wb") as out:
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:  # client vanished mid-body
                        tmp.unlink(missing_ok=True)
                        try:  # the connection is likely already gone
                            self._json({"error": "connection closed mid-upload"}, 400)
                        except OSError:
                            pass
                        return
                    out.write(chunk)
                    remaining -= len(chunk)
            with _upload_lock:
                target = _upload_target(dest, final)
                os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self._json({"ok": True, "name": target.name})

    def _bulk_one(self, action, bs, value, confirmed) -> dict:
        """ONE /api/bulk item. Split out of the loop so the loop can guard each
        item on its own: every op below gates on live/archived MEMBERSHIP
        internally before it builds a path, so an unknown or traversal base is
        refused per item and never applied."""
        from stt import archive, summarize
        if action == "category":
            return summarize.set_meeting_category(bs, value or "")
        if action == "date":
            return summarize.set_meeting_date(bs, value or "")
        if action == "rename":
            # each keeps its OWN date stamp, so renaming a run of recurring
            # meetings to one name still leaves them distinct
            return summarize.rename_meeting(bs, value or "")
        if action == "accept":
            # a bulk Keep settles the title and claims nothing more: nobody
            # opened these meetings, so `reviewed` stays as it is (plan row 17)
            return summarize.apply_meeting_edits(bs, title_state="confirmed")
        if action == "archive":
            return archive.archive_meeting(bs)
        if action == "restore":
            return archive.restore_meeting(bs)
        if action in ("delete", "drop_audio") and not confirmed:
            return {"ok": False, "error": "missing confirmation"}
        if action == "delete":
            return archive.delete_meeting(bs)
        if action == "drop_audio":
            return archive.drop_audio(bs)
        return {"ok": False, "error": f"unknown action '{action}'"}

    def _require_base(self, base) -> bool:
        """Validate `base` against real meeting basenames; on failure, send the
        400 and tell the caller to stop. Every handler that turns a client-
        supplied `base` into a file path must gate on this first."""
        if _known_base(base):
            return True
        self._json({"error": "unknown meeting"}, 400)
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = Fields(urllib.parse.parse_qsl(u.query), query=True)
        if not (_host_allowed(self.headers) and _origin_allowed(self.headers)):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            if u.path in ("/", "/next"):
                # `/` serves the panel. `/next`, the Phase 4C preview address,
                # serves the same page, so a bookmark to it keeps working. The
                # retired `ui` query param is ignored, and no GET here writes
                # anything.
                body = _next_html().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/favicon.ico":
                # the page names its own icon (an inline SVG in next.html).
                # A client that asks here anyway gets an empty answer, not
                # the 404 that was the only console error on every load
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/classic":
                # The old page is deleted. A bookmark to it lands on the panel
                # instead of a 404. The Location is relative, and a browser
                # keeps the #m/<base> fragment across the redirect, which the
                # panel reads as the same deep link.
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/api/state":
                self._json(gather_state())
            elif u.path == "/api/dupes":
                # the duplicate-transcript review list, fetched when its drawer
                # section opens. Deliberately NOT part of /api/state: two sides
                # of every pair resolved on a 2s poll would be pure waste for
                # the 99% of polls where nobody is looking at it.
                self._json({"pairs": _dupe_pair_rows(),
                            "scanning": bool(_dupe_worker["busy"]),
                            # a cold cache and a scanned-and-empty library both
                            # answer pairs:[]; only this tells them apart, so
                            # the drawer stops asserting "no duplicates found"
                            # about a comparison that never ran
                            "scanned": dupes.cache_scanned(),
                            "truncated": dupes.cache_truncated(),
                            "threshold": dupes.THRESHOLD})
            elif u.path == "/api/snippet":
                meeting, speaker = q.get("meeting", ""), q.text("speaker")
                if meeting and not _known_base(meeting):
                    # a stale reference (a meeting renamed/deleted before the
                    # registries tracked renames) must not kill playback — the
                    # client string is DISCARDED, never used as a path, and
                    # find_voice_clip searches the real library instead
                    meeting = ""
                # 30 s is the default sample length for ▶ playback
                f = _snippet_for(meeting, speaker, secs=q.number("secs", 30.0))
                if f is None:
                    self._json({"error": "no snippet"}, 404)
                    return
                data = f.read_bytes()
                try:  # per-request temp file; drop it once read
                    f.unlink()
                except OSError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/history":
                # the complete processing history (results.jsonl merged with
                # the status ring); filtering happens client-side
                self._json({"results": status.history()})
            elif u.path == "/api/archived":
                # archived meetings, newest first — small list, parsed on demand
                items = []
                for b in config.archived_bases():
                    d = {}
                    try:
                        d = json.loads(
                            (config.archive_dir() / b / f"{b}.json").read_text())
                    except (OSError, ValueError):
                        pass
                    items.append({"base": b, "title": _display_title(b),
                                  "date": d.get("date") or dates.meeting_date(b) or "",
                                  "category": d.get("category"),
                                  "minutes": round(d.get("duration_sec", 0) / 60),
                                  "seconds": round(float(d.get("duration_sec") or 0))})
                items.sort(key=lambda r: r["date"] or "", reverse=True)
                self._json({"items": items})
            elif u.path == "/api/voice_clips":
                # for the "Who is this?" dialog: this unknown's longest turn in
                # EACH meeting it was heard in — meeting names come from the
                # SERVER's registry, never the client, so no base gate needed
                uid = q.get("speaker", "")
                refs = unknowns.resolved_meetings(
                    unknowns.load()["speakers"].get(uid, {}))
                clips = []
                for m in [m for m in refs if _known_base(m)][:5]:
                    cs = review.find_voice_clips(uid, m, n=1)
                    if cs:
                        c = cs[0]
                        clips.append({"meeting": m, "start": c["start"],
                                      "dur": round(c["dur"], 1),
                                      "index": c["index"]})
                out = {"clips": clips}
                if refs and not clips:
                    archived = set(config.archived_bases())
                    live = [m for m in refs if _known_base(m)]
                    if not live and not any(m in archived for m in refs):
                        # every reference is DEAD — not live, not archived: the
                        # meetings this voice was heard in were deleted, so no
                        # audio exists to cut a clip from. Say WHY the list is
                        # empty instead of handing the dialog a blank player.
                        out["reason"] = "sources_deleted"
                    elif not live:
                        # every reference is ARCHIVED. The audio still exists,
                        # but the clip endpoints gate on LIVE membership, so
                        # the client's bare-audio fallback 404s and the user
                        # sees a silently broken player. Name the way out.
                        out["reason"] = "sources_archived"
                self._json(out)
            elif u.path == "/api/voice_lines":
                # WHAT this voice actually said, so the naming panel can be
                # answered by READING instead of listening. Half the unnamed
                # voices in a meeting are grumbling, a cough, or a stray "mhm",
                # and that is obvious from the text in a second.
                base = q.get("base", "")
                if not self._require_base(base):
                    return
                j = config.meeting_file(base, ".json")
                if not j.exists():
                    self._json({"error": "no transcript"}, 404)
                    return
                d = json.loads(j.read_text())
                who = q.get("speaker", "")
                # the caller may hold either half of the pair: the timeline row
                # and the legend know the cluster id, other surfaces only the
                # display label. Resolve display -> id first so both work.
                sid = next((s["id"] for s in d.get("speakers", [])
                            if s.get("display") == who), who)
                mine = [s for s in d.get("segments", [])
                        if s.get("speaker") == sid and s.get("text", "").strip()]
                # LONGEST FIRST, not chronological: the longest turns are the
                # evidence. Noise clusters are all sub-second fragments, and a
                # real person shows one full sentence in the first row. Reading
                # order would bury that under eight "mm-hmm"s.
                top = sorted(mine, key=lambda s: (s.get("end", 0) - s.get("start", 0)),
                             reverse=True)[:8]
                self._json({
                    "lines": [{"start": round(s.get("start", 0), 1),
                               "dur": round(s.get("end", 0) - s.get("start", 0), 1),
                               "text": s.get("text", "").strip()} for s in top],
                    "n": len(mine),
                    "talk_secs": round(sum(s.get("end", 0) - s.get("start", 0)
                                           for s in mine), 1)})
            elif u.path == "/api/transcript":
                base = q.text("base")
                if not self._require_base(base):
                    return
                j = config.meeting_file(base, ".json")
                if not j.exists():
                    self._json({"error": "no transcript"}, 404)
                    return
                d = json.loads(j.read_text())
                dism = {str(x) for x in (d.get("dismissed_voices") or [])}
                roster = output.roster_labels(d.get("speakers", []))
                segs = [{"index": i, "start": s["start"], "end": s["end"],
                         "speaker": s.get("speaker"),
                         "who": output.line_label(s, roster),
                         "text": s.get("text", ""),
                         "edited": bool(s.get("text_edited") or s.get("reviewed")),
                         "flags": s.get("flags", [])}
                        for i, s in enumerate(d.get("segments", []))
                        if s.get("text", "").strip()]
                self._json({"base": base, "strict": d.get("strict", False),
                            # the engines a second listen on a line may use
                            "listen_engines": _listen_engines(),
                            # the save stamp of exactly these bytes: the meeting
                            # page records it so the next poll's row stamp is
                            # compared against what is really on screen
                            "generated_at": d.get("generated_at"),
                            "duration_sec": d.get("duration_sec", 0),
                            # a silent recording: the transcript section says
                            # so instead of reporting an error
                            "no_speech": bool(d.get("no_speech")),
                            # the audio-may-be-lost note stays on the meeting
                            # after the Needs you row leaves (plan row 10)
                            "audio_short_by": (None if d.get("no_speech") else
                                               _audio_short_by({
                                                   "audio": config.meeting_audio(base),
                                                   "duration_sec": d.get("duration_sec")})),
                            "speakers": [s["display"] for s in d.get("speakers", [])],
                            # `dismissed` is what lets the legend drop the "?"
                            # without pretending the voice was ever named
                            "speaker_options": [{"id": s["id"], "display": s["display"],
                                                 "named": bool(s.get("name")),
                                                 "local": bool(s.get("local")),
                                                 "dismissed": s["id"] in dism}
                                                for s in d.get("speakers", [])],
                            "people": sorted(identify.load_registry().keys()),
                            "segments": segs})
            elif u.path == "/api/audio":
                if not self._require_base(q.get("base")):
                    return
                audio_f = config.meeting_audio(q["base"])
                if audio_f is None:
                    self._json({"error": "no audio"}, 404)
                    return
                self._serve_audio(audio_f)
            elif u.path == "/api/queue_audio":
                # a file still WAITING in a watched folder — it has no meeting yet,
                # so there is no base to gate on. _queue_file resolves the name
                # against the watched folders themselves and refuses anything that
                # is not a real audio file sitting directly in one of them.
                f = _queue_file(q.get("name"))
                if f is None:
                    self._json({"error": "no such queued file"}, 400)
                    return
                self._serve_audio(f)
            elif u.path == "/api/search":
                # filters (plan Phase 4E) arrive as query parameters or
                # inline in q ("budget after:2026-10-01"). A bad value
                # answers with `error` and no hits.
                self._json(search.query(q.get("q", ""), filters=_search_filters(q)))
            elif u.path == "/api/search_meetings":
                # meetings whose title or summary matches, best first, for the
                # /next search palette's Titles group. Read-only, like
                # /api/search, and empty when the index cannot answer.
                self._json({"meetings": search.meetings(
                    q.get("q", ""), filters=_search_filters(q))})
            elif u.path == "/api/threads":
                # saved library Ask threads, newest first: id, first question,
                # scope, and count. No answer text in the list.
                from stt import ask
                self._json({"threads": ask.list_threads()})
            elif u.path == "/api/thread":
                from stt import ask
                t = ask.get_thread(q.get("id", ""))
                if t is None:
                    self._json({"error": "no such thread"}, 404)
                    return
                self._json(t)
            elif u.path == "/api/groups":
                # computed groups, largest first. Read only: a GET writes
                # nothing, so the use count is a separate POST /api/usage
                self._json({"groups": groups.current()})
            elif u.path == "/api/txt":
                if not self._require_base(q.get("base")):
                    return
                f = config.meeting_file(q["base"], ".txt")
                body = f.read_bytes() if f.exists() else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/review":
                if not self._require_base(q.get("base")):
                    return
                self._json(review.list_flagged(q["base"]))
            elif u.path == "/api/suggest":
                # Generating a title and summary WRITES the meeting .json, so
                # it is a POST (plan Phase 4C: GET no longer writes). A stale
                # caller still on GET gets a 405 and changes nothing.
                self._json({"error": "use POST /api/suggest with {base}"}, 405)
            elif u.path == "/api/check_updates":
                self._json(check_updates())
            elif u.path.startswith("/static/fonts/"):
                # The panel serves its own vendored UI font (Figtree, OFL --
                # see gui/static/fonts/OFL.txt and NOTICE.md), never a
                # CDN. STRICT basename allowlist built from the actual files:
                # the request must equal one of them exactly, so a traversal
                # name (encoded or not) can never resolve to a path.
                name = u.path[len("/static/fonts/"):]
                if name not in {p.name for p in _FONT_DIR.glob("*.woff2")}:
                    self._json({"error": "not found"}, 404)
                    return
                body = (_FONT_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(body)))
                # the filenames are stable, so a long-lived immutable cache
                self.send_header("Cache-Control",
                                 "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        except BadRequest as e:
            self._json({"error": e.msg}, e.code)
        except UserError as e:
            # planned, and its text is the help ("Google Chrome not found
            # -- export Word instead"): shown as it is
            self._json({"ok": False, "error": str(e)}, e.code)
        except Exception:
            self._failed(u.path)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not (_host_allowed(self.headers) and _origin_allowed(self.headers)):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            if u.path == "/api/upload":
                # raw-body upload — must branch BEFORE _body(), which would
                # buffer the whole file and try to parse it as JSON
                self._upload(Fields(urllib.parse.parse_qsl(u.query), query=True))
                return
            b = self._body()
            if u.path == "/api/run":
                # every run goes through the job queue: it starts immediately
                # when the machine is idle, and WAITS (visible in the panel)
                # when a batch is already running — a Redo is never lost.
                files = b.texts("files", [])
                paths = b.texts("paths", [])
                picked = files + [Path(p).name for p in paths]
                label = (", ".join(picked[:2])
                         + (f" +{len(picked)-2} more" if len(picked) > 2 else "")
                         ) if picked else "all new recordings"
                busy = bool(control.snapshot()["pids"])
                jobs.add({"files": files, "paths": paths, "force": b.flag("force"),
                          "strict": b.flag("strict"), "verify": b.flag("verify"),
                          "onetime": b.flag("onetime"),
                          "parallel": b.integer("parallel", 1, lo=1, hi=2),
                          "label": label})
                control.clear_stopping()  # an explicit new run overrides a recent Stop
                _jobs_kicked["at"] = 0.0  # a fresh click may bypass the cooldown
                _kick_jobs()
                self._json({"ok": True, "queued": busy})
            elif u.path == "/api/unqueue":
                self._json({"ok": jobs.remove(float(b.number("at")))})
            elif u.path == "/api/pick_folder":
                which = _folder_kind(b)
                p = pick_folder(b.text("prompt", "Choose a folder", max_len=200))
                if p is None:
                    self._json({"cancelled": True})
                else:
                    self._json(set_folder(which, p))
            elif u.path == "/api/set_folder":
                self._json(set_folder(_folder_kind(b), b.text("path"),
                                      b.flag("confirm")))
            elif u.path == "/api/pick_files":
                fs = pick_files()
                self._json({"cancelled": True} if fs is None else {"ok": True, "paths": fs})
            elif u.path == "/api/stop":
                # blocks up to ~8s: group-SIGTERM, verify, escalate to SIGKILL
                self._json(control.stop_run())
            elif u.path == "/api/pause":
                control.pause()
                self._json({"ok": True})
            elif u.path == "/api/resume":
                control.resume()
                self._json({"ok": True})
            elif u.path == "/api/schedule":
                write_schedule(b.integer("hour", lo=0, hi=23),
                               b.integer("minute", lo=0, hi=59))
                self._json({"ok": True})
            elif u.path == "/api/model":
                choice = b.text("model")
                # only a model the settings UI actually offers. Any other string
                # went into STT_ASR_BACKEND verbatim, so a plain typo silently
                # corrupted the backend setting, and this was the route the
                # newline injection above travelled.
                if choice not in {c["id"] for c in ASR_CHOICES}:
                    self._json({"ok": False, "error": "Unknown model."})
                    return
                if choice.startswith("cloud:"):
                    from stt import asr_cloud
                    prov = asr_cloud.provider_from_backend(choice)
                    if not asr_cloud.available(prov):
                        self._json({"ok": False, "error": "Add this provider's API key first (Cloud keys… in Settings)."})
                        return
                _env_file_set({"STT_ASR_BACKEND": choice})
                self._json({"ok": True, "model": current_model()})
            elif u.path == "/api/name_preview":
                # What a save from the naming panel would DO, before it does
                # it: the meetings it reaches, and who this voice already
                # resembles. Read-only and best-effort -- the panel opens
                # without it and fills its quiet line in when this lands, so
                # every failure here answers with an empty preview, never an
                # error the user has to read.
                vec, uids, here = None, (), None
                if b.text("uid", ""):
                    vec = unknowns.samples_of(b["uid"])
                    if vec is None:
                        # already named, merged or forgotten: the save itself
                        # says so plainly, and a preview must not say it twice
                        self._json({"targets": [], "here": None, "looks_like": []})
                        return
                    uids = [b["uid"]]
                else:
                    if not self._require_base(b.get("meeting")):
                        return
                    from stt import diarcache
                    try:
                        _, _, cent_emb, _ = diarcache.load(
                            config.meeting_file(b["meeting"], ".diar.npz"))
                    except (OSError, ValueError, KeyError):
                        cent_emb = {}
                    vec = cent_emb.get(b.text("speaker", ""))
                    here = b["meeting"]
                try:
                    from stt import centroids
                    tgts = list(centroids.targets(vector=vec, uids=uids))
                except Exception:
                    # the index is a cache: a preview it cannot build is a
                    # missing line, never a blocked save
                    tgts = []
                # the meeting in hand is always a target of its own naming,
                # whatever the index says about its centroids
                if here and here not in tgts:
                    tgts = sorted(tgts + [here])
                self._json({"targets": tgts, "here": here,
                            "looks_like": _looks_like(vec)})
            elif u.path == "/api/name":
                name = b.text("name", "", max_len=200).strip()
                local, uid = b.flag("local"), b.text("uid", "")
                if names.has_unsafe(name):
                    # a newline split the .txt header and the roster, and a
                    # bidi control reorders how the name displays
                    self._json({"ok": False, "error": names.UNSAFE_NAME})
                    return
                # An empty name is the documented UNDO for a meeting-local
                # label (set_local_name drops the entry). It means nothing
                # anywhere else: an enrollment and a uid promotion have no
                # entry to clear, so an empty name there is simply a mistake.
                # The old bare `assert name` refused the undo outright, which
                # left a meeting-local label removable only by a full Redo.
                if not name and not (local and not uid):
                    self._json({"ok": False, "error": "Type a name first."})
                    return
                if name and not local:
                    if identify.reserved_name(name):
                        self._json({"ok": False, "error": identify.RESERVED_ERROR})
                        return
                    # the enrolled spelling, so a case variant names the
                    # existing person and relabels under the name she has
                    name = identify.canonical_name(name)
                confirm = b.flag("confirm")

                def _warn_payload(w):
                    # additive: ok stays boolean, error stays the inline text
                    # old clients render; `warn` powers the two-step confirm
                    msg = (f"This voice matches {name}'s saved samples at only "
                           f"{w['own']:.2f}")
                    if w["cross_name"] is not None and w["cross"] > w["own"]:
                        msg += (f" — and matches {w['cross_name']} better "
                                f"({w['cross']:.2f})")
                    msg += ". It may be the wrong person. Confirm to save anyway."
                    return {"ok": False, "warn": "suspect_sample",
                            "need_confirm": True, "error": msg, **w}
                # which meetings this naming can touch. Each branch below fills
                # in what it knows before falling through to the shared tail.
                tgt = {"vector": None, "names": [name], "uids": (), "first": None}
                if uid:
                    # the uid is GONE (already named, merged, or forgotten —
                    # a stale legend chip is the usual way this arrives). Say
                    # so, before anything else: with None samples the quality
                    # gate below silently iterated an empty list and promote()
                    # then returned False, which the panel showed as a generic
                    # "Could not save the name." on an already-successful save.
                    s = unknowns.samples_of(b["uid"])
                    if s is None:
                        self._json({"ok": False,
                                    "error": "That voice was already named or "
                                             "removed. Refresh and try again."})
                        return
                    if (unknowns.load()["speakers"].get(b["uid"]) or {}).get("dropped"):
                        # "Not a real speaker" keeps a tombstone; a stale tab
                        # or a direct POST must not name it back to life
                        self._json({"ok": False,
                                    "error": "That voice was removed as not a "
                                             "real speaker, so it cannot be "
                                             "named. Refresh the page."})
                        return
                    if not confirm:
                        # naming an unknown as an EXISTING person appends the
                        # unknown's samples to that stack — same quality bar
                        # as any other enrollment
                        warns = [w for w in (identify.sample_check(name, row)
                                             for row in s) if w]
                        if warns:
                            self._json(_warn_payload(min(warns, key=lambda w: w["own"])))
                            return
                    # the meeting page open under the panel, when the panel
                    # sends one: it relabels HERE, inside the response, as a
                    # cluster naming's meeting does, so the page the user is
                    # reading is done and not merely queued. Context only, never
                    # identity: the drawer names with no page open, and a page
                    # that went stale (renamed in another tab) must not block
                    # naming the voice. A base that is not a real meeting is
                    # dropped here and never reaches a file path, and so is
                    # one with no diarization cache: relabel skips it, so
                    # calling it applied would be the same false "here".
                    if (_known_base(b.get("meeting")) and config.meeting_file(
                            b["meeting"], ".diar.npz").exists()):
                        tgt["first"] = b["meeting"]
                    # promote() RETIRES the uid, and its `meetings` list with
                    # it. Read the list NOW. Only the newest MAX_SAMPLES rows
                    # survive in the stack, so an early meeting's cluster may
                    # no longer score against them, and nothing else finds
                    # that meeting afterwards: it kept a retired "Speaker N".
                    heard = unknowns.resolved_meetings(
                        unknowns.load()["speakers"].get(b["uid"]) or {})
                    if not unknowns.promote(b["uid"], name):
                        # samples existed a moment ago: another tab/relabel got
                        # there first. Still not a success, and still no relabel.
                        self._json({"ok": False,
                                    "error": "Could not save this voice. It may "
                                             "have been renamed already."})
                        return
                    # the samples it carried are this person's voiceprint as of
                    # this instant, so the stack finds the meetings that still
                    # score against them. The list read above covers the rest.
                    tgt["vector"] = identify.load_voiceprints().get(name)
                    tgt["uids"] = [b["uid"]]
                    tgt["bases"] = heard
                else:
                    if not self._require_base(b.get("meeting")):
                        return
                    if local:
                        # a transcript label for THIS meeting only: no floor
                        # check and no enrollment, because nothing is learned.
                        # The panel offers this after the floor refuses the
                        # real enrollment; re-saving the same cluster is the
                        # typo fix. Only this one meeting rebuilds.
                        # `speakers`: every cluster of ONE voice. The
                        # diarizer can split a person into two clusters with
                        # no uid between them, and a name saved for the first
                        # alone left the other counted as unnamed. ONE call
                        # checks every id and labels them all or none, and the
                        # meeting relabels once below. Without the list,
                        # `speaker` alone, as before.
                        from stt import summarize
                        res = summarize.set_local_name(
                            b["meeting"], _voice_ids(b), name)
                        if not res.get("ok"):
                            self._json(res)
                            return
                        # ONE meeting rebuilds, and it rebuilds HERE: the pass
                        # takes well under a poll interval, so a spawned one
                        # started and finished between polls and the open page
                        # never learned the name had landed.
                        if _relabel_now(b["meeting"]):
                            self._json({"ok": True, "local": True,
                                        "applied": True,
                                        "note": "this meeting has been relabeled"})
                        else:
                            # the lock is held right now: the queue runs this
                            # one meeting as soon as the pass in front frees it
                            _relabel_bases_background([b["meeting"]],
                                                      "meeting-local name")
                            self._json({"ok": True, "local": True,
                                        "applied": False,
                                        "note": "another relabel is running -- "
                                                "this meeting is renamed right "
                                                "after it"})
                        return
                    from stt import diarcache
                    # the meeting can be renamed, restamped or archived in the
                    # gap between the membership check above and these reads. An
                    # unguarded read answered with a bare 500 carrying the
                    # server's own filesystem path.
                    try:
                        raw_turns, _, cent_emb, _ = diarcache.load(
                            config.meeting_file(b["meeting"], ".diar.npz"))
                        mj = json.loads(config.meeting_file(
                            b["meeting"], ".json").read_text())
                    except (OSError, ValueError, KeyError):
                        self._json({"ok": False,
                                    "error": "That meeting changed while you "
                                             "were naming it. Refresh and try "
                                             "again."})
                        return
                    if b.text("speaker") not in cent_emb:
                        self._json({"ok": False,
                                    "error": "That speaker has no voice embedding "
                                             "in this meeting."})
                        return
                    # dismissed as "not a real speaker" HERE: a stale panel, a
                    # second tab, or a direct POST would otherwise enroll a real
                    # voiceprint for a voice the user just said is not a person,
                    # leaving the meeting both dismissed and named at once
                    if b["speaker"] in {str(x) for x
                                        in (mj.get("dismissed_voices") or [])}:
                        self._json({"ok": False,
                                    "error": "That voice is dismissed as not a "
                                             "real speaker in this meeting. "
                                             "Restore it first, then name it."})
                        return
                    # quality floor: a cluster with seconds of speech cannot
                    # identify anyone — refuse ENROLLMENT outright (no confirm
                    # override). Pooled across the cluster's same-meeting split
                    # twins, exactly like assign()'s minting gate: a person the
                    # diarizer split three ways must not be refused because
                    # each fragment alone looks like a noise floor. `floor`
                    # tells the panel the meeting-only fallback applies.
                    unnamed = [s["id"] for s in mj.get("speakers", [])
                               if not s.get("name")]
                    veto = unknowns.floor_violation(unknowns.pooled_stats(
                        b["speaker"], cent_emb,
                        unknowns.talk_stats(raw_turns), unnamed))
                    if veto:
                        self._json({"ok": False, "floor": True, "error": veto})
                        return
                    if not confirm:
                        w = identify.sample_check(name, cent_emb[b["speaker"]])
                        if w:
                            self._json(_warn_payload(w))
                            return
                    identify.enroll(name, cent_emb[b["speaker"]], source=b["meeting"])
                    # the cluster just enrolled, and the meeting it came from:
                    # that meeting relabels inside this request, so the open
                    # page stops showing "Voice 1" on its very next poll.
                    tgt["vector"] = cent_emb[b["speaker"]]
                    tgt["first"] = b["meeting"]
                # reaching here IS the success: every failure above answered with
                # an `error` and NO note and returned. The note promises a
                # relabel, so only a save that actually spawns one may carry it.
                res = _relabel_targets(**tgt)
                self._json({"ok": True, "note": "relabeling the meetings this voice appears in",
                            **res})
            elif u.path == "/api/forget":
                self._json({"ok": unknowns.drop(b.text("uid"))})
            elif u.path == "/api/dismiss_voice":
                # "Not a real speaker" for a voice the registry never tracked:
                # a PER-MEETING silence, not a tombstone. /api/forget drops an
                # unknown everywhere; this only stops the naming prompts here
                # and leaves every attributed line exactly where it is.
                if not self._require_base(b.get("base")):
                    return
                # `speakers`, as /api/name's local branch takes it: every
                # cluster of one split voice, dismissed or restored in ONE
                # write, all or none
                from stt import summarize
                fn = (summarize.restore_voice if b.flag("restore")
                      else summarize.dismiss_voice)
                self._json(fn(b["base"], _voice_ids(b)))
            elif u.path == "/api/rename_speaker":
                # the spelling rename_person will store: an enrolled case
                # variant of the new name is a merge into that person
                b.text("name")
                try:
                    new = names.person_name(b.text("new"))
                except names.BadName as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                b["new"] = identify.canonical_name(
                    new, {k: v for k, v in identify.load_registry().items()
                          if k != b["name"]})
                if identify.reserved_name(b["new"]):
                    self._json({"ok": False, "error": identify.RESERVED_ERROR})
                    return
                # a rename onto an enrolled person is a merge: the same
                # wrong-person check as /api/merge_speakers, before confirm
                if (not b.flag("confirm") and b["new"] != b["name"]
                        and b["new"] in identify.load_registry()):
                    prints = identify.load_voiceprints()
                    warn = _merge_warning(b["new"], prints.get(b.get("name")), prints,
                                          exclude=(b.get("name"),))
                    if warn:
                        self._json({**warn, "dst": b["new"]})
                        return
                ok = identify.rename_person(b["name"], b["new"])
                if not ok:
                    self._json({"ok": ok})
                    return
                # BOTH names: the meetings carrying the old label are the ones
                # that must be rewritten, and any already carrying the new one
                # (a rename onto an existing person) has to agree with them.
                self._json({"ok": ok, **_relabel_targets(
                    names=[b["name"], b["new"]],
                    vector=identify.load_voiceprints().get(b["new"]))})
            elif u.path == "/api/remove_speaker":
                ok = identify.remove_person(b.text("name"))
                if not ok:
                    self._json({"ok": ok})
                    return
                # the voiceprint is gone, so only the meetings that already
                # carry the name can change — no vector left to score with
                self._json({"ok": ok, **_relabel_targets(names=[b["name"]])})
            elif u.path == "/api/merge_speakers":
                # src/dst are "uid:U003" or "name:Jordan"
                st_, sv = _handle(b, "src")
                dt_, dv = _handle(b, "dst")
                if dt_ == "name":
                    if names.has_unsafe(dv):
                        self._json({"ok": False, "error": names.UNSAFE_NAME})
                        return
                    if identify.reserved_name(dv):
                        self._json({"ok": False, "error": identify.RESERVED_ERROR})
                        return
                    dv = identify.canonical_name(dv)
                    # the same quality bar as /api/name: folding a voice that
                    # does not match into a person poisons the profile, so a
                    # mismatch asks for confirm first
                    if not b.flag("confirm") and st_ in ("uid", "name"):
                        prints = identify.load_voiceprints()
                        rows = (unknowns.samples_of(sv) if st_ == "uid"
                                else prints.get(sv))
                        warn = _merge_warning(dv, rows, prints,
                                              exclude=(sv,) if st_ == "name" else ())
                        if warn:
                            self._json(warn)
                            return
                heard = []
                if st_ == "uid" and dt_ == "name":
                    # read BEFORE promote() retires the uid (see /api/name):
                    # a meeting whose row left the kept stack is found by
                    # nothing else afterwards
                    heard = unknowns.resolved_meetings(
                        unknowns.load()["speakers"].get(sv) or {})
                    ok = unknowns.promote(sv, dv)
                elif st_ == "uid" and dt_ == "uid":
                    ok = unknowns.merge(sv, dv)
                elif st_ == "name" and dt_ == "name":
                    ok = identify.merge_people(sv, dv)
                else:
                    ok = False  # merging a named person INTO an unknown is nonsense
                if not ok:
                    self._json({"ok": ok})
                    return
                # both sides of the merge, by whichever handle each one has.
                # A retired src uid resolves to nothing: merge folded its
                # meetings into dst, and promote consumed it entirely, so its
                # own list rides in as `bases` and the destination's
                # post-merge samples carry the rest.
                pairs = ((st_, sv), (dt_, dv))
                vec = (identify.load_voiceprints().get(dv) if dt_ == "name"
                       else unknowns.samples_of(dv))
                self._json({"ok": ok, **_relabel_targets(
                    vector=vec,
                    names=[v for t, v in pairs if t == "name"],
                    uids=[v for t, v in pairs if t == "uid"],
                    bases=heard)})
            elif u.path == "/api/rename":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.rename_meeting(b["base"], b.text("new")))
            elif u.path == "/api/ask":
                # the body's own shape first: a question of the wrong type is
                # that, whatever meeting it names
                q_ = b.text("question", "").strip()
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                if not summarize.available():
                    self._json({"error": "local LLM not installed"}, 503)
                    return
                if not q_:
                    self._json({"error": "empty question"}, 400)
                    return
                if len(q_) > 2000:
                    self._json({"error": "question too long (2000 chars max)"}, 400)
                    return
                # history is optional context: a malformed one is dropped
                # (test_server_ask), never a reason to refuse the question
                hist = b.get("history")
                hist = ([h for h in hist if isinstance(h, dict)]
                        if isinstance(hist, list) else None)
                try:
                    r = summarize.answer_question(b["base"], q_, history=hist)
                    self._json(r, 200 if r.get("ok") else 400)
                except summarize.LLMBusy:
                    self._json({"error": "The local model is busy with another "
                                         "summary or question. Try again in a "
                                         "minute."}, 503)
            elif u.path == "/api/ask_library":
                # library Ask (plan Phase 4F): FTS5 retrieval into the local
                # model, citations stored with meeting_id, the exchange saved
                # to its thread. Busy is a 503 with busy:true, and the page
                # keeps the typed question.
                from stt import ask, summarize
                q_ = b.text("question", "").strip()
                if not q_:
                    self._json({"error": "empty question"}, 400)
                    return
                if len(q_) > 2000:
                    self._json({"error": "question too long (2000 chars max)"}, 400)
                    return
                tid = b.text("thread", "") or None
                if tid is not None and not ask.valid_id(tid):
                    self._json({"error": "no such thread"}, 400)
                    return
                prev = ask.get_thread(tid) if tid else None
                if tid and prev is None:
                    self._json({"error": "no such thread"}, 404)
                    return
                # a follow-up keeps its thread's scope
                scope = ((prev or {}).get("scope") or b.obj("scope", None)
                         or {"kind": "all"})
                if not isinstance(scope, dict):
                    self._json({"error": "bad scope"}, 400)
                    return
                if not summarize.available():
                    self._json({"error": "The assistant is not installed, so Ask "
                                         "cannot answer. Search still works."}, 503)
                    return
                try:
                    r = ask.ask(q_, scope=scope,
                                history=ask.history_of(tid) if tid else None)
                except summarize.LLMBusy:
                    self._json({"busy": True,
                                "error": "The assistant is busy with a summary or "
                                         "another question. Try again when it "
                                         "finishes. Your question stays here."}, 503)
                    return
                if not r.get("ok"):
                    self._json(r, 400)
                    return
                if scope.get("kind") == "group":
                    groups.log_use("group_ask")
                if r.get("nothing_found") and not tid:
                    # a first question that found nothing starts no thread:
                    # the page shows the reply and its closest hits unsaved
                    self._json({"ok": True, "thread": None,
                                "message": ask.unsaved_message(q_, r),
                                "scope_label": r.get("scope_label", "")})
                    return
                t = ask.save_message(tid, {k: v for k, v in scope.items() if k != "label"},
                                     r.get("scope_label", ""), q_, r)
                self._json({"ok": True, "thread": t["id"],
                            "message": t["messages"][-1],
                            "scope_label": r.get("scope_label", "")})
            elif u.path == "/api/thread_delete":
                from stt import ask
                self._json({"ok": ask.delete_thread(b.text("id", ""))})
            elif u.path == "/api/hide_unknown":
                uid = b.text("uid")
                ok = (unknowns.archive(uid) if b.flag("hide", True)
                      else unknowns.restore(uid))
                self._json({"ok": ok})
            elif u.path == "/api/cloud_keys":
                from stt import asr_cloud
                key_envs = {prov: meta["key_env"]
                            for prov, meta in asr_cloud.PROVIDERS.items()}
                key_envs["anthropic"] = "STT_ANTHROPIC_KEY"  # assistant, not ASR
                updates, remove = {}, set()
                cleared = set(b.texts("clear", []))
                for prov, key_env in key_envs.items():
                    v = b.text(prov, "", max_len=4000).strip()
                    if v:  # a pasted key wins over a clear for the same provider
                        updates[key_env] = v
                    elif prov in cleared:
                        remove.add(key_env)
                if updates or remove:
                    _env_file_set(updates, remove=remove)
                    os.chmod(config.PROJECT_DIR / "stt.env", 0o600)
                self._json({"ok": True, "set": _cloud_key_status()})
            elif u.path == "/api/llm_backend":
                from stt import summarize
                backend = b.text("backend", "")
                if backend not in summarize.LLM_BACKENDS:
                    self._json({"error": "unknown assistant backend"}, 400)
                    return
                if not summarize.backend_available(backend):
                    self._json({"error": "that assistant isn't set up yet — add "
                                         "its API key (or install .venv-llm) "
                                         "first"}, 400)
                    return
                _env_file_set({"STT_LLM_BACKEND": backend})
                self._json({"ok": True, "backend": backend})
            elif u.path == "/api/automation":
                self._json(write_automation(watch=b.flag("watch", None),
                                            nightly=b.flag("nightly", None)))
            elif u.path == "/api/set_date":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.set_meeting_date(b["base"], b.text("date", "")))
            elif u.path == "/api/set_category":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.set_meeting_category(b["base"],
                                                          b.text("category", "")))
            elif u.path == "/api/queue_hold":
                # park a waiting file: automatic runs skip it until released.
                # Same membership gate as preview/delete.
                from stt import holds
                f = _queue_file(b.text("name", ""))
                if f is None:
                    self._json({"ok": False, "error": "no such queued file"})
                    return
                self._json({"ok": True, "held": holds.toggle(f.name)})
            elif u.path == "/api/queue_delete":
                # bin a waiting source file (a bad take, a test recording) before
                # it ever becomes a meeting. Same membership gate as the preview.
                if not b.flag("confirm"):
                    self._json({"ok": False, "error": "missing confirmation"})
                    return
                name = b.text("name", "")
                # every copy of this name, in the queue's own resolution order:
                # the first is the file the row showed and the only one deleted
                found = _queue_files(name)
                if not found:
                    self._json({"ok": False, "error": "no such queued file"})
                    return
                f = found[0]
                if name in status.active_now():
                    self._json({"ok": False,
                                "error": "this file is being processed right now"})
                    return
                mb = round(f.stat().st_size / 1e6, 1)
                # A FAILED row's X deletes the file. The failure record it
                # leaves behind is then history-only, so the very next poll
                # re-emits it as a gone:true row demanding a SECOND, separate
                # dismissal. Deleting the file is the whole answer to that
                # failure: dismiss it in the same breath. Only when the name
                # really is a live failure, so an ordinary queue delete never
                # writes a dead entry into dismissed_failures.json.
                was_failed = f.name in _failed_sources(status.read())
                f.unlink()
                # the recorder's channel-layout sidecar travels with its audio
                # (a name past the 255-byte limit has none, and raises)
                try:
                    f.with_suffix(".opts.json").unlink(missing_ok=True)
                except OSError:
                    pass
                out = {"ok": True, "freed_mb": mb}
                if was_failed and dismiss_failure(f.name):
                    out["dismissed_failure"] = True
                if len(found) == 1:
                    # the last file under this name is gone, and its hold
                    # goes with it: the next recording under the name is a
                    # new file that nobody parked
                    from stt import holds
                    holds.release(f.name)
                if len(found) > 1:
                    # the same name in the OTHER watched folder is a DIFFERENT
                    # recording. It takes this row's place immediately, and it
                    # inherits the deleted file's hold -- say so instead of
                    # letting the row look like it never went away.
                    out["shadowed"] = True
                    out["note"] = ("another file with this name is waiting in "
                                   "the other watched folder -- it takes this "
                                   "row's place, hold and all")
                self._json(out)
            elif u.path == "/api/queue_delete_dupes":
                # bin every waiting file that is BYTE-IDENTICAL to a meeting that
                # already exists. The list is re-derived here from the files on
                # disk, never taken from the client: the panel can ask for the
                # sweep, it cannot name what the sweep deletes.
                #
                # Content-identical only. A file that merely shares a NAME with a
                # past meeting's source is flagged in the row and deletable one
                # by one, because a recurring export honestly reuses one name for
                # different meetings, and this action has no undo.
                if not b.flag("confirm"):
                    self._json({"ok": False, "error": "missing confirmation"})
                    return
                from stt import holds as _holds
                held_now = _holds.items()
                deleted, freed = [], 0.0
                # every candidate the sweep DECLINED, with the server's own
                # reason. A sweep that deleted nothing used to answer
                # {ok:true, deleted:[]}, which reads as full success: no
                # message, and a Delete button that silently no-ops on every
                # click. The client says why, from these.
                skipped = []

                def _skip(nm, why):
                    skipped.append({"name": nm, "reason": why})
                # the PURE helper, never gather_state: gather_state kicks the
                # next queued batch and the dupe scan as side effects, and a
                # delete request must not be the thing that starts a run
                _sweep_queue, _, _ = _queue_and_dupes()
                for f in _sweep_queue:
                    if f.get("dup_reason") != "identical" or f["processed"]:
                        continue
                    if f["name"] in held_now:
                        _skip(f["name"], "it is held")
                        continue          # parked on purpose
                    # active is re-read per file, immediately before its unlink:
                    # a run can start at any moment (another poll's kick), and a
                    # snapshot taken before the loop would happily delete a
                    # source mid-transcode
                    if f["name"] in status.active_now():
                        _skip(f["name"], "it is being processed right now")
                        continue
                    # the matched meeting is re-checked too: a concurrent
                    # delete_meeting between the snapshot above and this unlink
                    # would leave this file as the only copy of that recording,
                    # the exact loss the liveness gate exists to prevent
                    if (f.get("dup_of") not in set(config.meeting_bases())
                            | set(config.archived_bases())):
                        _skip(f["name"], "the meeting it copied is gone")
                        continue
                    # and the meeting must still HOLD that audio. A manifest
                    # fingerprint keeps matching after drop_audio removed the
                    # meeting's own copy, so the base reads live while the only
                    # remaining bytes of that recording are the file below.
                    stored = _stored_audio(f.get("dup_of"))
                    if not stored:
                        _skip(f["name"],
                              "this is the last copy of that recording")
                        continue
                    p = _queue_file(f["name"])
                    if p is None:
                        _skip(f["name"], "it is no longer in a watched folder")
                        continue
                    # identity is proven HERE, not in the snapshot: this file is
                    # re-resolved by NAME, and a recurring export name can point
                    # at a different recording by now (iCloud delivered this
                    # week's file; the source-dir copy vanished and a same-named
                    # one in recordings_dir took the row). Re-fingerprint the
                    # exact path about to be unlinked and require it to equal
                    # the fingerprint that earned the match.
                    snap = f.get("dup_fp")
                    if not snap or dupes.fingerprint(p) != snap:
                        _skip(f["name"],
                              "the file changed since it was flagged")
                        continue
                    # the fingerprint samples three megabytes, so a same-size
                    # edit between them still matched. When the meeting holds
                    # a copy of the same size, every byte is compared first.
                    # A video's stored audio is an extracted track of another
                    # size, and the original's bytes exist nowhere else: there
                    # the sampled fingerprint is the only proof there is.
                    if dupes.same_bytes(p, stored) is False:
                        _skip(f["name"],
                              "its bytes differ from the meeting's copy")
                        continue
                    try:
                        freed += p.stat().st_size / 1e6
                        p.unlink()
                        try:
                            p.with_suffix(".opts.json").unlink(missing_ok=True)
                        except OSError:
                            pass    # a name too long to have a sidecar
                        deleted.append(f["name"])
                    except OSError as e:
                        _skip(f["name"], f"it could not be deleted ({e.strerror})")
                        continue
                self._json({"ok": True, "deleted": deleted, "skipped": skipped,
                            "freed_mb": round(freed, 1)})
            elif u.path == "/api/dupe_ignore":
                # "keep both": this pair is two different meetings. Nothing is
                # deleted and nothing is hidden from the library — the pair just
                # stops being offered for review.
                a, b_ = b.text("a", ""), b.text("b", "")
                if not _known_base(a) or not _known_base(b_):
                    self._json({"ok": False, "error": "unknown meeting"}, 400)
                    return
                self._json({"ok": dupes.ignore_pair(a, b_)})
            elif u.path == "/api/dupe_scan":
                # recompute now (the drawer's Rescan): synchronous, because the
                # user asked for it and is watching the section redraw. Goes
                # through the same single-flight guard as the poll's kick; when
                # a scan is already running this answers with the current cache
                # and scanning=True, and the drawer keeps polling.
                ran = _force_dupe_scan()
                self._json({"ok": True, "ran": ran, "pairs": _dupe_pair_rows(),
                            "scanning": bool(_dupe_worker["busy"]),
                            "scanned": dupes.cache_scanned(),
                            "truncated": dupes.cache_truncated()})
            elif u.path == "/api/dismiss_failure":
                # the X on a failed row whose source file no longer exists:
                # nothing to delete, just stop showing that old failure
                self._json({"ok": dismiss_failure(b.text("name", ""))})
            elif u.path == "/api/fix_recorder_permissions":
                # Reset THIS APP's TCC entries so macOS re-prompts on the next
                # Start. Needed because the recorder is ad-hoc signed: a rebuild
                # changes its cdhash and orphans the grants with no prompt and no
                # error. Scoped strictly to our own bundle id — tccutil cannot
                # touch anything else here — and the actual granting still happens
                # in the OS permission dialogs.
                outs = []
                for svc in ("Microphone", "AudioCapture"):
                    r = subprocess.run(["/usr/bin/tccutil", "reset", svc,
                                        "com.stt-workflow.recorder"],
                                       capture_output=True, text=True, timeout=15)
                    outs.append(r.returncode)
                if any(outs):
                    self._json({"ok": False,
                                "error": "macOS refused the permission reset — "
                                         "rebuild the recorder (./setup.sh "
                                         "build-recorder) and try again, or remove "
                                         "'STT Recorder' by hand in System Settings "
                                         "> Privacy & Security > Microphone."})
                else:
                    self._json({"ok": True,
                                "message": "Permissions reset. Now click Start "
                                           "recording in the menu bar and grant "
                                           "BOTH prompts when macOS asks."})
            elif u.path == "/api/group_override":
                # "Not part of this group", keyed on meeting_id, with undo
                if not self._require_base(b.get("base")):
                    return
                key = b.text("group", "").strip()
                if not key:
                    self._json({"ok": False, "error": "no group"}, 400)
                    return
                mkey = config.id_of(b["base"]) or b["base"]
                undo = b.flag("undo")
                # the key must name a real group: an invented one was stored
                # forever, matching nothing. An undo may name a group its own
                # exclusion dissolved (one meeting is no group), so the key
                # already stored for this meeting is enough for it.
                g = groups.by_key(key)
                if undo:
                    if g is None and key not in groups.load_overrides().get(mkey, ()):
                        self._json({"ok": False, "error": "no such group"}, 400)
                        return
                elif g is None:
                    self._json({"ok": False, "error": "no such group -- refresh "
                                                      "and try again"}, 400)
                    return
                elif b["base"] not in g["bases"]:
                    self._json({"ok": False, "error": "that meeting is not in "
                                                      "this group"}, 400)
                    return
                self._json(groups.set_override(mkey, key, exclude=not undo))
            elif u.path == "/api/usage":
                # one count line per group open or group question, no title
                ok = groups.log_use(b.text("event", ""))
                self._json({"ok": ok}, 200 if ok else 400)
            elif u.path == "/api/seen":
                # the panel is in front of the owner: record the look, so the
                # menu bar's "new since last look" count starts over. Ids and a
                # time only, never a title.
                from stt import ready
                d = ready.mark_seen()
                self._json({"ok": True, "seen_at": d["seen_at"]})
            elif u.path == "/api/suggest":
                # title and description from the local model, saved to
                # the meeting .json (the call that used to be a GET)
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.suggest_title(b["base"]))
            elif u.path == "/api/accept_meeting":
                # inbox: name/date/tag it, and mark it reviewed so it joins the list
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                # a Keep on ONE meeting: the title is confirmed, and a person
                # did look at it
                self._json(summarize.apply_meeting_edits(
                    b["base"], title=(b.text("name", "") or None),
                    date=(b.text("date", "") or None),
                    category=b.text("category", None),
                    reviewed=True, title_state="confirmed"))
            elif u.path == "/api/bulk":
                # multi-select actions. Every op below gates on live/archived
                # MEMBERSHIP internally before it builds a path, so an unknown or
                # traversal base is simply refused per-item — never applied.
                action = b.text("action", "")
                value, confirmed = b.text("value", None), b.flag("confirm")
                results = []
                # the cap is real, so the answer says so: a selection above it
                # silently lost its tail and the client could not tell a
                # dropped item from one that was never selected
                asked = b.texts("bases", [])
                dropped = max(0, len(asked) - 500)
                for bs in asked[:500]:
                    # PER ITEM: one meeting's OSError (an unwritable folder, a
                    # vanished file) used to abort the loop, leaving the items
                    # before it applied on disk, the items after it never
                    # attempted, and the client a bare 500 with no results to
                    # report. Every item is now tried, and every outcome told.
                    try:
                        r = self._bulk_one(action, bs, value, confirmed)
                    except Exception:
                        # the detail to the log: exception text names paths
                        print(f"panel: bulk {action} on one meeting failed",
                              file=sys.stderr)
                        traceback.print_exc(file=sys.stderr)
                        r = {"ok": False, "error": "this one could not be "
                                                   "changed (see the panel log)"}
                    results.append({"base": bs, **r})
                out = {"ok": all(r.get("ok") for r in results) and not dropped,
                       "n_ok": sum(1 for r in results if r.get("ok")),
                       "freed_mb": round(sum(r.get("freed_mb") or 0
                                             for r in results), 1),
                       "results": results}
                if dropped:
                    out["dropped"] = dropped
                    out["error"] = (f"only the first 500 of {len(asked)} were "
                                    f"done; {dropped} were not attempted")
                self._json(out)
            elif u.path == "/api/archive_meeting":
                if not self._require_base(b.get("base")):
                    return
                from stt import archive
                self._json(archive.archive_meeting(b["base"]))
            elif u.path == "/api/restore_meeting":
                # NOT _require_base: the base is by definition not a live
                # meeting. restore_meeting gates on archived-list membership
                # before any path is built — same defense, different universe.
                from stt import archive
                self._json(archive.restore_meeting(b.text("base", "")))
            elif u.path == "/api/delete_meeting":
                # live or archived; delete_meeting gates on membership in one
                # of the two lists before any path is built. confirm is a
                # seatbelt so nothing deletes on a malformed call: only a JSON
                # true confirms (the string "false" used to delete).
                if not b.flag("confirm"):
                    self._json({"ok": False, "error": "missing confirmation"})
                    return
                from stt import archive
                self._json(archive.delete_meeting(b.text("base", "")))
            elif u.path == "/api/review":
                if not self._require_base(b.get("base")):
                    return
                action = b.text("action")
                if action == "accept_minor":
                    self._json(review.accept_minor(b["base"]))
                    return
                if action == "insert":
                    self._json(review.insert_segment(
                        b["base"], b.number("start"), b.number("end"),
                        b.text("speaker"), b.text("text", "")))
                    return
                # the client's snapshot of the line it means: `start` and
                # `end` cross-check `index` against the file as it is now
                start, end = b.number("start", None), b.number("end", None)
                if action == "delete":
                    self._json(review.delete_segment(b["base"], b.integer("index"),
                                                     start=start, end=end))
                    return
                if action == "split":
                    self._json(review.split_segment(
                        b["base"], b.integer("index"), start=start,
                        text_a=b.text("text_a", ""), text_b=b.text("text_b", ""),
                        speaker_a=b.text("speaker_a", None),
                        speaker_b=b.text("speaker_b", None), end=end))
                    return
                self._json(review.apply(b["base"], b.integer("index"), action,
                                        start=start, text=b.text("text", None),
                                        speaker_id=b.text("speaker", None),
                                        end=end))
            elif u.path == "/api/export":
                if not self._require_base(b.get("base")):
                    return
                fmt = b.text("fmt")
                if fmt not in ("docx", "pdf", "reveal"):
                    raise BadRequest("'fmt' must be docx, pdf or reveal")
                if fmt == "reveal":
                    f = config.meeting_file(b["base"], ".txt")
                    subprocess.run(["open", "-R", str(f)], capture_output=True)
                    self._json({"ok": f.exists()})
                else:
                    path = export.export(b["base"], fmt)
                    subprocess.run(["open", "-R", str(path)], capture_output=True)
                    self._json({"ok": True, "path": str(path)})
            elif u.path == "/api/retranscribe":
                if not self._require_base(b.get("base")):
                    return
                engine = b.text("engine", "") or "parakeet"
                # an engine the second listen does not offer starts no
                # child (the child checks this again)
                from stt import retranscribe as _rt
                why = _rt.engine_problem(engine)
                if why:
                    self._json({"error": why})
                    return
                # one-shot subprocess: the panel must never hold a 2GB ASR model
                r = subprocess.run(
                    [str(config.CODE_DIR / ".venv/bin/python"), "-m", "stt.retranscribe",
                     b["base"], str(b.number("start")), str(b.number("end")),
                     engine],
                    capture_output=True, text=True, timeout=180,
                    cwd=str(config.CODE_DIR),
                    env={**os.environ, "PYTHONPATH": str(config.CODE_DIR)})
                try:
                    self._json(json.loads(r.stdout.strip().splitlines()[-1]))
                except Exception:
                    # the child's stderr is a traceback with server paths in
                    # it: the log keeps it, and the client gets a sentence
                    print("panel: re-transcription failed:\n"
                          + (r.stderr or "")[-2000:], file=sys.stderr)
                    self._json({"error": "The second listen failed. The "
                                         "details are in the panel log."}, 500)
            elif u.path == "/api/remove_sample":
                ok = identify.remove_sample(b.text("name"), b.integer("index"))
                if not ok:
                    self._json({"ok": False,
                                "error": "Can't remove the only sample — remove the person instead."})
                    return
                # a removed sample changes who this profile matches — re-run
                # identification like every other registry edit, so a
                # misattributed voice drops out of past transcripts (and
                # resurfaces as an unknown to name) instead of the correction
                # only taking effect on the next unrelated relabel. The
                # REMAINING stack says who that still is; the name catches the
                # meetings the removed sample had already labeled.
                self._json({"ok": ok, "note": "re-identifying all meetings in background",
                            **_relabel_targets(
                                names=[b["name"]],
                                vector=identify.load_voiceprints().get(b["name"]))})
            elif u.path == "/api/reassign_sample":
                r = identify.reassign_sample(b.text("name"), b.integer("index"),
                                             b.text("to", ""))
                if not r.get("ok"):
                    self._json(r)
                    return
                # a moved voice changes BOTH profiles, so both names count
                to = b.text("to", "").strip()
                self._json({**r, **_relabel_targets(
                    names=[n for n in (b["name"], to) if n],
                    vector=identify.load_voiceprints().get(b["name"]))})
            elif u.path == "/api/punctuate":
                _env_file_set({"STT_PUNCTUATE": "1" if b.flag("on") else "0"})
                self._json({"ok": True})
            elif u.path == "/api/mic_speaker":
                # who "me" is on the meeting recorder's mic channel; enables the
                # channel-aware path for new recordings (they process as mono
                # until this is set and that person is enrolled)
                name = b.text("name", "").strip()
                if names.has_unsafe(name):
                    # a line break here also injected a line into stt.env
                    raise BadRequest(names.UNSAFE_NAME)
                if name:
                    _env_file_set({"STT_MIC_SPEAKER": name})
                else:
                    _env_file_set({}, remove=("STT_MIC_SPEAKER",))
                self._json({"ok": True, "mic_speaker": name or None})
            elif u.path == "/api/relabel":
                # the explicit "re-identify everything" button: one `all` job
                # on the queue, which absorbs any targeted job waiting behind it
                _relabel_all_background("re-identify everything")
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except BadRequest as e:
            self._json({"error": e.msg}, e.code)
        except UserError as e:
            # planned, and its text is the help ("Google Chrome not found
            # -- export Word instead"): shown as it is
            self._json({"ok": False, "error": str(e)}, e.code)
        except Exception:
            self._failed(u.path)


def check_updates():
    from huggingface_hub import HfApi, scan_cache_dir
    api = HfApi()
    local = {}
    try:
        for repo in scan_cache_dir().repos:
            revs = sorted(repo.revisions, key=lambda r: r.last_modified or 0)
            if revs:
                local[repo.repo_id] = revs[-1].commit_hash
    except Exception:
        pass
    out = []
    for label, repo in MODEL_REPOS.items():
        try:
            # offline/stalled connections must not hang "Check updates"
            # indefinitely — the button has no cancel
            latest = api.model_info(repo, timeout=10).sha
            have = local.get(repo)
            out.append({"label": label, "repo": repo,
                        "cached": bool(have),
                        "update_available": bool(have and latest and have != latest)})
        except Exception:
            out.append({"label": label, "repo": repo, "cached": repo in local,
                        "update_available": None, "note": "offline?"})
    return {"models": out}


class PortInUse(RuntimeError):
    """The panel's port is taken: another panel, or another program, is
    listening on 127.0.0.1:PORT. The message is one line for a person."""


def _bind(port):
    """The panel's HTTP server on 127.0.0.1:`port`. A port in use raises
    PortInUse (one line) instead of the OSError traceback that ended a
    second panel, and that the menu-bar app did not catch at all."""
    import errno
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        raise PortInUse(f"port {port} is in use: another control panel (or another "
                        f"program) already listens on 127.0.0.1:{port}") from None


def start_server():
    global RELABEL_QUEUE
    try:
        # one-time flat->folder layout migration + date backfill (idempotent)
        config.migrate_flat_meetings()
    except Exception:
        pass  # the panel must still start; the batch retries the migration
    try:
        # stamp a stable meeting_id into anything that predates it, rebuild the
        # id index, and sweep orphan lock files (idempotent, and a no-op once
        # every meeting carries an id). The ARCHIVE is stamped too: a restore
        # has to bring the meeting's id back with it.
        config.backfill_meeting_ids()
        config.backfill_meeting_ids(config.archive_dir())
    except Exception:
        pass  # same contract: never block the panel on a migration
    # bind first: with the port taken, no relabel worker starts for a panel
    # that cannot serve a request
    srv = _bind(PORT)
    # the one relabel worker for this process. Every naming, rename, merge and
    # queued follow-up goes through it from here on, instead of spawning a
    # child that reloads the models this process already has warm.
    if RELABEL_QUEUE is None:
        RELABEL_QUEUE = jobqueue.RelabelQueue()
    RELABEL_QUEUE.start()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_FONT_DIR = _STATIC_DIR / "fonts"   # vendored Figtree (OFL), served by GET /static/fonts/
# The panel (plan Phases 4C and 4D), served at /: a skeleton, a stylesheet,
# and several small plain scripts joined in this fixed order, with no import
# lines and no build step: the frame, then the meeting page, then the voice
# sheet, then the library views (Groups and Ask).
_NEXT_PARTS = ("next.html", "next.css", "next.js", "next-meeting.js", "next-voice.js",
               "next-library.js")
_next_cache: dict = {"mtimes": None, "html": None}


def _compose(parts, css_marker, js_marker):
    """Assemble one served page from its (skeleton, css, js, ...) parts. Every
    part after the CSS is a script, joined in order. The CSS and JS are full
    of braces, so the skeleton carries marker comments and we substitute with
    str.replace -- never .format()/f-strings."""
    page, css, *js = ((_STATIC_DIR / f).read_text(encoding="utf-8") for f in parts)
    return page.replace(css_marker, css).replace(js_marker, "\n".join(js))


def _compose_next():
    return _compose(_NEXT_PARTS, "/*@NEXT_CSS@*/", "//@NEXT_JS@")


def _cached(parts, cache, compose):
    """The composed page, recomposed only when one of its static parts
    changes on disk -- editing a part reloads live, with no server restart."""
    mtimes = tuple((_STATIC_DIR / f).stat().st_mtime_ns for f in parts)
    if cache["html"] is None or cache["mtimes"] != mtimes:
        cache["html"] = compose()
        cache["mtimes"] = mtimes
    return cache["html"]


def _dir_present(p) -> bool:
    """Is the folder there? The first-run card checks the watched folder
    with it: a path that is set but missing showed a green check. A denied
    stat (a folder macOS privacy guards) counts as there."""
    try:
        return Path(p).is_dir()
    except PermissionError:
        return True
    except OSError:
        return False


def _next_html():
    return _cached(_NEXT_PARTS, _next_cache, _compose_next)


if __name__ == "__main__":
    # Headless panel: serve the control panel WITHOUT the menu-bar app, on a
    # chosen port (STT_PANEL_PORT, default PORT). Paired with STT_HOME this runs
    # the panel against a synthetic data home for demos/screenshots — nothing
    # here touches real recordings. Only runs on direct execution; importing the
    # module for the menu bar or the tests is unaffected.
    config.private_umask()
    PORT = int(os.environ.get("STT_PANEL_PORT", str(PORT)))
    try:
        start_server()
    except PortInUse as e:
        print(f"STT control panel not started: {e}. Stop the other one, or set "
              "STT_PANEL_PORT to a free port.", file=sys.stderr)
        sys.exit(1)
    print(f"STT control panel: http://127.0.0.1:{PORT}/   "
          f"(STT_HOME={config.STT_HOME or '(real data — none set)'})")
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()  # block forever; serve_forever runs in a daemon thread
    except KeyboardInterrupt:
        pass


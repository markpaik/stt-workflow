"""Local control panel for the STT pipeline.

A tiny stdlib HTTP server bound to 127.0.0.1 only, started as a thread inside the
menu-bar app. Serves one polished HTML page + a JSON API. All actions shell out to
the same entrypoints the CLI uses, so the panel can never bypass the pipeline's
locks and safety rails.
"""
import json
import os
import plistlib
import re
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from stt import (config, control, dates, export, identify, jobs, manifest, rates,
                 recorder, review, search, status, unknowns)

PORT = 8737
AGENT = Path.home() / "Library/LaunchAgents/com.stt-workflow.batch.plist"
LABEL = "com.stt-workflow.batch"
RUN_SH = config.PROJECT_DIR / "run.sh"

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


def _known_base(base) -> bool:
    """A meeting basename must name a real transcript on disk — the only
    defense against a path-traversal `base` like '../../../etc/passwd' reaching
    a file path. Deliberately a plain membership test (not path resolution):
    glob() can only ever return names of files actually inside meetings_dir."""
    return (isinstance(base, str) and bool(base)
            and base in set(config.meeting_bases()))

ASR_CHOICES = [
    {"id": "parakeet", "label": "Parakeet TDT 0.6B v2", "note": "fastest · lowest benchmark WER · English"},
    {"id": "mlxwhisper:large-v3", "label": "Whisper large-v3 (MLX)", "note": "best punctuation robustness · ~4x realtime"},
    {"id": "mlxwhisper:turbo", "label": "Whisper large-v3-turbo (MLX)", "note": "Whisper punctuation · ~9x realtime"},
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
    "Parakeet TDT 0.6B v2": "mlx-community/parakeet-tdt-0.6b-v2",
    "Whisper large-v3 (MLX)": "mlx-community/whisper-large-v3-mlx",
    "Whisper large-v3-turbo (MLX)": "mlx-community/whisper-large-v3-turbo",
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
    """launchd caches plists — every edit must bootout+bootstrap to apply."""
    import os
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], capture_output=True)


def _watch_paths():
    """Both folders the batch agent watches: the primary source AND the local
    recordings staging folder (the meeting recorder's output). Every place that
    rewrites WatchPaths uses this, so changing the source folder or toggling the
    watch never silently drops the recorder's trigger."""
    return [str(config.source_dir()), str(config.recordings_dir())]


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
        else:
            d.pop("WatchPaths", None)
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
    return {"ok": True, **read_schedule()}


def write_schedule(hh, mm):
    write_automation(hour=hh, minute=mm)


_dur_cache = {}


def _est_duration(p: Path):
    """Audio seconds for a queued file: exact (ffprobe) when the bytes are local,
    size-based estimate (~1 MB/min for voice memos) for dataless iCloud files —
    probing those would force a download."""
    try:
        key = (str(p), p.stat().st_mtime)
    except FileNotFoundError:
        return None
    if key in _dur_cache:
        return _dur_cache[key]
    dur = None
    try:
        from stt import audio as A, icloud
        if icloud._fully_present(p):
            dur = A.duration_sec(p)
        else:
            rate = 6.0 if p.suffix.lower() in config.VIDEO_EXTS else 1.0  # MB/min
            dur = (p.stat().st_size / 1e6) / rate * 60.0
    except Exception:
        pass
    _dur_cache[key] = dur
    return dur


_meet_cache = {}
_relabel_kicked = {"at": 0.0}
_jobs_kicked = {"at": 0.0}


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
    _jobs_kicked["at"] = _time.monotonic()
    _spawn(jobs.spawn_args(nxt[0]))


def _queue_file(name):
    """The waiting source file called `name`, or None.

    Queue items have no meeting yet, so there is no base to gate on — this is
    their equivalent of _known_base. A name is accepted ONLY if it lands, after
    resolution, directly inside one of the watched folders and is a real audio
    file. Resolving first and then re-checking the parent is what defeats
    traversal ('../../etc/passwd') and symlinks pointing out of the folder;
    dotfiles are refused so an in-progress .rec-*.caf capture can't be served
    or deleted out from under the recorder."""
    if not name or not isinstance(name, str):
        return None
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    for folder in (config.source_dir(), config.recordings_dir()):
        try:
            p = (folder / name).resolve()
            if p.parent != folder.resolve() or not p.is_file():
                continue
            if p.suffix.lower() not in config.AUDIO_EXTS:
                continue
            return p
        except OSError:
            continue
    return None


def _display_title(base: str) -> str:
    """The folder name minus its trailing date stamp, so 'Weekly Check-in 07102026'
    shows as 'Weekly Check-in' (the date is shown separately). The date lives in
    the FILENAME to keep recurring meetings unique on disk; the panel shows the
    clean name. dates.strip_stamp is the single definition of that rule — the
    pipeline, rename, the date editor, and the recorder all stamp through the same
    helpers, so what the panel strips is exactly what they add."""
    return dates.strip_stamp(base)


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
        audio_p = config.meeting_audio(j.stem, dst_dir)
        from datetime import date as _date
        from datetime import datetime as _dt
        audio_mtime = (audio_p or j).stat().st_mtime
        # stored date wins (stamped at process time, human-editable);
        # filename/mtime derivation only for pre-migration jsons
        iso = (d.get("date") or dates.meeting_date(j.stem)
               or _date.fromtimestamp(audio_mtime).isoformat())
        meta = {"base": j.stem,
                "date": iso,
                # display name: the folder carries the date so recurring
                # meetings stay unique on disk, but the panel shows the clean
                # name (the date is shown separately)
                "title": _display_title(j.stem),
                "minutes": round(d.get("duration_sec", 0) / 60),
                "speakers": [s["display"] for s in d.get("speakers", [])],
                "strict": d.get("strict", False),
                "flagged": sum(1 for s in d.get("segments", [])
                               if s.get("flags") and not review.is_minor(s)),
                "flagged_minor": sum(1 for s in d.get("segments", [])
                                     if s.get("flags") and review.is_minor(s)),
                "summary": d.get("ai_summary", ""),
                "next_steps": d.get("ai_next_steps", []),
                "category": d.get("category"),  # work | personal | None
                # ONLY an explicit False means "not yet named/dated by a human".
                # Meetings processed before the inbox existed carry no key at all,
                # and must not retroactively flood it — so a missing key = reviewed.
                "needs_review": d.get("reviewed") is False,
                "suggested": d.get("ai_title") or "",
                # when transcription last ran (new or redo). Older transcripts
                # predate this field — fall back to generated_at, then mtime.
                "processed_at": (d.get("processed_at") or d.get("generated_at")
                                 or _dt.fromtimestamp(
                                     j.stat().st_mtime).isoformat(timespec="seconds")),
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
    rows = []
    try:
        for ln in p.read_text().splitlines():
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    except OSError:
        rows = []
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
    rows += [r for r in st.get("recent", [])
             if (r.get("name"), r.get("at")) not in seen]
    latest = {}
    for r in rows:
        nm = r.get("name")
        if nm is None:
            continue
        prev = latest.get(nm)
        if prev is None or (r.get("at") or "") >= (prev.get("at") or ""):
            latest[nm] = r
    return {nm: (r.get("summary") or "failed")
            for nm, r in latest.items() if not r.get("ok")}


def _one_line(text, cap=160):
    """A single-sentence preview of a summary — first sentence, else a hard cap."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    for end in (". ", "? ", "! "):
        i = t.find(end)
        if 0 < i <= cap:
            return t[:i + 1]
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def _timeline_tray(st, queue, meetings, active_out, unknown_list, rec):
    """(timeline, tray) — the unified per-meeting feed (newest first) and the
    ranked attention tray, built entirely from gather_state's own locals."""
    from datetime import datetime as _dt
    timeline, failed_entries = [], []
    # sources already owned by another row: never emit a duplicate history-only
    # failure for a file that is queued, in flight, or already a meeting
    emitted = set()

    # --- recording: the live capture, always one row, pinned to the top ---
    if rec:
        cap = Path(rec.get("caf", "recording")).stem or "recording"
        timeline.append({
            "id": f"rec:{cap}", "state": "recording",
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
            "source_file": name, "prev_id": f"src:{name}",
            "title": _display_title(base) if base else name,
            "date": dates.meeting_date(base or name),
            "when": e.get("since") or status._now(),
            "stage": e.get("stage"), "pct": e.get("pct"), "eta": e.get("eta_sec")})

    # --- meetings: needs_name (awaiting a human name/date) or ready. A meeting
    # being reprocessed shows as its processing row above, not twice. ---
    active_bases = {e.get("base") for e in active_out.values() if e.get("base")}
    for meta in meetings:
        if meta["base"] in active_bases:
            continue
        if meta.get("source_file"):
            emitted.add(meta["source_file"])
        row = {"source_file": meta.get("source_file"),
               "title": meta["title"], "date": meta["date"],
               "when": meta["processed_at"]}
        if meta["needs_review"]:
            row.update({"id": meta["base"], "state": "needs_name",
                        "suggested_title": meta["suggested"],
                        "suggested_date": meta["date"],
                        "has_audio": bool(meta["audio"])})
        else:
            row.update({"id": meta["base"], "state": "ready",
                        "minutes": meta["minutes"], "speakers": meta["speakers"],
                        "category": meta["category"],
                        "review_substantial": meta["flagged"],
                        "review_minor": meta["flagged_minor"],
                        "has_summary": bool(meta["summary"]),
                        "summary": _one_line(meta["summary"])})
        timeline.append(row)

    # --- queue sources: waiting / held / failed. A source in flight is already a
    # processing row above; a processed one is already its meeting. ---
    failed_map = _failed_sources(st)
    for f in queue:
        name = f["name"]
        if f["processed"] or name in emitted:
            continue
        emitted.add(name)
        row = {"id": f"src:{name}", "source_file": name,
               "title": name, "date": dates.meeting_date(name),
               "when": _dt.fromtimestamp(f["mtime"]).isoformat(timespec="seconds"),
               "size_mb": f["size_mb"], "est_minutes": f.get("est_min")}
        if f["held"]:
            row.update({"state": "held", "held": True})
        elif name in failed_map:
            row.update({"state": "failed", "error": failed_map[name],
                        "retry_note": "still in the watched folder — it re-runs on the next batch"})
            failed_entries.append(row)
        else:
            row["state"] = "waiting"
        timeline.append(row)

    # --- history-only failures: a file that failed and is no longer queued (and
    # never became a meeting). It stays visible so the failure is not silently
    # lost the moment the source leaves the folder. ---
    for name, err in failed_map.items():
        if name in emitted:
            continue
        emitted.add(name)
        row = {"id": f"src:{name}", "state": "failed", "source_file": name,
               "title": name, "date": dates.meeting_date(name),
               "when": _failed_at(st, name) or status._now(),
               "error": err,
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
    for row in failed_entries:
        tray.append({"kind": "failed", "title": row["title"],
                     "detail": row["error"], "target": row["id"], "count": 1})
    for meta in meetings:
        if meta["base"] not in active_bases and meta["flagged"] and not meta["needs_review"]:
            tray.append({"kind": "review", "title": meta["title"],
                         "detail": f"{meta['flagged']} segment"
                                   f"{'s' if meta['flagged'] != 1 else ''} flagged for review",
                         "target": meta["base"], "count": meta["flagged"]})
    for u in unknown_list:
        if u.get("archived"):  # hidden one-time voices never nag
            continue
        n = len(u.get("meetings", []))
        tray.append({"kind": "unknown_voice", "title": u["display"],
                     "detail": f"heard in {n} meeting{'s' if n != 1 else ''} — who is this?",
                     "target": u["uid"], "count": n})
    return timeline, tray


def _failed_at(st, name):
    """When `name`'s most recent (failed) result landed — for the history-only
    failure row's sort timestamp."""
    at = None
    for r in _results_rows() + list(st.get("recent", [])):
        if r.get("name") == name and (at is None or (r.get("at") or "") >= at):
            at = r.get("at")
    return at


def gather_state():
    from stt import holds, summarize
    _held = holds.items()
    st = status.read()
    m = manifest.load()
    src_dir, dst_dir = config.source_dir(), config.meetings_dir()
    queue = []
    # BOTH watched folders: the iCloud source AND the recorder's staging dir.
    # Only the source used to be listed, so a finished recording waited for the
    # batch completely invisibly — "I stopped, where did it go?"
    _qfiles, _qseen = [], set()
    for _qd in (src_dir, config.recordings_dir()):
        try:
            for p in sorted(_qd.iterdir()):
                if p.name not in _qseen:
                    _qseen.add(p.name)
                    _qfiles.append(p)
        except OSError:
            pass
    try:
        for p in _qfiles:
            if p.is_file() and p.suffix.lower() in config.AUDIO_EXTS and not p.name.startswith("."):
                done = manifest.is_processed(m, p.name, p.stat().st_mtime)
                dur = None if done else _est_duration(p)
                est_min, est_detail = None, None
                if dur:
                    ests = status.stage_estimates(dur)
                    est_min = round(sum(ests.values()) / 60)
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
                              "held": p.name in _held, "est_detail": est_detail})
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
    reg = unknowns.load()
    unknown_list = []
    for uid, meta in sorted(reg["speakers"].items()):
        if meta.get("dropped"):
            continue  # tombstoned "not a real speaker" — never surfaces again
        unknown_list.append({"uid": uid, "display": unknowns.display(uid),
                             "archived": bool(meta.get("archived")),
                             "meetings": meta.get("meetings", [])})
    enrolled = [{"name": n, "samples": meta.get("n_samples", 1),
                 "sources": [s for s in meta.get("sources", []) if s and s != "?"]}
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
    # a queued relabel (another one was mid-flight when requested): kick it
    # with a cooldown so a failed kick is retried rather than dropped forever
    pending = config.PROJECT_DIR / "relabel_pending.flag"
    if pending.exists():
        import time as _time
        if _time.monotonic() - _relabel_kicked.get("at", 0) > 60:
            _relabel_kicked["at"] = _time.monotonic()
            _spawn([str(RUN_SH), "relabel", "--all"])
    if not running:
        _kick_jobs()  # self-heal: idle with queued jobs -> start the next one
    active = st.get("active", {}) if running else {}
    n_active = max(1, len(active))
    active_out, active_eta_sum = {}, 0.0
    for name, entry in active.items():
        pct, eta = status.estimate_progress(entry, n_active)
        e = dict(entry)
        if pct is not None:
            e["pct"] = round(pct * 100)
            e["eta_sec"] = round(eta)
            active_eta_sum += eta
        bd = status.stage_breakdown(entry, n_active)
        if bd:
            e["stages"] = bd
        active_out[name] = e
    overall_eta = None
    if running:
        pend_secs = 0.0
        pend_names = set(st.get("pending", []))
        for f in queue:
            if f["name"] in pend_names and f.get("est_min"):
                pend_secs += f["est_min"] * 60
        overall_eta = round((active_eta_sum + pend_secs) / n_active) if (active_eta_sum or pend_secs) else None
    rec_state = _recording_state()  # resolved once: reused by the live banner AND the timeline
    timeline, tray = _timeline_tray(st, queue, meetings, active_out, unknown_list, rec_state)
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
            "queue": queue, "meetings": meetings,
            # the One Timeline: one row per meeting/file, changing state in place
            # (recording -> waiting/held -> processing -> needs_name -> ready |
            # failed), newest first, archived excluded; plus a ranked tray of
            # what needs the user. Both are strictly additive — every key above
            # keeps its shape so the current panel keeps working unchanged.
            "timeline": timeline, "tray": tray,
            "archived_count": len(config.archived_bases(dst_dir)),
            "enrolled": enrolled, "unknowns": unknown_list,
            "max_samples": identify.MAX_SAMPLES,
            "schedule": read_schedule(), "model": current_model(),
            "asr_choices": ASR_CHOICES, "battery": battery,
            "cloud_keys": _cloud_key_status(),
            "paths": {"source": str(src_dir), "dest": str(dst_dir)},
            "punctuate": _env_file_get()[0].get("STT_PUNCTUATE", "1") == "1",
            "mic_speaker": config.mic_speaker(),
            "recorder_ready": recorder.available(),
            "rates": rates.summary(),
            "relabel_pending": (config.PROJECT_DIR / "relabel_pending.flag").exists(),
            "llm_available": summarize.available(),
            "llm_backend": summarize.llm_backend(),
            "llm_backends": {b: summarize.backend_available(b)
                             for b in summarize.LLM_BACKENDS}}


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


def set_folder(which: str, path: str):
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"ok": False, "error": f"not a folder: {p}"}
    key = "STT_ICLOUD_DIR" if which == "source" else "STT_MEETINGS_DIR"
    _env_file_set({key: str(p)})
    import os
    os.environ[key] = str(p)  # children (batch runs) inherit immediately
    if which == "source":
        config.ICLOUD_DIR = p
        # the agent's WatchPaths must follow the new source folder
        for pl in (AGENT,):
            if pl.exists():
                d = plistlib.loads(pl.read_bytes())
                d["WatchPaths"] = _watch_paths()
                pl.write_bytes(plistlib.dumps(d))
        if AGENT.exists():
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], capture_output=True)
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


def _spawn(args):
    # `with` so the parent releases its copy of the log fd once the child has
    # its own dup — the long-lived panel process otherwise leaks one fd per spawn.
    with open(config.PROJECT_DIR / "logs" / "spawned.log", "a") as log:
        log.write(f"\n--- {' '.join(str(a) for a in args)}\n")
        log.flush()
        subprocess.Popen(args, cwd=str(config.PROJECT_DIR),
                         stdout=log, stderr=log, start_new_session=True)


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

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

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
        q = dict(urllib.parse.parse_qsl(u.query))
        if not _origin_allowed(self.headers):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            if u.path == "/":
                body = _page_html().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self._json(gather_state())
            elif u.path == "/api/snippet":
                meeting = q.get("meeting", "")
                if meeting and not _known_base(meeting):
                    # a stale reference (a meeting renamed/deleted before the
                    # registries tracked renames) must not kill playback — the
                    # client string is DISCARDED, never used as a path, and
                    # find_voice_clip searches the real library instead
                    meeting = ""
                try:
                    secs = float(q.get("secs", 30.0))  # default sample length for ▶ playback
                except ValueError:
                    secs = 30.0
                f = _snippet_for(meeting, q["speaker"], secs=secs)
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
                                  "minutes": round(d.get("duration_sec", 0) / 60)})
                items.sort(key=lambda r: r["date"] or "", reverse=True)
                self._json({"items": items})
            elif u.path == "/api/voice_clips":
                # for the "Who is this?" dialog: this unknown's longest turn in
                # EACH meeting it was heard in — meeting names come from the
                # SERVER's registry, never the client, so no base gate needed
                uid = q.get("speaker", "")
                meetings = (unknowns.load()["speakers"]
                            .get(uid, {}).get("meetings", []))[:5]
                clips = []
                for m in meetings:
                    if not _known_base(m):
                        continue  # stale reference (renamed before tracking)
                    cs = review.find_voice_clips(uid, m, n=1)
                    if cs:
                        c = cs[0]
                        clips.append({"meeting": m, "start": c["start"],
                                      "dur": round(c["dur"], 1),
                                      "index": c["index"]})
                self._json({"clips": clips})
            elif u.path == "/api/transcript":
                base = q["base"]
                if not self._require_base(base):
                    return
                j = config.meeting_file(base, ".json")
                if not j.exists():
                    self._json({"error": "no transcript"}, 404)
                    return
                d = json.loads(j.read_text())
                segs = [{"index": i, "start": s["start"], "end": s["end"],
                         "speaker": s.get("speaker"),
                         "who": s.get("display") or s.get("name") or s.get("speaker") or "?",
                         "text": s.get("text", ""),
                         "edited": bool(s.get("text_edited") or s.get("reviewed")),
                         "flags": s.get("flags", [])}
                        for i, s in enumerate(d.get("segments", []))
                        if s.get("text", "").strip()]
                self._json({"base": base, "strict": d.get("strict", False),
                            "duration_sec": d.get("duration_sec", 0),
                            "speakers": [s["display"] for s in d.get("speakers", [])],
                            "speaker_options": [{"id": s["id"], "display": s["display"]}
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
                self._json(search.query(q.get("q", "")))
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
            elif u.path == "/api/edits":
                if not self._require_base(q.get("base")):
                    return
                self._json({"n": review.count_decisions(q["base"])})
            elif u.path == "/api/suggest":
                if not self._require_base(q.get("base")):
                    return
                from stt import summarize
                self._json(summarize.suggest_title(q["base"]))
            elif u.path == "/api/check_updates":
                self._json(check_updates())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not _origin_allowed(self.headers):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            b = self._body()
            if u.path == "/api/run":
                # every run goes through the job queue: it starts immediately
                # when the machine is idle, and WAITS (visible in the panel)
                # when a batch is already running — a Redo is never lost.
                files = b.get("files") or []
                paths = b.get("paths") or []
                names = files + [Path(p).name for p in paths]
                label = (", ".join(names[:2]) + (f" +{len(names)-2} more" if len(names) > 2 else "")
                         ) if names else "all new recordings"
                busy = bool(control.snapshot()["pids"])
                jobs.add({"files": files, "paths": paths, "force": bool(b.get("force")),
                          "strict": bool(b.get("strict")), "verify": bool(b.get("verify")),
                          "onetime": bool(b.get("onetime")),
                          "parallel": int(b.get("parallel", 1)), "label": label})
                control.clear_stopping()  # an explicit new run overrides a recent Stop
                _jobs_kicked["at"] = 0.0  # a fresh click may bypass the cooldown
                _kick_jobs()
                self._json({"ok": True, "queued": busy})
            elif u.path == "/api/unqueue":
                self._json({"ok": jobs.remove(float(b["at"]))})
            elif u.path == "/api/pick_folder":
                p = pick_folder(b.get("prompt", "Choose a folder"))
                if p is None:
                    self._json({"cancelled": True})
                else:
                    self._json(set_folder(b["which"], p))
            elif u.path == "/api/set_folder":
                self._json(set_folder(b["which"], b["path"]))
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
                hh, mm = int(b["hour"]), int(b["minute"])
                assert 0 <= hh < 24 and 0 <= mm < 60
                write_schedule(hh, mm)
                self._json({"ok": True})
            elif u.path == "/api/model":
                choice = b["model"]
                if choice.startswith("cloud:"):
                    from stt import asr_cloud
                    prov = asr_cloud.provider_from_backend(choice)
                    if not asr_cloud.available(prov):
                        self._json({"ok": False, "error": "Add this provider's API key first (Cloud keys… in Settings)."})
                        return
                if choice.startswith("mlxwhisper"):
                    variant = choice.split(":", 1)[1] if ":" in choice else "large-v3"
                    _env_file_set({"STT_ASR_BACKEND": "mlxwhisper",
                                   "STT_WHISPER_MLX_MODEL": variant})
                else:
                    _env_file_set({"STT_ASR_BACKEND": choice})
                self._json({"ok": True, "model": current_model()})
            elif u.path == "/api/name":
                name = b["name"].strip()
                assert name
                if b.get("uid"):
                    ok = unknowns.promote(b["uid"], name)
                else:
                    if not self._require_base(b.get("meeting")):
                        return
                    from stt import diarcache
                    _, _, cent_emb, _ = diarcache.load(
                        config.meeting_file(b["meeting"], ".diar.npz"))
                    identify.enroll(name, cent_emb[b["speaker"]], source=b["meeting"])
                    ok = True
                if ok:
                    _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": ok, "note": "relabeling all meetings in background"})
            elif u.path == "/api/forget":
                self._json({"ok": unknowns.drop(b["uid"])})
            elif u.path == "/api/rename_speaker":
                ok = identify.rename_person(b["name"], b["new"])
                if ok:
                    _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": ok})
            elif u.path == "/api/remove_speaker":
                ok = identify.remove_person(b["name"])
                if ok:
                    _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": ok})
            elif u.path == "/api/merge_speakers":
                # src/dst are "uid:U003" or "name:Jordan"
                st_, sv = b["src"].split(":", 1)
                dt_, dv = b["dst"].split(":", 1)
                if st_ == "uid" and dt_ == "name":
                    ok = unknowns.promote(sv, dv)
                elif st_ == "uid" and dt_ == "uid":
                    ok = unknowns.merge(sv, dv)
                elif st_ == "name" and dt_ == "name":
                    ok = identify.merge_people(sv, dv)
                else:
                    ok = False  # merging a named person INTO an unknown is nonsense
                if ok:
                    _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": ok})
            elif u.path == "/api/rename":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.rename_meeting(b["base"], b["new"]))
            elif u.path == "/api/ask":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                if not summarize.available():
                    self._json({"error": "local LLM not installed"}, 503)
                    return
                q_ = (b.get("question") or "").strip()
                if not q_:
                    self._json({"error": "empty question"}, 400)
                    return
                if len(q_) > 2000:
                    self._json({"error": "question too long (2000 chars max)"}, 400)
                    return
                hist = b.get("history")
                try:
                    r = summarize.answer_question(
                        b["base"], q_, history=hist if isinstance(hist, list) else None)
                    self._json(r, 200 if r.get("ok") else 400)
                except summarize.LLMBusy:
                    self._json({"error": "The local model is busy with another "
                                         "summary or question. Try again in a "
                                         "minute."}, 503)
            elif u.path == "/api/hide_unknown":
                ok = (unknowns.archive(b["uid"]) if b.get("hide", True)
                      else unknowns.restore(b["uid"]))
                self._json({"ok": ok})
            elif u.path == "/api/cloud_keys":
                from stt import asr_cloud
                key_envs = {prov: meta["key_env"]
                            for prov, meta in asr_cloud.PROVIDERS.items()}
                key_envs["anthropic"] = "STT_ANTHROPIC_KEY"  # assistant, not ASR
                updates, remove = {}, set()
                cleared = {str(p) for p in (b.get("clear") or [])}
                for prov, key_env in key_envs.items():
                    v = (b.get(prov) or "").strip()
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
                backend = str(b.get("backend") or "")
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
                self._json(write_automation(
                    watch=(bool(b["watch"]) if "watch" in b else None),
                    nightly=(bool(b["nightly"]) if "nightly" in b else None)))
            elif u.path == "/api/set_date":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.set_meeting_date(b["base"], b.get("date", "")))
            elif u.path == "/api/set_category":
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.set_meeting_category(b["base"], b.get("category", "")))
            elif u.path == "/api/queue_hold":
                # park a waiting file: automatic runs skip it until released.
                # Same membership gate as preview/delete.
                from stt import holds
                f = _queue_file(b.get("name"))
                if f is None:
                    self._json({"ok": False, "error": "no such queued file"})
                    return
                self._json({"ok": True, "held": holds.toggle(f.name)})
            elif u.path == "/api/queue_delete":
                # bin a waiting source file (a bad take, a test recording) before
                # it ever becomes a meeting. Same membership gate as the preview.
                if not b.get("confirm"):
                    self._json({"ok": False, "error": "missing confirmation"})
                    return
                name = b.get("name")
                f = _queue_file(name)
                if f is None:
                    self._json({"ok": False, "error": "no such queued file"})
                    return
                if name in status.read().get("active", {}):
                    self._json({"ok": False,
                                "error": "this file is being processed right now"})
                    return
                mb = round(f.stat().st_size / 1e6, 1)
                f.unlink()
                # the recorder's channel-layout sidecar travels with its audio
                f.with_suffix(".opts.json").unlink(missing_ok=True)
                self._json({"ok": True, "freed_mb": mb})
            elif u.path == "/api/recorder_note":
                # dismiss the last-recording outcome strip
                status.clear_recorder_note()
                self._json({"ok": True})
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
            elif u.path == "/api/accept_meeting":
                # inbox: name/date/tag it, and mark it reviewed so it joins the list
                if not self._require_base(b.get("base")):
                    return
                from stt import summarize
                self._json(summarize.apply_meeting_edits(
                    b["base"], title=(b.get("name") or None),
                    date=(b.get("date") or None), category=b.get("category"),
                    reviewed=True))
            elif u.path == "/api/bulk":
                # multi-select actions. Every op below gates on live/archived
                # MEMBERSHIP internally before it builds a path, so an unknown or
                # traversal base is simply refused per-item — never applied.
                from stt import archive, summarize
                action = str(b.get("action") or "")
                value, confirmed = b.get("value"), bool(b.get("confirm"))
                results = []
                for bs in [str(x) for x in (b.get("bases") or [])][:500]:
                    if action == "category":
                        r = summarize.set_meeting_category(bs, value or "")
                    elif action == "date":
                        r = summarize.set_meeting_date(bs, value or "")
                    elif action == "rename":
                        # each keeps its OWN date stamp, so renaming a run of
                        # recurring meetings to one name still leaves them distinct
                        r = summarize.rename_meeting(bs, value or "")
                    elif action == "accept":
                        r = summarize.apply_meeting_edits(bs, reviewed=True)
                    elif action == "archive":
                        r = archive.archive_meeting(bs)
                    elif action == "restore":
                        r = archive.restore_meeting(bs)
                    elif action in ("delete", "drop_audio") and not confirmed:
                        r = {"ok": False, "error": "missing confirmation"}
                    elif action == "delete":
                        r = archive.delete_meeting(bs)
                    elif action == "drop_audio":
                        r = archive.drop_audio(bs)
                    else:
                        r = {"ok": False, "error": f"unknown action '{action}'"}
                    results.append({"base": bs, **r})
                self._json({"ok": all(r.get("ok") for r in results),
                            "n_ok": sum(1 for r in results if r.get("ok")),
                            "freed_mb": round(sum(r.get("freed_mb") or 0
                                                  for r in results), 1),
                            "results": results})
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
                self._json(archive.restore_meeting(str(b.get("base") or "")))
            elif u.path == "/api/delete_meeting":
                # live or archived; delete_meeting gates on membership in one
                # of the two lists before any path is built. confirm is a
                # seatbelt so nothing deletes on a malformed call.
                if not b.get("confirm"):
                    self._json({"ok": False, "error": "missing confirmation"})
                    return
                from stt import archive
                self._json(archive.delete_meeting(str(b.get("base") or "")))
            elif u.path == "/api/review":
                if not self._require_base(b.get("base")):
                    return
                if b.get("action") == "accept_minor":
                    self._json(review.accept_minor(b["base"]))
                    return
                if b.get("action") == "insert":
                    self._json(review.insert_segment(b["base"], b["start"], b["end"],
                                                     b["speaker"], b.get("text", "")))
                    return
                if b.get("action") == "delete":
                    self._json(review.delete_segment(b["base"], int(b["index"]),
                                                     start=b.get("start")))
                    return
                if b.get("action") == "split":
                    self._json(review.split_segment(
                        b["base"], int(b["index"]), start=b.get("start"),
                        text_a=b.get("text_a", ""), text_b=b.get("text_b", ""),
                        speaker_a=b.get("speaker_a"), speaker_b=b.get("speaker_b")))
                    return
                self._json(review.apply(b["base"], int(b["index"]), b["action"],
                                        start=b.get("start"), text=b.get("text"),
                                        speaker_id=b.get("speaker")))
            elif u.path == "/api/export":
                if not self._require_base(b.get("base")):
                    return
                if b["fmt"] == "reveal":
                    f = config.meeting_file(b["base"], ".txt")
                    subprocess.run(["open", "-R", str(f)], capture_output=True)
                    self._json({"ok": f.exists()})
                else:
                    path = export.export(b["base"], b["fmt"])
                    subprocess.run(["open", "-R", str(path)], capture_output=True)
                    self._json({"ok": True, "path": str(path)})
            elif u.path == "/api/retranscribe":
                if not self._require_base(b.get("base")):
                    return
                # one-shot subprocess: the panel must never hold a 2GB ASR model
                r = subprocess.run(
                    [str(config.PROJECT_DIR / ".venv/bin/python"), "-m", "stt.retranscribe",
                     b["base"], str(b["start"]), str(b["end"]),
                     b.get("engine") or "parakeet"],
                    capture_output=True, text=True, timeout=180,
                    cwd=str(config.PROJECT_DIR),
                    env={**__import__("os").environ,
                         "PYTHONPATH": str(config.PROJECT_DIR)})
                try:
                    self._json(json.loads(r.stdout.strip().splitlines()[-1]))
                except Exception:
                    self._json({"error": (r.stderr or "re-transcription failed")[-300:]}, 500)
            elif u.path == "/api/remove_sample":
                ok = identify.remove_sample(b["name"], int(b["index"]))
                if ok:
                    # a removed sample changes who this profile matches — re-run
                    # identification like every other registry edit, so a
                    # misattributed voice drops out of past transcripts (and
                    # resurfaces as an unknown to name) instead of the correction
                    # only taking effect on the next unrelated relabel
                    _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": ok, "note": "re-identifying all meetings in background"} if ok else
                           {"ok": False,
                            "error": "Can't remove the only sample — remove the person instead."})
            elif u.path == "/api/reassign_sample":
                r = identify.reassign_sample(b["name"], int(b["index"]), b.get("to", ""))
                if r.get("ok"):
                    _spawn([str(RUN_SH), "relabel", "--all"])  # moved voice, re-identify
                self._json(r)
            elif u.path == "/api/punctuate":
                _env_file_set({"STT_PUNCTUATE": "1" if b.get("on") else "0"})
                self._json({"ok": True})
            elif u.path == "/api/mic_speaker":
                # who "me" is on the meeting recorder's mic channel; enables the
                # channel-aware path for new recordings (they process as mono
                # until this is set and that person is enrolled)
                name = (b.get("name") or "").strip()
                if name:
                    _env_file_set({"STT_MIC_SPEAKER": name})
                else:
                    _env_file_set({}, remove=("STT_MIC_SPEAKER",))
                self._json({"ok": True, "mic_speaker": name or None})
            elif u.path == "/api/relabel":
                _spawn([str(RUN_SH), "relabel", "--all"])
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


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


def start_server():
    try:
        # one-time flat->folder layout migration + date backfill (idempotent)
        config.migrate_flat_meetings()
    except Exception:
        pass  # the panel must still start; the batch retries the migration
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_PAGE_PARTS = ("page.html", "app.css", "app.js")
_page_cache = {"mtimes": None, "html": None}


def _compose_page():
    """Assemble the served panel from its static parts. The CSS and JS are full
    of braces, so the skeleton carries marker comments and we substitute with
    str.replace -- never .format()/f-strings."""
    page = (_STATIC_DIR / "page.html").read_text(encoding="utf-8")
    css = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    return page.replace("/*@APP_CSS@*/", css).replace("//@APP_JS@", js)


def _page_html():
    """The composed page, recomposed only when a static part changes on disk --
    editing page.html/app.css/app.js reloads live, with no server restart."""
    mtimes = tuple((_STATIC_DIR / f).stat().st_mtime_ns for f in _PAGE_PARTS)
    if _page_cache["html"] is None or _page_cache["mtimes"] != mtimes:
        _page_cache["html"] = _compose_page()
        _page_cache["mtimes"] = mtimes
    return _page_cache["html"]


# Module-level constant kept for import-time consumers (tests, tooling); the
# HTTP handler uses _page_html() so file edits reload without a restart.
HTML = _compose_page()


if __name__ == "__main__":
    # Headless panel: serve the control panel WITHOUT the menu-bar app, on a
    # chosen port (STT_PANEL_PORT, default PORT). Paired with STT_HOME this runs
    # the panel against a synthetic data home for demos/screenshots — nothing
    # here touches real recordings. Only runs on direct execution; importing the
    # module for the menu bar or the tests is unaffected.
    PORT = int(os.environ.get("STT_PANEL_PORT", str(PORT)))
    start_server()
    print(f"STT control panel: http://127.0.0.1:{PORT}/   "
          f"(STT_HOME={config.STT_HOME or '(real data — none set)'})")
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()  # block forever; serve_forever runs in a daemon thread
    except KeyboardInterrupt:
        pass


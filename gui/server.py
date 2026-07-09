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
                 review, search, status, unknowns)

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
            d["WatchPaths"] = [str(config.source_dir())]
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
        meta = {"base": j.stem,
                # stored date wins (stamped at process time, human-editable);
                # filename/mtime derivation only for pre-migration jsons
                "date": d.get("date") or dates.meeting_date(j.stem)
                        or _date.fromtimestamp(audio_mtime).isoformat(),
                "minutes": round(d.get("duration_sec", 0) / 60),
                "speakers": [s["display"] for s in d.get("speakers", [])],
                "strict": d.get("strict", False),
                "flagged": sum(1 for s in d.get("segments", [])
                               if s.get("flags") and not review.is_minor(s)),
                "flagged_minor": sum(1 for s in d.get("segments", [])
                                     if s.get("flags") and review.is_minor(s)),
                "summary": d.get("ai_summary", ""),
                "next_steps": d.get("ai_next_steps", []),
                # when transcription last ran (new or redo). Older transcripts
                # predate this field — fall back to generated_at, then mtime.
                "processed_at": (d.get("processed_at") or d.get("generated_at")
                                 or _dt.fromtimestamp(
                                     j.stat().st_mtime).isoformat(timespec="seconds")),
                "audio": str(audio_p) if audio_p else None}
    except Exception:
        meta = None
    _meet_cache[key] = meta
    if len(_meet_cache) > 400:  # drop stale mtime generations
        for k in list(_meet_cache)[:200]:
            _meet_cache.pop(k, None)
    return meta


def gather_state():
    from stt import summarize
    st = status.read()
    m = manifest.load()
    src_dir, dst_dir = config.source_dir(), config.meetings_dir()
    queue = []
    try:
        for p in sorted(src_dir.iterdir()):
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
                              "est_detail": est_detail})
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
            "queue": queue, "meetings": meetings,
            "enrolled": enrolled, "unknowns": unknown_list,
            "max_samples": identify.MAX_SAMPLES,
            "schedule": read_schedule(), "model": current_model(),
            "asr_choices": ASR_CHOICES, "battery": battery,
            "cloud_keys": _cloud_key_status(),
            "paths": {"source": str(src_dir), "dest": str(dst_dir)},
            "punctuate": _env_file_get()[0].get("STT_PUNCTUATE", "1") == "1",
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
                d["WatchPaths"] = [str(p)]
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


def _snippet_for(meeting: str, speaker_key: str, secs: float = 12.0):
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
    log = open(config.PROJECT_DIR / "logs" / "spawned.log", "a")
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
                body = HTML.encode()
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
                    secs = float(q.get("secs", 12.0))
                except ValueError:
                    secs = 12.0
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
                data = audio_f.read_bytes()
                ctype = {"m4a": "audio/mp4", "mp4": "audio/mp4", "wav": "audio/wav",
                         "mp3": "audio/mpeg", "aiff": "audio/aiff"}.get(
                             audio_f.suffix[1:], "application/octet-stream")
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


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STT Workflow</title>
<style>
:root{--bg:#fff;--card:#fff;--ink:#0d0d0d;--sub:#6f6f6f;--line:#e6e6e6;
--accent:#635bff;--accent-h:#5851ea;--acc-ink:#fff;--ok:#107a3d;--warn:#a15c07;--bad:#d92d20;
--chip:#f2f2f2;--inset:#fafafa;--hairline:#ececec}
@media(prefers-color-scheme:dark){:root{--bg:#0a0a0a;--card:#111;--ink:#f2f2f2;
--sub:#9a9a9a;--line:#262626;--chip:#1f1f1f;--inset:#161616;--hairline:#222;
--accent:#7a73ff;--accent-h:#8b85ff;--acc-ink:#fff}}
/* manual override (theme toggle): must win over the OS preference BOTH ways */
:root[data-theme=dark]{--bg:#0a0a0a;--card:#111;--ink:#f2f2f2;
--sub:#9a9a9a;--line:#262626;--chip:#1f1f1f;--inset:#161616;--hairline:#222;
--accent:#7a73ff;--accent-h:#8b85ff;--acc-ink:#fff;color-scheme:dark}
:root[data-theme=light]{--bg:#fff;--card:#fff;--ink:#0d0d0d;--sub:#6f6f6f;
--line:#e6e6e6;--chip:#f2f2f2;--inset:#fafafa;--hairline:#ececec;
--accent:#635bff;--accent-h:#5851ea;--acc-ink:#fff;color-scheme:light}
.themebtn{width:32px;height:32px;border-radius:8px;padding:0;font-size:15px;
display:flex;align-items:center;justify-content:center;flex:none}
*{box-sizing:border-box;margin:0}
html{scroll-behavior:smooth}
body{font:14.5px/1.55 -apple-system,system-ui,"Segoe UI",sans-serif;background:var(--bg);
color:var(--ink);padding:0 28px 48px;max-width:880px;margin:0 auto;
-webkit-font-smoothing:antialiased}
h1{font:700 22px/1.15 -apple-system,system-ui,sans-serif;letter-spacing:-.03em}
h2{font:650 14px/1.2 -apple-system,system-ui,sans-serif;letter-spacing:-.015em;margin:0 0 12px;
display:flex;align-items:center;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:22px 24px;margin:18px 0}
/* Two tracks on wide windows: fluid work column + fixed reference column.
   minmax(0,1fr) + min-width:0 children so no content can force the track
   past its container (the failure mode that broke the old grid). */
@media(min-width:1180px){
  body{max-width:1280px}
  #cols{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;align-items:start}
  #colmain,#colside{min-width:0}
  #colside{position:sticky;top:66px}
  #spkcard h2{flex-wrap:wrap}
  #spkcard #spkfilter{width:100%;order:9;margin-top:6px}
  #spkcard .inset{max-height:calc(100vh - 330px)}
}
.row{display:flex;align-items:center;gap:10px;padding:10px 0;
border-bottom:1px solid var(--hairline)}
.row:last-child{border-bottom:0}
.inset{background:var(--inset);border:1px solid var(--hairline);border-radius:10px;
padding:2px 12px;max-height:44vh;overflow-y:auto;overscroll-behavior:contain}
.grow{flex:1;min-width:0}.name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--sub);font-size:13px}
.chip{background:transparent;border:1px solid var(--line);border-radius:99px;
padding:2px 10px;font-size:12px;font-weight:500;color:var(--sub);white-space:nowrap;
font-variant-numeric:tabular-nums}
.chip.live{border-color:transparent;background:color-mix(in srgb,var(--accent) 11%,transparent);color:var(--accent)}
.chip.done{border-color:transparent;background:color-mix(in srgb,var(--ok) 12%,transparent);color:var(--ok)}
.chip.warn{border-color:transparent;background:color-mix(in srgb,var(--warn) 13%,transparent);color:var(--warn)}
button{font:inherit;font-size:13.5px;font-weight:500;border:1px solid var(--line);
border-radius:8px;padding:6px 14px;cursor:pointer;background:var(--card);color:var(--ink);
transition:background .15s,border-color .15s,transform .02s}
button:hover{background:var(--chip);border-color:var(--sub)}
button:active{transform:scale(.98)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--acc-ink);font-weight:600}
button.primary:hover{background:var(--accent-h);border-color:var(--accent-h)}
button.danger{background:transparent;border-color:color-mix(in srgb,var(--bad) 40%,transparent);color:var(--bad)}
button:disabled{opacity:.4;cursor:default;transform:none}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 35%,transparent);outline-offset:1px}
.link{background:none;border:0;padding:2px 4px;color:var(--accent);font-size:13px;font-weight:500}
.link:hover{background:none;text-decoration:underline;text-underline-offset:3px}
input[type=text],select{font:inherit;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.top{display:flex;align-items:center;gap:13px;padding:22px 4px 14px;position:sticky;
top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);
backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
border-bottom:1px solid var(--hairline)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--sub);flex:none}
.dot.run{background:var(--accent);animation:pulse 1.2s infinite}
.dot.paused{background:var(--warn)}
@keyframes pulse{50%{opacity:.3}}
.bar{height:4px;border-radius:99px;background:var(--chip);overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;border-radius:99px;background:var(--accent);transition:width .6s}
.stagechips{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.stagechips .s{font-size:11px;font-weight:500;padding:2px 10px;border-radius:99px;
border:1px solid var(--hairline);color:var(--sub)}
.stagechips .s.on{background:var(--accent);border-color:var(--accent);color:var(--acc-ink)}
/* Slide-over flyouts (Settings, History, Ask): fixed-position panels openable
   at any scroll, so the page itself stays clean. */
.fly{position:fixed;top:0;right:0;height:100vh;width:min(460px,94vw);
background:var(--card);border-left:1px solid var(--line);
box-shadow:-24px 0 60px rgba(0,0,0,.25);padding:20px 24px 32px;
overflow-y:auto;overscroll-behavior:contain;z-index:40;
transform:translateX(105%);transition:transform .22s ease;visibility:hidden}
.fly.open{transform:none;visibility:visible}
#setfly select{max-width:200px}
#histfly{width:min(560px,94vw)}
/* Ask: a chat thread that fills the flyout, input pinned at the bottom */
#askfly{width:min(560px,94vw);display:flex;flex-direction:column}
#askthread{flex:1;overflow-y:auto;overscroll-behavior:contain;display:flex;
flex-direction:column;margin:6px -6px 0;padding:0 6px}
.bub{max-width:86%;padding:8px 12px;border-radius:14px;margin-top:10px;
white-space:pre-wrap;overflow-wrap:break-word;font-size:13.5px;line-height:1.45}
.bub.q{align-self:flex-end;background:color-mix(in srgb,var(--accent) 16%,var(--card));
border:1px solid color-mix(in srgb,var(--accent) 35%,transparent);border-bottom-right-radius:4px}
.bub.a{align-self:flex-start;background:var(--inset);border:1px solid var(--hairline);
border-bottom-left-radius:4px;color:var(--ink)}
.bub.warn{border-color:color-mix(in srgb,var(--warn) 55%,transparent)}
#flyveil{position:fixed;inset:0;background:rgba(0,0,0,.28);display:none;z-index:39}
#flyveil.open{display:block}
.checkbox{width:16px;height:16px;accent-color:var(--accent);flex:none}
dialog{margin:auto;border:1px solid var(--line);border-radius:14px;padding:26px;
background:var(--card);color:var(--ink);
box-shadow:0 20px 60px rgba(0,0,0,.2);max-width:440px;width:92%;
max-height:88vh;overflow-y:auto;overscroll-behavior:contain}
dialog::backdrop{background:rgba(0,0,0,.45)}
dialog h1{font-size:17px}
.timegrid{display:flex;gap:8px;align-items:center;margin:14px 0}
.seg{display:flex;background:var(--chip);border-radius:9px;padding:2px}
.seg button{background:transparent;border:0;padding:5px 12px;border-radius:7px}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.14)}
audio{width:100%;height:34px;margin-top:8px;border-radius:8px}
.muted{color:var(--sub);font-size:13px;line-height:1.55}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);
border-top-color:var(--accent);border-radius:50%;animation:rot .7s linear infinite;vertical-align:-2px}
@keyframes rot{to{transform:rotate(360deg)}}
.playbtn{width:30px;height:30px;border-radius:50%;padding:0;font-size:11px;
display:flex;align-items:center;justify-content:center;flex:none}
.playbtn:hover{background:var(--accent);border-color:var(--accent);color:var(--acc-ink)}
dialog.wide{max-width:680px}
.tseg{display:flex;gap:10px;padding:7px 8px;border-radius:8px;cursor:pointer;align-items:baseline}
.tseg:hover{background:var(--inset)}
.tseg.now{background:color-mix(in srgb,var(--accent) 9%,transparent)}
.tseg.flagged{background:color-mix(in srgb,var(--warn) 8%,transparent)}
.tseg .t{color:var(--sub);font-size:11.5px;font-family:ui-monospace,"SF Mono",Menlo,monospace;
flex:none;width:46px}
.tseg .w{font-weight:600;flex:none;width:118px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tseg .x{flex:1;min-width:0}
.sdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.toggle{min-width:52px}
.mgroup{font-size:11.5px;font-weight:600;color:var(--sub);text-transform:uppercase;
letter-spacing:.08em;padding:14px 0 3px;font-family:ui-monospace,"SF Mono",Menlo,monospace}
.mghdr{cursor:pointer;user-select:none}
.mghdr:hover{color:var(--ink)}
#meetings{position:relative;max-height:62vh}
#mrail{display:none;flex:none;flex-direction:column;gap:1px;padding:10px 0 0;
max-height:62vh;overflow-y:auto;overscroll-behavior:contain;
font-family:ui-monospace,"SF Mono",Menlo,monospace}
#mrail .yr{font-size:10.5px;font-weight:700;color:var(--sub);letter-spacing:.08em;padding:8px 8px 2px}
#mrail button{border:0;background:none;color:var(--sub);text-align:left;padding:2.5px 8px;
border-radius:6px;cursor:pointer;font-size:11.5px;font-family:inherit;letter-spacing:.04em;text-transform:uppercase}
#mrail button:hover{background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--ink)}
mark{background:color-mix(in srgb,var(--warn) 26%,transparent);color:inherit;border-radius:3px}
mark.cur{background:color-mix(in srgb,var(--accent) 30%,transparent);outline:1.5px solid var(--accent)}
.pnitem{padding:7px 10px;border-radius:6px;cursor:pointer}
.pnitem:hover,.pnitem.cur{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.tseg .segbtn{opacity:0;flex:none;padding:1px 8px;font-size:12px;border-radius:6px}
.tgap{height:8px;margin:0 8px;border-radius:6px;text-align:center;line-height:8px;
  font-size:11px;color:transparent;cursor:pointer;transition:all .12s}
.tgap:hover,.tgap:focus-visible{height:20px;line-height:20px;color:var(--accent);
  background:color-mix(in srgb,var(--accent) 7%,transparent)}
.tgap.editing{height:auto;line-height:normal;color:var(--ink);cursor:default;
  background:var(--inset);padding:10px}
.tseg:hover .segbtn,.tseg:focus-within .segbtn{opacity:1}
.tseg.editing{cursor:default;background:var(--inset)}
#tipbox{position:fixed;z-index:60;width:min(560px,80vw);background:var(--card);
  color:var(--ink);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;font-size:13px;line-height:1.55;
  box-shadow:0 12px 40px rgba(0,0,0,.22);pointer-events:none;display:none}
#tipbox ul{margin:6px 0 0 18px;padding:0}
#tipbox .tiphead{font-weight:600;margin-top:8px}
.mtitle,.mdate{cursor:text;border-radius:4px}
.mtitle:hover,.mdate:hover{background:var(--chip);
  box-shadow:0 0 0 4px var(--chip)}
.inline-edit{font:inherit;background:var(--card);color:var(--ink);
  border:1px solid var(--accent);border-radius:6px;padding:1px 6px}
.rvnav{width:30px;height:30px;padding:0;border-radius:8px;font-size:14px;
  display:flex;align-items:center;justify-content:center}
</style>
<script>
// theme: "auto" follows macOS; "light"/"dark" pin it. Applied pre-paint.
(function(){const q=new URLSearchParams(location.search).get("theme");
const t=q||localStorage.getItem("stt_theme");
if(t==="light"||t==="dark")document.documentElement.dataset.theme=t;})();
</script></head><body>
<div class="top">
  <h1>STT Workflow</h1>
  <span id="statusdot" class="dot"></span><span id="statustext" class="sub"></span>
  <span class="grow"></span>
  <button class="themebtn" id="setbtn" onclick="toggleSettings()" title="Settings">⚙</button>
  <button class="themebtn" id="themebtn" onclick="cycleTheme()" title=""></button>
  <span id="mem" class="chip" style="display:none" title="Memory held by transcription processes right now"></span>
  <span id="battery" class="chip"></span>
</div>

<div id="cols">
<div id="colmain">

<div class="card" id="activecard" style="display:none">
  <h2>Processing now</h2><div id="active"></div>
  <div class="row" style="border:0;padding-top:12px">
    <button class="danger" id="stopbtn" onclick="stopRun()">Stop processing</button>
    <span class="muted" id="stopnote">Stopping is safe — the current file’s original stays in iCloud and will re-run next time.</span>
  </div>
</div>

<div class="card">
  <h2>Queue <span id="qcount" class="chip"></span><span class="grow"></span>
    <button class="link" id="selall" onclick="selAll()">Select all</button></h2>
  <div id="queue" class="inset"></div>
  <div class="row" style="border:0;padding-top:12px">
    <button class="primary" id="runsel" onclick="runSelected()">Process selected</button>
    <button id="runall" onclick="runAll()">Process all new</button>
    <button id="runother" onclick="pickFiles()" title="Choose files from anywhere on disk">Other files…</button>
    <span class="grow"></span>
    <button id="pausebtn"></button>
  </div>
  <div style="display:flex;gap:22px;margin-top:10px">
    <label class="sub" style="white-space:nowrap"><input type="checkbox" id="par2" class="checkbox" style="vertical-align:-3px"> two at a time</label>
    <label class="sub" style="white-space:nowrap" title="For confidential conversations: never guess an uncertain speaker — flag for review instead"><input type="checkbox" id="strict" class="checkbox" style="vertical-align:-3px"> strict</label>
    <label class="sub" style="white-space:nowrap" title="A second engine transcribes too; the spots where the engines disagree get flagged for review with both versions. Adds a few minutes per hour of audio."><input type="checkbox" id="verify" class="checkbox" style="vertical-align:-3px"> verify</label>
    <label class="sub" style="white-space:nowrap" title="Focus groups, interviews with people you'll never need to identify: their voices are NOT added to the Speakers list (and no voice samples are kept for them). The transcript still labels them Speaker 1, 2… and you can still enroll someone from the meeting later."><input type="checkbox" id="onetime" class="checkbox" style="vertical-align:-3px"> one-time speakers</label>
  </div>
  <div class="muted" id="parnote" style="margin-top:6px">“Two at a time” uses ~10 CPU cores and gives ≈1.7× throughput. “Strict” never guesses an uncertain speaker — it flags for review (use for confidential conversations). “Verify” has a second engine listen too and flags the disagreements.</div>
  <div id="recentwrap" style="display:none"><h2 style="margin-top:16px">Recent results
    <span class="chip" title="The 5 latest results — the full permanent list is one click away">5 latest</span>
    <span class="grow"></span>
    <button onclick="openHistory()" title="Every file this pipeline ever processed, searchable and filterable">History…</button></h2><div id="recent"></div></div>
</div>

<aside id="histfly" class="fly">
  <h2 style="margin-bottom:4px">Processing history <span class="grow"></span>
    <button class="themebtn" onclick="flyToggle('#histfly',false)" title="Close history">✕</button></h2>
  <p class="muted" style="margin-top:6px">Every file this pipeline has processed, newest first. Failures keep their full error text.</p>
  <div style="display:flex;gap:8px;margin:12px 0 4px">
    <input type="text" id="histq" placeholder="Filter by name…" autocomplete="off" spellcheck="false"
      style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:6px 8px"
      oninput="histRender()">
    <select id="histok" onchange="histRender()" title="Show everything, or only one outcome" style="font-size:13px">
      <option value="">all</option><option value="ok">processed</option><option value="fail">failed</option>
    </select>
  </div>
  <div class="sub" id="histcount"></div>
  <div id="histlist"></div>
</aside>

<aside id="askfly" class="fly">
  <h2 style="margin-bottom:2px">Ask <span class="grow"></span>
    <button class="themebtn" onclick="flyToggle('#askfly',false)" title="Close (the thread stays until you ask about another meeting)">✕</button></h2>
  <div class="sub" id="askmeet" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
  <p class="muted" id="askpriv" style="margin-top:6px;font-size:12.5px"></p>
  <div id="askthread"></div>
  <div id="asknote" class="sub" style="margin-top:6px;flex:none"></div>
  <div style="display:flex;gap:8px;margin-top:8px;flex:none">
    <input type="text" id="askq" placeholder="e.g. What did we decide about the rollout?" autocomplete="off" spellcheck="false"
      style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:7px 10px"
      onkeydown="if(event.key==='Enter')askSend()">
    <button class="primary" id="askbtn" onclick="askSend()">Ask</button>
  </div>
</aside>

<aside id="setfly" class="fly">
  <h2 style="margin-bottom:4px">Settings <span class="grow"></span>
    <button class="themebtn" onclick="toggleSettings(false)" title="Close settings">✕</button></h2>
  <div class="row"><div class="grow"><div class="name">Folder watch</div>
    <div class="sub" id="watchnote"></div></div>
    <button class="toggle" id="watchbtn" onclick="togWatch()" title="On: a new recording starts processing within moments of landing. Off: files wait for the nightly run or a manual click."></button></div>
  <div class="row"><div class="grow"><div class="name">Nightly run</div>
    <div class="sub" id="schedtext"></div></div>
    <button class="toggle" id="nightbtn" onclick="togNightly()" title="On: everything new processes at the scheduled time. Independent of the folder watch."></button>
    <button onclick="openSchedule()" id="schedchg">Change…</button></div>
  <div class="row"><div class="grow"><div class="name">Transcription model</div>
    <div class="sub" id="modelnote"></div></div>
    <select id="modelsel" onchange="setModel()"></select></div>
  <div class="row"><div class="grow"><div class="name">Summaries &amp; Ask</div>
    <div class="sub" id="llmnote"></div></div>
    <select id="llmsel" onchange="setLlm()"></select></div>
  <div class="row"><div class="grow"><div class="name">Cloud transcription</div>
    <div class="sub" id="cloudnote"></div></div>
    <button onclick="openCloudKeys()">Cloud keys…</button></div>
  <div class="row"><div class="grow"><div class="name">Punctuation cleanup</div>
    <div class="sub">Restore punctuation &amp; casing (never changes words)</div></div>
    <button class="toggle" id="punctbtn" onclick="togglePunct()"></button></div>
  <div class="row"><div class="grow"><div class="name">Speed calibration</div>
    <div class="sub" id="ratesnote"></div></div></div>
  <div class="row"><div class="grow"><div class="name">Model updates</div>
    <div class="sub" id="updnote">Check HuggingFace for newer versions</div></div>
    <button onclick="checkUpdates()" id="updbtn">Check</button></div>
  <div class="row"><div class="grow"><div class="name">Watched folder</div>
    <div class="sub" id="srcpath" style="word-break:break-all"></div></div>
    <button onclick="pickFolder('source')">Change…</button></div>
  <div class="row"><div class="grow"><div class="name">Transcripts folder</div>
    <div class="sub" id="dstpath" style="word-break:break-all"></div></div>
    <button onclick="pickFolder('dest')">Change…</button></div>
</aside>
<div id="flyveil" onclick="flyCloseAll()"></div>

<div class="card">
  <h2>Transcripts <span class="grow"></span>
    <select id="msort" onchange="localStorage.setItem('stt_msort',this.value);render()" title="How the list is ordered and grouped" style="font-size:13px">
      <option value="date">by month</option>
      <option value="name">by name</option>
    </select>
    <input type="text" id="mfilter" placeholder="Search words or titles…"
           oninput="render();scheduleSearch()" style="width:min(340px,45vw);font-size:13px"></h2>
  <div id="searchhits"></div>
  <div style="display:flex;gap:10px;align-items:stretch">
    <div id="mrail"></div>
    <div id="meetings" class="inset" style="flex:1;min-width:0"></div>
  </div>
</div>

</div>
<aside id="colside">

<div class="card" id="spkcard">
  <h2>Speakers <span class="grow"></span>
    <label class="sub" style="font-weight:400;white-space:nowrap" title="Show only voices without a name yet"><input type="checkbox" id="spkunk" class="checkbox" style="vertical-align:-2px" onchange="render()"> unidentified only</label>
    <input type="text" id="spkfilter" placeholder="Find a speaker…" oninput="render()" style="width:min(220px,34vw);font-size:13px"></h2>
  <div class="inset">
    <div id="enrolled"></div>
    <div id="unknowns"></div>
  </div>
  <div id="relnote" class="muted" style="display:none;margin-top:8px"></div>
</div>

</aside>
</div>

<dialog id="dlg"></dialog>
<div id="tipbox"></div>
<script>
const $=q=>document.querySelector(q);
function fmtEta(sec){if(sec==null)return'';if(sec<90)return'1 min';
  if(sec<3600)return Math.round(sec/60)+' min';
  return Math.floor(sec/3600)+'h '+String(Math.round(sec%3600/60)).padStart(2,'0')+'m'}
function fmtM(sec){if(sec==null)return'?';if(sec<60)return Math.max(1,Math.round(sec))+'s';
  return Math.round(sec/60)+'m'}
function stageLine(st){ // done: actual · active: elapsed of expected · ahead: expected
  if(!st)return'';
  const parts=st.filter(x=>x.state==='active'||(x.secs||x.est||0)>=30).map(x=>{
    const nice=STAGE_NICE[x.stage]||x.stage;
    if(x.state==='done')return`${nice} ${fmtM(x.secs)} ✓`;
    if(x.state==='active'){
      const over=x.est&&x.secs>x.est;
      return`<b>${nice} ${fmtM(x.secs)} of ~${fmtM(x.est)}</b>${over?' (running long — still working)':''}`;
    }
    return`then ${nice} ~${fmtM(x.est)}`;
  });
  return parts.length?`<div class="sub" style="margin-top:3px">${parts.join(' · ')}</div>`:'';
}
let S=null, selected=new Set(), showHidden=false;
// collapsible transcript groups: overrides persist per sort mode; the newest
// month (or first letter) is open unless the user collapsed it
const MG={ov:JSON.parse(localStorage.getItem('stt_mgroups')||'{}'),keys:[],sort:'date'};
function mgToggle(key,open){
  MG.ov[MG.sort+':'+key]=open?1:0;
  localStorage.setItem('stt_mgroups',JSON.stringify(MG.ov));
  render();
}
function mgJump(i){
  const key=MG.keys[i];
  if(key===undefined)return;
  if(MG.ov[MG.sort+':'+key]!==1){mgToggle(key,1)}
  const el=document.getElementById('mg-'+i),c=$('#meetings');
  if(el&&c)c.scrollTop=el.offsetTop-6;
}
async function api(p,body){const r=await fetch(p,body?{method:'POST',body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return (s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// For values embedded as a JS string literal INSIDE an onclick="..." attribute (e.g. onclick="f('${escJs(x)}')").
// esc() alone breaks there: an apostrophe closes the JS string early, and HTML-entity-encoding it
// (&#39;) doesn't help — the browser HTML-decodes the attribute before compiling it as JS, so the
// entity turns back into a literal quote first. Backslash-escaping survives that decode step.
function escJs(s){return esc(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")}

const STAGES=['downloading','converting','transcribing','diarizing','verifying','writing','summarizing'];
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',diarizing:'Speakers',verifying:'Verifying',writing:'Writing',summarizing:'Summary'};

function render(){
  const s=S;
  // header
  $('#statusdot').className='dot '+(s.running?'run':(s.paused?'paused':''));
  $('#statustext').textContent=s.running?'processing':(s.paused?'automatic runs paused':'idle');
  $('#battery').textContent=s.battery;
  const gb=s.mem_mb>=1024?(s.mem_mb/1024).toFixed(1)+' GB':(s.mem_mb+' MB');
  $('#mem').style.display=s.mem_mb>0?'inline':'none';
  $('#mem').textContent='mem '+gb;
  // active
  const act=Object.entries(s.active||{});
  const orphaned=s.running&&!act.length&&s.mem_mb>500;
  $('#activecard').style.display=s.running?'block':'none';
  $('#active').innerHTML=(orphaned?`<div class="row"><span class="chip warn">recovering</span>
    <div class="grow"><div class="name">Background workers are still running</div>
    <div class="sub">They hold ${gb} of memory — “Stop processing” shuts down the whole group and verifies it.</div></div></div>`:'')
  +(act.length?act.map(([n,a])=>{
    const idx=STAGES.indexOf(a.stage);
    const pct=a.pct!=null?a.pct:null;
    return `<div class="row"><div class="grow"><div class="name">${esc(n.replace(/\.[^.]+$/,''))}</div>
    <div class="stagechips">${STAGES.filter(st=>(st!=='verifying'||a.stage==='verifying')&&(st!=='summarizing'||a.stage==='summarizing')).map((st,i)=>`<span class="s ${i<=idx?'on':''}">${STAGE_NICE[st]}</span>`).join('')}</div>
    ${pct!=null?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}
    ${stageLine(a.stages)}
    </div><div style="text-align:right;min-width:86px">${pct!=null?`<div class="name">${pct}%</div><div class="sub">≈ ${fmtEta(a.eta_sec)} left</div>`:'<span class="spin"></span>'}</div></div>`;
  }).join(''):(orphaned?'':'<div class="sub">Starting…</div>'))
  +(s.overall_eta_sec?`<div class="sub" style="padding-top:10px">Everything queued: ≈ ${fmtEta(s.overall_eta_sec)} remaining</div>`:'');
  // queued panel runs (redos / hand-picked) — waiting for the current run to finish
  const qjobs=(s.queued_jobs||[]).map(j=>
    `<div class="row"><span style="width:17px"></span><div class="grow"><div class="name">↻ ${esc(j.label)}</div><div class="sub">requested run${j.strict?' · strict':''}${j.verify?' · verify':''}</div></div><span class="chip live">${s.running?'starts after current run':'starting…'}</span><button class="segbtn" title="Cancel this queued run" onclick="api('/api/unqueue',{at:${j.at}}).then(r=>{if(!r.ok)alert('Could not cancel — it may have already started.');refresh()})">✕</button></div>`).join('');
  // queue
  const newFiles=s.queue.filter(f=>!f.processed);
  $('#qcount').textContent=newFiles.length+' new';
  $('#queue').innerHTML=qjobs+(s.queue.length?s.queue.map(f=>{
    const running=s.active&&s.active[f.name];
    const pend=(s.pending||[]).includes(f.name);
    const chip=running?'<span class="chip live">processing</span>':pend?'<span class="chip live">queued</span>':f.processed?'<span class="chip done">done</span>':(f.video?'<span class="chip">video</span>':'');
    const box=(!f.processed&&!running&&!pend)?`<input type="checkbox" class="checkbox" ${selected.has(f.name)?'checked':''} onchange="tog('${escJs(f.name)}',this.checked)">`:'<span style="width:17px"></span>';
    const est=(!f.processed&&f.est_min)?` · <span ${f.est_detail?`title="${esc(f.est_detail)}"`:''}>~${f.est_min} min to process</span>`:'';
    return `<div class="row">${box}<div class="grow"><div class="name">${esc(f.name)}</div><div class="sub">${f.size_mb} MB${est}</div></div>${chip}</div>`;
  }).join(''):(qjobs?'':'<div class="sub">Nothing in the iCloud folder.</div>'));
  $('#runsel').disabled=!selected.size||s.running;
  $('#runall').disabled=s.running||!newFiles.length;
  $('#runother').disabled=false;  // extra picks queue behind the current run now
  const selectable=newFiles.filter(f=>!(s.active&&s.active[f.name])&&!(s.pending||[]).includes(f.name));
  $('#selall').style.display=selectable.length>1?'inline':'none';
  $('#selall').textContent=selected.size>=selectable.length&&selectable.length?'Deselect all':'Select all';
  // pause button
  $('#pausebtn').textContent=s.paused?'Resume automatic runs':'Pause automatic runs';
  $('#pausebtn').onclick=()=>api(s.paused?'/api/resume':'/api/pause',{}).then(refresh);
  // recent results (successes and failures)
  const rec=s.recent||[];
  $('#recentwrap').style.display=rec.length?'block':'none';
  $('#recent').innerHTML=rec.slice(0,5).map(r=>`<div class="row">
    <span class="chip ${r.ok?'done':'warn'}">${r.ok?'✓':'failed'}</span>
    <div class="grow"><div class="name">${esc(r.name.replace(/\.[^.]+$/,''))}</div>
    <div class="sub">${esc(r.summary||'')}${r.ok?'':' · original kept in iCloud — will retry next run'}</div></div>
    <span class="sub">${(r.at||'').slice(5,16).replace('T',' ')}</span></div>`).join('');
  // punctuation toggle
  $('#punctbtn').textContent=s.punctuate?'On':'Off';
  $('#punctbtn').style.color=s.punctuate?'var(--ok)':'var(--sub)';
  // learned speed rates
  $('#ratesnote').textContent=s.rates&&s.rates.runs
    ?`Measured from ${s.rates.runs} run${s.rates.runs>1?'s':''}: ${s.rates.text} realtime — estimates improve automatically`
    :'Estimates use factory measurements until a few runs complete';
  // speakers (▶ = one-tap voice playback, found automatically in recent meetings)
  const sq=($('#spkfilter').value||'').trim().toLowerCase();
  const unkOnly=$('#spkunk').checked;
  const smatch=t=>!sq||(t||'').toLowerCase().includes(sq);
  const enr=unkOnly?[]:s.enrolled.filter(e=>smatch(e.name));
  $('#enrolled').innerHTML=enr.map(e=>`<div class="row">
    <button class="playbtn" data-key="${esc(e.name)}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(e.name)}</div>
    <div class="sub" title="${esc((e.sources||[]).join(', '))}">${e.samples} voice sample${e.samples>1?'s':''}${e.sources&&e.sources.length?' · from '+esc(e.sources[e.sources.length-1])+(e.sources.length>1?' +'+(e.sources.length-1):''):''}</div></div>
    <button onclick="openSpeakerActions('name:${escJs(e.name)}','${escJs(e.name)}','')">⋯</button></div>`).join('')
    ||(unkOnly?'':`<div class="sub">${sq?'No enrolled speaker matches “'+esc(sq)+'”.':'No one enrolled yet.'}</div>`);
  // the unknowns search also matches the meetings a voice was heard in, so
  // "who was that in Tuesday's sync?" is findable by the meeting's name
  const vis=s.unknowns.filter(u=>!u.archived&&(smatch(u.display)||u.meetings.some(smatch))),
        hid=s.unknowns.filter(u=>u.archived&&(smatch(u.display)||u.meetings.some(smatch)));
  $('#unknowns').innerHTML=vis.map(u=>`<div class="row">
    <button class="playbtn" data-key="${u.uid}" data-meeting="${esc(u.meetings[0]||'')}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(u.display)}</div>
    <div class="sub">heard in ${u.meetings.length} meeting${u.meetings.length>1?'s':''}</div></div>
    <button class="primary" onclick="openName('${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">Who is this?</button>
    <button onclick="openSpeakerActions('uid:${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">⋯</button></div>`).join('')
    +(hid.length?`<div class="sub" style="padding:8px 0 2px"><button class="link" onclick="showHidden=!showHidden;render()">${showHidden?'▾':'▸'} ${hid.length} hidden</button></div>`
      +(showHidden?hid.map(u=>`<div class="row" style="opacity:.6">
        <button class="playbtn" data-key="${u.uid}" data-meeting="${esc(u.meetings[0]||'')}" onclick="playVoice(this)">▶</button>
        <div class="grow"><div class="name">${esc(u.display)}</div>
        <div class="sub">hidden · heard in ${u.meetings.length} meeting${u.meetings.length>1?'s':''}</div></div>
        <button onclick="api('/api/hide_unknown',{uid:'${escJs(u.uid)}',hide:false}).then(refresh)">Restore</button></div>`).join(''):''):'')
    ||`<div class="sub" style="padding-top:8px">${sq?'No unidentified voice matches “'+esc(sq)+'”.':'No unidentified voices right now.'}</div>`;
  $('#relnote').style.display=s.relabel_pending?'block':'none';
  $('#relnote').textContent='Applying names to all transcripts… (moments)';
  // settings — the two automatic triggers, independently switchable
  const sc=s.schedule;
  $('#watchbtn').textContent=sc.watch?'On':'Off';
  $('#watchbtn').style.color=sc.watch?'var(--ok)':'var(--sub)';
  $('#watchbtn').disabled=!sc.installed;
  $('#watchnote').textContent=!sc.installed
    ?'Not installed — run ./setup.sh install-agent'
    :(sc.watch?'New recordings process within moments of landing (while the Mac is awake)'
              :'Off — new files wait for the nightly run or a manual click');
  $('#nightbtn').textContent=sc.nightly?'On':'Off';
  $('#nightbtn').style.color=sc.nightly?'var(--ok)':'var(--sub)';
  $('#nightbtn').disabled=!sc.installed;
  $('#schedchg').style.display=sc.nightly?'inline-block':'none';
  $('#schedtext').textContent=!sc.installed?'Not installed'
    :(sc.nightly
      ?new Date(2000,0,1,sc.hour,sc.minute).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+' · runs at next wake if the Mac is asleep'
      :'Off — turn on to process everything new at a set time');
  const sel=$('#modelsel');
  const pickable=s.asr_choices.filter(c=>!c.cloud||(s.cloud_keys||{})[c.cloud]);
  sel.innerHTML=pickable.map(c=>`<option value="${c.id}" ${c.id===s.model?'selected':''}>${c.label}</option>`).join('');
  $('#modelnote').textContent=(s.asr_choices.find(c=>c.id===s.model)||{}).note||'';
  const nk=['scribe','openai','voxtral'].filter(p=>(s.cloud_keys||{})[p]).length;
  $('#cloudnote').textContent=nk?`${nk} provider key${nk>1?'s':''} set — cloud engines appear in the model picker`:'Optional: bring your own API key (ElevenLabs · OpenAI · Mistral)';
  // assistant backend picker (summaries & Ask)
  const LB={local:'Local Qwen3-8B',anthropic:'Claude Haiku · cloud',openai:'OpenAI GPT · cloud'};
  const av=s.llm_backends||{};
  $('#llmsel').innerHTML=Object.keys(LB).map(b=>
    `<option value="${b}" ${b===s.llm_backend?'selected':''} ${av[b]?'':'disabled'}>${LB[b]}${av[b]?'':' (no key)'}</option>`).join('');
  $('#llmnote').textContent=s.llm_backend==='local'
    ?(av.local?'Runs on this Mac — transcripts never leave it':'Local model not installed — pick a cloud assistant or install .venv-llm')
    :'Cloud assistant: transcript text uploads for summaries and Ask. Strict recordings always stay local.';
  $('#srcpath').textContent=s.paths.source.replace(/^\/Users\/[^/]+/,'~');
  $('#dstpath').textContent=s.paths.dest.replace(/^\/Users\/[^/]+/,'~');
  // meetings: filter by title/speaker, newest meeting-date first, in
  // collapsible groups (month, or first letter when sorted by name)
  const mq=($('#mfilter').value||'').toLowerCase();
  const msort=($('#msort')&&$('#msort').value)||'date';
  const shown=s.meetings.filter(m=>!mq||m.base.toLowerCase().includes(mq)
    ||m.speakers.join(' ').toLowerCase().includes(mq))
    .slice().sort(msort==='name'
      ?(a,b)=>a.base.toLowerCase().localeCompare(b.base.toLowerCase())
      :(a,b)=>(b.date||'').localeCompare(a.date||''));
  if(document.querySelector('#meetings .inline-edit'))return;  // typing in place — don't wipe it
  const groups=[];
  for(const m of shown){
    const key=msort==='name'
      ?(/^[a-z]/i.test(m.base)?m.base[0].toUpperCase():'#')
      :(m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{month:'long',year:'numeric'}):'Undated');
    if(!groups.length||groups[groups.length-1].key!==key)groups.push({key,rows:[]});
    groups[groups.length-1].rows.push(m);
  }
  MG.keys=groups.map(g=>g.key);MG.sort=msort;
  const mgOpen=(k,i)=>{
    if(mq)return true;  // searching: every group with matches shows its rows
    const ov=MG.ov[msort+':'+k];
    return ov!==undefined?!!ov:i===0;  // newest month / first letter open by default
  };
  const mrow=m=>{
    const day=m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}):'';
    return `<div class="row"><div class="grow${m.summary?' hastip':''}" data-base="${esc(m.base)}">
    <div class="name"><span class="mtitle" onclick="inlineRename('${escJs(m.base)}',event)" title="Click to rename">${esc(m.base)}</span></div>
    <div class="sub">${day?`<span class="mdate" onclick="inlineDate('${escJs(m.base)}','${esc(m.date)}',event)" title="Click to change the meeting date">${day}</span> · `:''}${m.minutes} min · ${m.speakers.map(esc).join(', ')}${m.strict?' · strict':''}
      ${m.flagged?` <span class="chip warn" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Step through each uncertain segment with its audio — accept or fix it">⚠ ${m.flagged} to review</span>`:(m.flagged_minor?` <span class="chip" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Only sub-second crosstalk crumbs — bulk-accept or skim them">${m.flagged_minor} minor</span>`:'')}</div>
    ${m.summary?`<div class="sub" style="margin-top:3px;font-style:italic">${esc(m.summary.length>150?m.summary.slice(0,150)+'…':m.summary)}</div>`:''}</div>
    <button class="primary" onclick="openTranscript('${escJs(m.base)}')">Read</button>
    <button onclick="openSummary('${escJs(m.base)}')">Summary</button>
    <button onclick="openAsk('${escJs(m.base)}')" ${S.llm_available?'':'disabled'} title="${S.llm_available?'Ask questions about this meeting, answered on this Mac':'Needs the local model (.venv-llm) installed'}">Ask</button>
    <button onclick="openMeetingMenu('${escJs(m.base)}')" title="Export, rename, reprocess…">⋯</button>
  </div>`};
  $('#meetings').innerHTML=groups.map((g,i)=>{
    const openG=mgOpen(g.key,i);
    return `<div class="mgroup mghdr" id="mg-${i}" onclick="mgToggle('${escJs(g.key)}',${openG?0:1})" title="${openG?'Collapse':'Expand'} ${esc(g.key)}"><span style="display:inline-block;width:15px">${openG?'▾':'▸'}</span>${esc(g.key)} · ${g.rows.length}</div>`
      +(openG?g.rows.map(mrow).join(''):'');
  }).join('')||`<div class="sub">${mq?'No transcript titles match “'+esc(mq)+'”.':'No transcripts yet — process something above.'}</div>`;
  // jump rail: one entry per group, year markers between months
  const rail=$('#mrail');
  if(groups.length>=3&&!mq){
    let lastYr='';
    rail.innerHTML=groups.map((g,i)=>{
      let h='';
      if(msort==='date'){
        const yr=(g.key.match(/\d{4}/)||[''])[0];
        if(yr&&yr!==lastYr){h=`<div class="yr">${yr}</div>`;lastYr=yr;}
        return h+`<button onclick="mgJump(${i})" title="Jump to ${esc(g.key)} (${g.rows.length})">${g.key==='Undated'?'—':esc(g.key.slice(0,3))}</button>`;
      }
      return `<button onclick="mgJump(${i})" title="Jump to ${esc(g.key)} (${g.rows.length})">${esc(g.key)}</button>`;
    }).join('');
    rail.style.display='flex';
  }else rail.style.display='none';
  _syncVoiceBtns();  // re-render rebuilt the ▶ buttons; restore ◼ on the playing one
}
// Voice-sample playback is tracked by speaker KEY (not DOM node), because the
// panel re-renders every 2s and rebuilds the buttons — the stop (◼) state must
// survive that, so a click always stops the sample that's playing.
let voiceAudio=null, voiceKey=null;
function _syncVoiceBtns(){
  const playing=voiceAudio&&!voiceAudio.paused;
  document.querySelectorAll('.playbtn').forEach(b=>{
    b.textContent=(playing&&b.dataset.key===voiceKey)?'◼':'▶';
    b.title=(playing&&b.dataset.key===voiceKey)?'Stop':'Play a short sample of this voice';
  });
}
function stopVoice(){
  if(voiceAudio)voiceAudio.pause();
  voiceAudio=null;voiceKey=null;_syncVoiceBtns();
}
function playVoice(btn){
  const key=btn.dataset.key;
  if(voiceKey===key&&voiceAudio&&!voiceAudio.paused){stopVoice();return}  // toggle off
  if(voiceAudio)voiceAudio.pause();
  document.querySelectorAll('audio').forEach(a=>a.pause());  // exclusive playback
  const mtg=btn.dataset.meeting||'';
  voiceKey=key;btn.textContent='…';
  voiceAudio=new Audio('/api/snippet?speaker='+encodeURIComponent(key)+(mtg?'&meeting='+encodeURIComponent(mtg):''));
  voiceAudio.onplaying=_syncVoiceBtns;
  voiceAudio.onended=voiceAudio.onerror=()=>{if(voiceKey===key)stopVoice()};
  voiceAudio.play().catch(()=>{if(voiceKey===key)stopVoice()});
}
function tog(n,on){on?selected.add(n):selected.delete(n);$('#runsel').disabled=!selected.size}
function selAll(){
  const sel=S.queue.filter(f=>!f.processed&&!(S.active&&S.active[f.name])&&!(S.pending||[]).includes(f.name)).map(f=>f.name);
  if(selected.size>=sel.length){selected.clear()}else{sel.forEach(n=>selected.add(n))}
  render();
}
function runOpts(){return {parallel:$('#par2').checked?2:1,strict:$('#strict').checked,verify:$('#verify').checked,onetime:$('#onetime').checked}}
function runSelected(){api('/api/run',{files:[...selected],...runOpts()}).then(()=>{selected.clear();refresh()})}
function runAll(){api('/api/run',runOpts()).then(refresh)}
async function pickFiles(){
  const r=await api('/api/pick_files',{});
  if(r.cancelled||!r.paths?.length)return;
  await api('/api/run',{paths:r.paths,...runOpts()});
  refresh();
}
function togglePunct(){api('/api/punctuate',{on:!S.punctuate}).then(refresh)}

// ---- full-text search across all transcripts ----
let searchTimer=null;
function scheduleSearch(){
  clearTimeout(searchTimer);
  const q=$('#mfilter').value.trim();
  if(q.length<3){$('#searchhits').innerHTML='';return}
  searchTimer=setTimeout(async()=>{
    const r=await api('/api/search?q='+encodeURIComponent(q));
    if($('#mfilter').value.trim().toLowerCase()!==r.query)return; // stale response
    const rx=new RegExp(r.query.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
    const hl=s=>esc(s).replace(rx,m=>'<mark>'+m+'</mark>');
    $('#searchhits').innerHTML=r.hits.length?`
      <div class="mgroup">Said in transcripts — ${r.total} match${r.total>1?'es':''}</div>
      <div class="inset" style="max-height:30vh;margin-bottom:10px">
      ${r.hits.map(h=>{const mm=Math.floor(h.start/60),ss=String(Math.floor(h.start%60)).padStart(2,'0');
        return `<div class="tseg" onclick="openTranscript('${escJs(h.base)}',${h.index})" title="Open the transcript at this moment">
        <span class="t">${mm}:${ss}</span><span class="w">${esc(h.who)}</span>
        <span class="x">${hl(h.snippet)}<span class="sub"> — ${esc(h.base)}</span></span></div>`}).join('')}
      </div>`:`<div class="sub" style="padding:8px 0">Nothing in any transcript matches “${esc(r.query)}”.</div>`;
  },250);
}

// ---- per-meeting menu: export / copy / reveal / rename / reprocess ----
function openMeetingMenu(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(base)}</h1>
  <div class="row" style="margin-top:8px"><div class="grow"><div class="name">Export as Word</div>
    <div class="sub">Styled .docx → Downloads</div></div>
    <button onclick="doExport('${escJs(base)}','docx',this)">Export</button></div>
  <div class="row"><div class="grow"><div class="name">Export as PDF</div>
    <div class="sub">Print-ready → Downloads</div></div>
    <button onclick="doExport('${escJs(base)}','pdf',this)">Export</button></div>
  <div class="row"><div class="grow"><div class="name">Copy transcript</div>
    <div class="sub">Plain text to the clipboard</div></div>
    <button onclick="copyTxt('${escJs(base)}',this)">Copy</button></div>
  <div class="row"><div class="grow"><div class="name">Show files</div>
    <div class="sub">Reveal the .txt / .json in Finder</div></div>
    <button onclick="api('/api/export',{base:'${escJs(base)}',fmt:'reveal'})">Reveal</button></div>
  <div class="row"><div class="grow"><div class="name">Rename</div>
    <div class="sub">Retitle this recording (updates all files)</div></div>
    <button onclick="dlg.close();openRename('${escJs(base)}')">Rename…</button></div>
  ${m.audio?`<div class="row" style="border:0"><div class="grow"><div class="name">Reprocess</div>
    <div class="sub">Re-run transcription + speakers from the stored audio</div></div>
    <button onclick="dlg.close();openRedo('${escJs(base)}','${escJs(m.audio)}')">Redo…</button></div>`:''}
  <div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="dlg.close()">Close</button></div>`;
  dlg.showModal();
}
async function doExport(base,fmt,btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>';
  const r=await api('/api/export',{base,fmt});
  btn.disabled=false;btn.textContent=r.ok?'Done ✓':'Failed';
  if(r.error)alert(r.error);
}
async function copyTxt(base,btn){
  const txt=await fetch('/api/txt?base='+encodeURIComponent(base)).then(r=>r.text());
  await navigator.clipboard.writeText(txt);
  btn.textContent='Copied ✓';
}

// ---- review flow: step through flagged segments with their audio ----
let RV=null;
async function openReview(base){
  const d=await api('/api/review?base='+encodeURIComponent(base));
  if(d.error){alert(d.error);return}
  if(!d.items||!d.items.length){alert('Nothing left to review — all resolved.');refresh();return}
  RV={...d,i:0};
  $('#dlg').classList.add('wide');
  dlg.onkeydown=e=>{ // ←/→ flip between flagged segments unless typing
    if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;
    if(e.key==='ArrowLeft'){e.preventDefault();rvGo(-1)}
    if(e.key==='ArrowRight'){e.preventDefault();rvGo(1)}
  };
  dlg.onclose=()=>{const a=$('#rva');if(a)a.pause();dlg.onclose=null;dlg.onkeydown=null;$('#dlg').classList.remove('wide');refresh()};
  renderReview();
  dlg.showModal();
}
function renderReview(){
  const it=RV.items[RV.i];
  const minorLeft=RV.items.slice(RV.i).filter(x=>x.minor).length;
  const alts=(it.alt||[]).map((a,k)=>`<div class="sub" style="margin-top:4px">Second engine heard “<b>${esc(a.theirs||'(nothing)')}</b>” where this says “${esc(a.ours||'(nothing)')}” <button style="font-size:12px;padding:2px 8px" onclick="rvUseAlt(${k})" title="Swap the second engine’s version into the text below">Use it</button></div>`).join('');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Review — ${esc(RV.base)}</h1>
  <div class="sub" style="margin-top:4px;display:flex;gap:8px;align-items:center">
    <button class="rvnav" onclick="rvGo(-1)" ${RV.i===0?'disabled':''} title="Previous flagged segment (nothing is changed when you flip)">‹</button>
    <button class="rvnav" onclick="rvGo(1)" ${RV.i>=RV.items.length-1?'disabled':''} title="Next flagged segment (nothing is changed when you flip)">›</button>
    <span>${RV.i+1} of ${RV.items.length} · ${esc(it.flags.join(', '))}${it.minor?' · minor':''}</span>
    ${minorLeft?`<button style="font-size:12px;padding:3px 10px" onclick="rvAcceptMinor()" title="Sub-second crosstalk crumbs (“like”, “so”…) — accept them all in one click; substantial items stay">✓ Accept ${minorLeft} minor</button>`:''}</div>
  <audio id="rva" controls src="/api/audio?base=${encodeURIComponent(RV.base)}"></audio>
  ${it.prev?`<div class="muted" style="margin-top:8px">…${esc(it.prev)}</div>`:''}
  <div style="display:flex;gap:8px;align-items:flex-start;margin:8px 0">
    <select id="rvspk" title="Who actually said this?">${spkOptions(RV.speakers,RV.people,it.speaker)}</select>
    <textarea id="rvtext" style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:60px">${esc(it.text)}</textarea>
  </div>
  ${alts}
  ${it.next?`<div class="muted">${esc(it.next)}…</div>`:''}
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:14px">
    <button onclick="rvPlay()">▶ Play clip</button>
    <div style="display:flex;gap:8px">
      <button onclick="rvNext()">Skip</button>
      <button onclick="rvApply('accept')" title="The speaker and text are right — clear the flag">Accept as-is</button>
      <button class="primary" onclick="rvApply('edit')" title="Save the corrected speaker/text back to the transcript files">Save changes</button>
    </div>
  </div>`;
  spkWireNew($('#rvspk'));
  rvPlay();
}
function rvGo(d){
  const j=RV.i+d;
  if(j<0||j>=RV.items.length)return;
  RV.i=j;renderReview();
}
function rvUseAlt(k){
  const a=RV.items[RV.i].alt[k],ta=$('#rvtext');
  // "ours" is normalized tokens — match them loosely against the display text
  const pat=a.ours.trim().split(/\s+/).map(t=>t.replace(/[.*+?^$()|[\]\\{}]/g,'\\$&')).join("[^A-Za-z0-9']+");
  const re=pat?new RegExp(pat,'i'):null;
  if(re&&re.test(ta.value))ta.value=ta.value.replace(re,a.theirs);
  else ta.value=(ta.value+' '+a.theirs).trim();
}
function rvPlay(){
  const it=RV.items[RV.i],a=$('#rva');
  if(!a)return;
  const stopAt=it.end+0.7;
  const go=()=>{a.currentTime=Math.max(0,it.start-0.7);a.play();
    a.ontimeupdate=()=>{if(a.currentTime>=stopAt)a.pause()}};
  a.readyState>=1?go():a.onloadedmetadata=go;
}
async function rvAcceptMinor(){
  const r=await api('/api/review',{base:RV.base,action:'accept_minor'});
  if(!r.ok){alert(r.error||'failed');return}
  RV.items=RV.items.filter((x,idx)=>idx<RV.i||!x.minor);
  if(RV.i>=RV.items.length){dlg.close();return}
  renderReview();
}
async function rvApply(action){
  const it=RV.items[RV.i];
  const body={base:RV.base,index:it.index,start:it.start,action};
  if(action==='edit'){
    const v=$('#rvspk').value;
    if(v==='__new__'){alert('Pick or name the speaker first.');return}
    body.text=$('#rvtext').value;body.speaker=v}
  const r=await api('/api/review',body);
  if(!r.ok){alert(r.error||'Save failed');return}
  if(r.merged){
    // the reassignment folded neighbors into one turn — every later item's
    // index/start in this pre-fetched list may now be stale; refetch
    const d=await api('/api/review?base='+encodeURIComponent(RV.base));
    RV.items=d.items;RV.i=0;
    if(!RV.items.length){dlg.close();return}
    renderReview();return}
  rvNext();
}
function rvNext(){
  RV.i++;
  if(RV.i>=RV.items.length){dlg.close();return}
  renderReview();
}

// Only one thing plays at a time: starting ANY audio pauses all the others.
document.addEventListener('play',e=>{
  document.querySelectorAll('audio').forEach(a=>{if(a!==e.target)a.pause()});
  if(voiceAudio&&!voiceAudio.paused&&e.target!==voiceAudio)stopVoice();
},true);
async function stopRun(){
  const b=$('#stopbtn');b.disabled=true;b.innerHTML='<span class="spin"></span> Stopping…';
  const r=await api('/api/stop',{});   // server verifies the whole group is gone
  b.disabled=false;b.textContent='Stop processing';
  const cq=r.cleared_jobs?` Also cancelled ${r.cleared_jobs} queued run${r.cleared_jobs>1?'s':''}.`:'';
  if(r.survivors&&r.survivors.length){
    $('#stopnote').textContent='Some processes would not die (pids '+r.survivors.join(', ')+') — try again, or reboot if it persists.';
  }else{
    $('#stopnote').textContent=(r.forced?'Stopped (had to force-kill a stuck worker). Memory is released.':'Stopped and verified — nothing left running, memory released.')+cq;
  }
  refresh();
}
async function openRedo(base,audio){
  const ed=await api('/api/edits?base='+encodeURIComponent(base));
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Reprocess “${esc(base)}”</h1>
  <p class="muted" style="margin-top:8px">Re-runs transcription + speaker detection from the stored audio with the current model and speaker library. The existing transcript is replaced.</p>
  ${ed.n?`<p class="muted" style="margin-top:8px;color:var(--warn,#c60)"><b>⚠ This meeting has ${ed.n} manual edit${ed.n>1?'s':''}</b> (corrections, added or removed lines). A redo rebuilds everything from the audio, so they will no longer apply — they’re archived to a “.reviews.superseded.json” file next to the transcript, not deleted.</p>`:''}
  <label class="sub" style="display:block;margin-top:10px"><input type="checkbox" id="redostrict" class="checkbox" style="vertical-align:-3px"> strict mode — never guess an uncertain speaker (for confidential conversations)</label>
  <label class="sub" style="display:block;margin-top:6px"><input type="checkbox" id="redoverify" class="checkbox" style="vertical-align:-3px"> verify — a second engine listens too; disagreements get flagged with both versions</label>
  <label class="sub" style="display:block;margin-top:6px"><input type="checkbox" id="redoonetime" class="checkbox" style="vertical-align:-3px"> one-time speakers — don’t add this meeting’s unnamed voices to the Speakers list (focus groups)</label>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="api('/api/run',{paths:['${escJs(audio)}'],force:true,strict:$('#redostrict').checked,verify:$('#redoverify').checked,onetime:$('#redoonetime').checked}).then(()=>{dlg.close();refresh()})">Reprocess</button>
  </div>`;
  dlg.showModal();
}
// Speaker picker used by the viewer editor and the review dialog: this
// meeting's speakers, then every enrolled person, then "New person…" —
// so a voice the diarizer missed (crosstalk) can still be credited correctly.
function spkOptions(speakers,people,sel){
  const seen=new Set(speakers.map(s=>s.display));
  let h=speakers.map(s=>`<option value="${s.id}" ${s.id===sel?'selected':''}>${esc(s.display)}</option>`).join('');
  const others=(people||[]).filter(p=>!seen.has(p));
  if(others.length)h+=`<optgroup label="Someone else">${others.map(p=>`<option value="name:${esc(p)}">${esc(p)}</option>`).join('')}</optgroup>`;
  return h+`<option value="__new__">＋ New person…</option>`;
}
function spkWireNew(sel){
  // remember the last real selection so cancelling the New-person prompt
  // restores the segment's actual speaker instead of jumping to option 0
  sel.addEventListener('focus',()=>{if(sel.value!=='__new__')sel._prev=sel.value});
  sel.addEventListener('change',()=>{
    if(sel.value!=='__new__'){sel._prev=sel.value;return}
    const nm=(prompt('Who said this? (name as it should appear in the transcript)')||'').trim();
    if(!nm){if(sel._prev!=null)sel.value=sel._prev;else sel.selectedIndex=0;return}
    const o=document.createElement('option');o.value='name:'+nm;o.textContent=nm;
    sel.insertBefore(o,sel.lastElementChild);sel.value='name:'+nm;
  });
}
let _tipTimer=null;
function fmtWhen(iso){
  if(!iso)return '';
  try{return new Date(iso).toLocaleString([],{month:'short',day:'numeric',
    year:'numeric',hour:'numeric',minute:'2-digit'});}catch(e){return iso;}
}
function _tipHtml(m){
  let h=esc(m.summary||'');
  if((m.next_steps||[]).length){
    h+='<div class="tiphead">Committed next steps</div><ul>'
      +m.next_steps.slice(0,5).map(s=>`<li>${esc(s)}</li>`).join('')
      +(m.next_steps.length>5?`<li>+${m.next_steps.length-5} more…</li>`:'')+'</ul>';
  }
  if(m.processed_at)h+=`<div class="tiphead">Processed</div>${esc(fmtWhen(m.processed_at))}`;
  return h;
}
document.addEventListener('mouseover',e=>{
  const row=e.target.closest&&e.target.closest('.hastip');
  const tip=$('#tipbox');
  if(!row||!row.dataset.base){clearTimeout(_tipTimer);tip.style.display='none';return}
  if(tip.dataset.for===row.dataset.base&&tip.style.display==='block')return;
  clearTimeout(_tipTimer);
  _tipTimer=setTimeout(()=>{
    const m=(S&&S.meetings||[]).find(x=>x.base===row.dataset.base);
    if(!m||!m.summary)return;
    tip.innerHTML=_tipHtml(m);tip.dataset.for=m.base;
    tip.style.display='block';
    const r=row.getBoundingClientRect(),tw=tip.offsetWidth,th=tip.offsetHeight;
    let x=Math.min(r.left,window.innerWidth-tw-12);
    let y=r.bottom+8;
    if(y+th>window.innerHeight-8)y=Math.max(8,r.top-th-8);  // flip above
    tip.style.left=Math.max(8,x)+'px';tip.style.top=y+'px';
  },250);
});
document.addEventListener('scroll',()=>{$('#tipbox').style.display='none'},true);
function inlineRename(base,ev){
  ev.stopPropagation();
  $('#tipbox').style.display='none';
  const el=ev.currentTarget;
  el.outerHTML=`<input class="inline-edit" id="ire" value="${esc(base)}" size="${Math.min(60,base.length+4)}">`;
  const inp=$('#ire');inp.focus();inp.select();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    const nm=inp.value.trim();
    if(save&&nm&&nm!==base){
      const r=await api('/api/rename',{base,new:nm});
      if(!r.ok)alert(r.error||'Rename failed');
    }
    inp.remove();refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter')finish(true);else if(e.key==='Escape')finish(false)};
  inp.onblur=()=>finish(true);
}
function inlineDate(base,iso,ev){
  ev.stopPropagation();
  $('#tipbox').style.display='none';
  const el=ev.currentTarget;
  el.outerHTML=`<input type="date" class="inline-edit" id="ide" value="${esc(iso)}">`;
  const inp=$('#ide');inp.focus();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    if(save&&inp.value&&inp.value!==iso){
      const r=await api('/api/set_date',{base,date:inp.value});
      if(!r.ok)alert(r.error||'Date not saved');
    }
    inp.remove();refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter')finish(true);else if(e.key==='Escape')finish(false)};
  inp.onblur=()=>finish(true);
}
const HUES=['#0071e3','#34c759','#ff9f0a','#ff375f','#bf5af2','#64d2ff','#ffd60a','#ac8e68'];
let tvTimer=null,TV=null;
function tvRow(g,i){
  const mm=Math.floor(g.start/60),ss=String(Math.floor(g.start%60)).padStart(2,'0');
  return `<div class="tseg${g.flags.length?' flagged':''}" id="ts${i}" onclick="tvSeek(${g.start})" title="${g.flags.length?'Uncertain: '+esc(g.flags.join(', '))+' — tap to listen':'Tap to listen from here'}">
  <span class="t">${mm}:${ss}</span>
  <span class="w" title="${esc(g.who)}" style="color:color-mix(in srgb, ${TV.color[g.who]||'currentColor'} 65%, var(--ink))">${esc(g.who)}${g.flags.length?' *':''}${g.edited?' ✎':''}</span>
  <span class="x">${esc(g.text)}</span>
  <button class="segbtn" onclick="tvEdit(${i},event)" title="Fix this line (speaker or text)">✎</button></div>`;
}
async function openTranscript(base,target=null){
  // re-render IN PLACE — a close()+showModal() pair races its own queued
  // close event on a fast API: the old view's onclose fires after the new
  // dialog opens, nulling TV and closing it (seen as a blank flash after
  // any merge/split/insert reload)
  if(tvTimer){clearInterval(tvTimer);tvTimer=null}
  TVF=null;  // any re-render rebuilds the rows, so old find marks are gone
  dlg.onclose=null;
  const d=await api('/api/transcript?base='+encodeURIComponent(base));
  if(d.error){alert(d.error);return}
  const color={};d.speakers.forEach((w,i)=>color[w]=HUES[i%HUES.length]);
  TV={base,segs:d.segments,speakers:d.speaker_options,people:d.people||[],color};
  const legend=d.speakers.map(w=>`<span class="chip"><span class="sdot" style="background:${color[w]}"></span>${esc(w)}</span>`).join(' ');
  $('#dlg').classList.add('wide');
  const proc=(S.meetings.find(x=>x.base===base)||{}).processed_at;
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(base)}</h1>
  <div class="sub" style="margin:6px 0 2px">${legend}${d.strict?' <span class="chip warn">strict</span>':''}</div>
  ${proc?`<div class="sub" style="color:var(--muted,#8a8f98);margin-bottom:4px">Processed ${esc(fmtWhen(proc))}</div>`:''}
  <div style="display:flex;gap:8px;align-items:center">
    <audio id="tva" controls src="/api/audio?base=${encodeURIComponent(base)}" style="flex:1"></audio>
    <button onclick="tvAddAt()" title="Add a line the pipeline missed, at the audio’s current position — pause where you heard it, then click">＋ Line at playhead</button>
  </div>
  <div style="display:flex;gap:6px;align-items:center;margin-top:8px">
    <input id="tvfind" type="search" placeholder="Find in transcript… (⌘F)" autocomplete="off" spellcheck="false"
      style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:6px 8px"
      oninput="tvFindInput()" onkeydown="tvFindKey(event)">
    <span id="tvfindn" class="sub" style="min-width:64px;text-align:right"></span>
    <button onclick="tvFindNav(-1)" title="Previous match (Shift+Enter)">‹</button>
    <button onclick="tvFindNav(1)" title="Next match (Enter)">›</button>
  </div>
  <div id="tvlist" style="max-height:46vh;overflow:auto;margin-top:8px">${tvGap(-1)}${TV.segs.map((g,i)=>tvRow(g,i)+tvGap(i)).join('')}</div>
  <div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="tvClose()">Close</button></div>`;
  if(!dlg.open)dlg.showModal();
  dlg.onclose=tvClose;
  dlg.onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='f'){e.preventDefault();const f=$('#tvfind');if(f){f.focus();f.select()}}};
  if(target!=null){
    const i=TV.segs.findIndex(g=>g.index===target);
    if(i>=0)setTimeout(()=>{const el=$('#ts'+i);
      if(el){el.scrollIntoView({block:'center'});el.classList.add('now')}
      tvSeek(TV.segs[i].start)},80);
  }
  const audio=$('#tva');
  tvTimer=setInterval(()=>{
    if(!audio||audio.paused||!TV)return;
    const t=audio.currentTime;
    document.querySelectorAll('.tseg.now').forEach(e=>e.classList.remove('now'));
    const i=TV.segs.findIndex(g=>t>=g.start&&t<g.end);
    if(i>=0){const el=$('#ts'+i);if(el)el.classList.add('now')}
  },500);
}
function tvSeek(t){const a=$('#tva');if(!a)return;
  const go=()=>{a.currentTime=t;a.play()};
  a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true})}
function tvClose(){if(tvTimer){clearInterval(tvTimer);tvTimer=null}TV=null;TVF=null;const a=$('#tva');if(a)a.pause();dlg.onclose=null;dlg.onkeydown=null;dlg.close();$('#dlg').classList.remove('wide');refresh()}
// ---- find within the open transcript (⌘F): occurrence count, next/prev, highlights ----
let TVF=null,tvFindT=null;  // {q, rx, hits:[{i,k}], cur} — hits are occurrences, not rows
function tvFindInput(){clearTimeout(tvFindT);tvFindT=setTimeout(()=>tvFindRun(true),200)}
function tvFindKey(e){
  if(e.key==='Enter'){
    e.preventDefault();clearTimeout(tvFindT);
    const q=$('#tvfind').value.trim();
    if(!TVF||TVF.q!==q)tvFindRun(true);
    else tvFindNav(e.shiftKey?-1:1);
  }else if(e.key==='Escape'){
    // clear the find, NOT the dialog — swallow it before the native <dialog>
    // cancel behavior closes the whole viewer
    e.preventDefault();e.stopPropagation();
    tvFindClear();$('#tvfind').blur();
  }
}
function tvFindRun(reset){
  const f=$('#tvfind'),q=f?(f.value||'').trim():'';
  const prev=TVF;
  tvFindUnmark();TVF=null;
  if(q.length<2){tvFindCount();return}
  const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
  const hits=[];
  TV.segs.forEach((g,i)=>{const m=g.text.match(rx);if(m)for(let k=0;k<m.length;k++)hits.push({i,k})});
  const cur=hits.length?(reset?0:Math.min(prev?Math.max(prev.cur,0):0,hits.length-1)):-1;
  TVF={q,rx,hits,cur};
  tvFindMark();tvFindCount();
  if(reset&&cur>=0)tvFindShow();
}
function tvFindMark(){
  if(!TVF||!TVF.hits.length)return;
  const rows={};TVF.hits.forEach((h,n)=>{(rows[h.i]=rows[h.i]||[]).push(n)});
  for(const i in rows){
    const el=$('#ts'+i),g=TV.segs[i];
    if(!el||el.classList.contains('editing'))continue;   // mid-edit: textarea owns the row
    const x=el.querySelector('.x');if(!x)continue;
    const t=g.text;let out='',last=0,k=0;
    t.replace(TVF.rx,(m,off)=>{                          // escape AROUND matches, so a query
      const n=rows[i][k++];                              // containing & < > still highlights
      out+=esc(t.slice(last,off))+`<mark${n===TVF.cur?' class="cur"':''}>${esc(m)}</mark>`;
      last=off+m.length;return m;
    });
    x.innerHTML=out+esc(t.slice(last));
  }
}
function tvFindUnmark(){
  if(!TVF)return;
  new Set(TVF.hits.map(h=>h.i)).forEach(i=>{
    const el=$('#ts'+i),g=TV.segs[i];
    if(!el||!g||el.classList.contains('editing'))return;
    const x=el.querySelector('.x');if(x)x.innerHTML=esc(g.text);
  });
}
function tvFindNav(d){
  if(!TVF||!TVF.hits.length)return;
  TVF.cur=(TVF.cur+d+TVF.hits.length)%TVF.hits.length;
  tvFindMark();tvFindCount();tvFindShow();
}
function tvFindShow(){
  const h=TVF.hits[TVF.cur];if(!h)return;
  const el=$('#ts'+h.i);if(!el)return;
  (el.querySelector('mark.cur')||el).scrollIntoView({block:'center'});
}
function tvFindCount(){
  const n=$('#tvfindn');if(!n)return;
  n.textContent=TVF?(TVF.hits.length?`${TVF.cur+1} of ${TVF.hits.length}`:'0 of 0'):'';
}
function tvFindClear(){
  clearTimeout(tvFindT);
  tvFindUnmark();TVF=null;
  const f=$('#tvfind');if(f)f.value='';
  tvFindCount();
}
function tvFindRefresh(){  // an edited row was rebuilt bare — recompute and repaint
  const f=$('#tvfind');
  if(TVF&&f&&f.value.trim().length>=2)tvFindRun(false);
}
function tvEdit(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],el=$('#ts'+i);
  el.onclick=null;el.classList.add('editing');
  el.innerHTML=`<div style="flex:1;min-width:0">
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
      <select id="tvspk">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <select id="tvengine" title="Which engine listens again — a different one from the original gives an independent second opinion">
        ${[['parakeet','Parakeet · fast'],['mlxwhisper:large-v3','Whisper v3 · thorough'],['mlxwhisper:turbo','Whisper turbo']].map(([v,l])=>`<option value="${v}" ${v===(S.model==='parakeet'?'mlxwhisper:large-v3':'parakeet')?'selected':''}>${l}</option>`).join('')}
      </select>
      <button onclick="tvRetrans(${i},event)" title="Listen to this span again with the chosen engine and propose corrected text">↻ Re-transcribe</button>
      <span id="tvrx" class="sub"></span></div>
    <textarea id="tvta" style="width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:64px">${esc(g.text)}</textarea>
    <div style="display:flex;gap:6px;margin-top:6px">
      <button onclick="tvDelete(${i},event)" title="Remove this line entirely (echo, noise heard as speech)">Remove line</button>
      <button onclick="tvSplitUI(${i},event)" title="Split this line in two — click inside the text where the second voice starts, then press this">✂ Split line</button>
      <span class="grow"></span>
      <button onclick="tvPlaySpan(${i},event)">▶ Play span</button>
      <button onclick="tvRestore(${i},event)">Cancel</button>
      <button class="primary" onclick="tvSave(${i},event)">Save</button></div></div>`;
  spkWireNew($('#tvspk'));
}
function tvSplitUI(i,ev){
  ev.stopPropagation();
  const ta=$('#tvta'),pos=ta.selectionStart||0,full=ta.value;
  const a=full.slice(0,pos).trim(),b=full.slice(pos).trim();
  if(!a||!b){alert('Click inside the text where the split should happen — some words before the cursor, some after — then press Split again.');return}
  const g=TV.segs[i],el=$('#ts'+i);
  const box='width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:48px';
  el.innerHTML=`<div style="flex:1;min-width:0">
    <div class="sub" style="margin-bottom:6px">Splitting this line in two — set each half’s speaker. If a half matches its neighbor’s speaker, they join into one turn automatically.</div>
    <div style="display:flex;gap:6px;align-items:flex-start;margin-bottom:6px">
      <select id="tvsa">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <textarea id="tvta1" style="${box}">${esc(a)}</textarea></div>
    <div style="display:flex;gap:6px;align-items:flex-start">
      <select id="tvsb">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <textarea id="tvta2" style="${box}">${esc(b)}</textarea></div>
    <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:6px">
      <button onclick="tvRestore(${i},event)">Cancel</button>
      <button class="primary" onclick="tvSplitSave(${i},event)">Split</button></div></div>`;
  spkWireNew($('#tvsa'));spkWireNew($('#tvsb'));
  $('#tvsb').focus();
}
async function tvSplitSave(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],sa=$('#tvsa').value,sb=$('#tvsb').value;
  if(sa==='__new__'||sb==='__new__'){alert('Pick or name both speakers first.');return}
  const r=await api('/api/review',{base:TV.base,action:'split',index:g.index,start:g.start,
    text_a:$('#tvta1').value,text_b:$('#tvta2').value,speaker_a:sa,speaker_b:sb});
  if(!r.ok){alert(r.error||'Split failed');return}
  openTranscript(TV.base,r.index);
}
function tvGap(i){
  return `<div class="tgap" id="tg${i}" tabindex="0" role="button"
    aria-label="Add a line here — a voice the pipeline missed"
    onclick="tvAddLine(${i})"
    onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();tvAddLine(${i})}"
    title="Add a line here — a voice the pipeline missed">＋ add line</div>`;
}
function tvFmt(t){const m=Math.floor(t/60),s=Math.floor(t%60);return m+':'+String(s).padStart(2,'0')}
function tvParseT(v){
  const p=String(v).trim().split(':').map(Number);
  if(p.some(isNaN))return null;
  return p.reverse().reduce((acc,x,k)=>acc+x*Math.pow(60,k),0);
}
function tvAddLine(i,at=null){
  document.querySelectorAll('.tgap.editing').forEach(e=>{const k=+e.id.slice(2);e.outerHTML=tvGap(k)});
  const g=TV.segs[i];  // undefined for i=-1 (the gap before the first line)
  const start=at!=null?at:(g?g.end:0);
  const el=$('#tg'+i);
  el.classList.add('editing');el.onclick=null;
  el.innerHTML=`<div style="text-align:left">
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
      <select id="tvnspk">${spkOptions(TV.speakers,TV.people,null)}</select>
      <label class="sub">at <input id="tvnat" value="${tvFmt(start)}" style="width:56px;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:3px 6px;text-align:center"></label>
      <span class="sub">(tip: pause the audio where you heard it — “＋ Line at playhead” fills this in)</span></div>
    <textarea id="tvnta" placeholder="What they said" style="width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:48px"></textarea>
    <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:6px">
      <button onclick="tvSeek(Math.max(0,tvParseT($('#tvnat').value)-2))">▶ Listen here</button>
      <button onclick="const k=${i};$('#tg'+k).outerHTML=tvGap(k)">Cancel</button>
      <button class="primary" onclick="tvSaveNew()">Add</button></div></div>`;
  spkWireNew($('#tvnspk'));
  $('#tvnta').focus();
}
function tvAddAt(){
  const a=$('#tva'),t=a?a.currentTime:0;
  let i=TV.segs.findIndex(g=>g.start>t)-1;
  if(i<-1)i=TV.segs.length-1;
  i=Math.max(0,i);
  const el=$('#tg'+i);if(el)el.scrollIntoView({block:'center'});
  tvAddLine(i,t);
}
async function tvSaveNew(){
  const spk=$('#tvnspk').value,text=$('#tvnta').value,start=tvParseT($('#tvnat').value);
  if(spk==='__new__'){alert('Pick or name the speaker first.');return}
  if(start==null){alert('Time should look like 12:34.');return}
  if(!text.trim()){alert('Type what they said.');return}
  const r=await api('/api/review',{base:TV.base,action:'insert',start,end:start+3,speaker:spk,text});
  if(!r.ok){alert(r.error||'failed');return}
  openTranscript(TV.base,r.index);
}
async function tvDelete(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i];
  if(!confirm('Remove this line from the transcript? Its audio stays; only the text line goes.'))return;
  const r=await api('/api/review',{base:TV.base,action:'delete',index:g.index,start:g.start});
  if(!r.ok){alert(r.error||'failed');return}
  openTranscript(TV.base);
}
function tvPlaySpan(i,ev){ev.stopPropagation();const g=TV.segs[i],a=$('#tva');
  if(!a)return;a.currentTime=Math.max(0,g.start-0.5);a.play();
  a.ontimeupdate=()=>{if(a.currentTime>=g.end+0.5){a.pause();a.ontimeupdate=null}}}
function tvRestore(i,ev){if(ev)ev.stopPropagation();
  const el=$('#ts'+i);el.outerHTML=tvRow(TV.segs[i],i);tvFindRefresh()}
async function tvRetrans(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i];
  const eng=$('#tvengine').value;
  // estimate from THIS machine's own past re-transcriptions (localStorage
  // median per engine) — no hardcoded numbers that lie on faster/slower Macs
  const hist=JSON.parse(localStorage.getItem('stt_retrans_secs')||'{}');
  const past=(hist[eng]||[]).slice().sort((a,b)=>a-b);
  const estTxt=past.length?` (~${Math.round(past[Math.floor(past.length/2)])}s on this Mac)`:'';
  $('#tvrx').innerHTML='<span class="spin"></span> listening again…'+estTxt;
  const t0=Date.now();
  const r=await api('/api/retranscribe',{base:TV.base,start:g.start,end:g.end,engine:eng});
  hist[eng]=((hist[eng]||[]).concat((Date.now()-t0)/1000)).slice(-5);
  localStorage.setItem('stt_retrans_secs',JSON.stringify(hist));
  if(r.error){$('#tvrx').textContent='failed: '+r.error;return}
  $('#tvta').value=r.text;
  $('#tvrx').textContent='proposed by '+(r.engine||'second engine')+' — edit if needed, then Save';
}
async function tvSave(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],spk=$('#tvspk').value;
  if(spk==='__new__'){alert('Pick or name the speaker first.');return}
  const r=await api('/api/review',{base:TV.base,index:g.index,start:g.start,
    action:'edit',text:$('#tvta').value,speaker:spk});
  if(!r.ok){alert(r.error||'Save failed');return}
  if(r.merged){openTranscript(TV.base,r.index);return}  // rows changed: neighbors folded into one turn
  if(spk.startsWith('name:')){openTranscript(TV.base,g.index);return}  // new person → fresh legend/colors
  const sp=TV.speakers.find(s=>s.id===spk);
  g.text=$('#tvta').value;g.flags=[];g.edited=true;
  if(sp){g.who=sp.display;g.speaker=sp.id}
  tvRestore(i);
}
async function pickFolder(which){
  const prompt=which==='source'?'Choose the folder to watch for new recordings':'Choose where transcripts are stored';
  const r=await api('/api/pick_folder',{which,prompt});
  if(!r.cancelled)refresh();
}

function openSchedule(){
  const sc=S.schedule;const h=sc.hour??2,m=sc.minute??0;
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Nightly run time</h1>
  <p class="muted" style="margin-top:8px">Everything new processes at this time each night. If the Mac is asleep at that moment, the run happens automatically at the next wake. (The folder watch is a separate switch — it picks files up the moment they land.)</p>
  <div class="timegrid">
    <select id="sh">${[...Array(24).keys()].map(i=>`<option value="${i}" ${i===h?'selected':''}>${(i%12)||12} ${i<12?'AM':'PM'}</option>`).join('')}</select>
    <b>:</b>
    <select id="sm">${[0,15,30,45].map(i=>`<option value="${i}" ${i===m?'selected':''}>${String(i).padStart(2,'0')}</option>`).join('')}</select>
  </div>
  <p class="muted">Best between 1–5 AM, plugged in. Overnight runs need AC power.</p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="api('/api/schedule',{hour:+$('#sh').value,minute:+$('#sm').value}).then(()=>{dlg.close();refresh()})">Save</button>
  </div>`;
  dlg.showModal();
}
function mmss(t){const m=Math.floor(t/60),s=String(Math.floor(t%60)).padStart(2,'0');return m+':'+s}
// ---- enrolled-name autocomplete for "Who is this?" (native datalist can't be
// styled, scrolled, or ranked — with a long roster it overflowed the page) ----
let PN={items:[],cur:-1};
function pnameFilter(){
  const q=($('#pname').value||'').trim().toLowerCase();
  const names=S.enrolled.map(e=>e.name).sort((a,b)=>a.localeCompare(b));
  let items=q?names.filter(n=>n.toLowerCase().includes(q)):names;
  if(q){ // closest match first: whole-name prefix, then any word's prefix, then substring
    const rank=n=>{const l=n.toLowerCase();
      return l.startsWith(q)?0:(l.split(/\s+/).some(w=>w.startsWith(q))?1:2)};
    items=items.slice().sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  }
  PN={items,cur:items.length&&q?0:-1};
  pnameRender();
}
function pnameRender(){
  const dd=$('#pnamedd');if(!dd)return;
  const q=($('#pname').value||'').trim();
  const rx=q?new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'):null;
  dd.innerHTML=PN.items.map((n,i)=>
    `<div class="pnitem${i===PN.cur?' cur':''}" onmousedown="event.preventDefault();pnamePick(${i})">${rx?esc(n).replace(rx,m=>'<mark>'+m+'</mark>'):esc(n)}</div>`).join('');
  dd.style.display=PN.items.length?'block':'none';
  const c=dd.querySelector('.pnitem.cur');if(c)c.scrollIntoView({block:'nearest'});
}
function pnamePick(i){const f=$('#pname');if(!f)return;f.value=PN.items[i];pnameClose();f.focus()}
function pnameClose(){const dd=$('#pnamedd');if(dd)dd.style.display='none';PN.cur=-1}
function pnameKey(e){
  const dd=$('#pnamedd'),open=dd&&dd.style.display!=='none';
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    if(!open){pnameFilter();return}
    if(!PN.items.length)return;
    PN.cur=(PN.cur+(e.key==='ArrowDown'?1:-1)+PN.items.length)%PN.items.length;
    pnameRender();
  }else if(e.key==='Enter'){
    e.preventDefault();
    if(open&&PN.cur>=0)pnamePick(PN.cur);
    else $('#pnamesave').click();
  }else if(e.key==='Escape'&&open){
    // close only the dropdown, not the whole dialog
    e.preventDefault();e.stopPropagation();pnameClose();
  }
}
async function openName(uid,display,meeting){
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Who is ${esc(display)}?</h1>
  <p class="muted" style="margin-top:8px">Listen to this voice — the clip is their longest turn in each meeting they were heard in, and “Read” opens the transcript at that exact moment for full context. Typing an <b>existing</b> name merges this voice into that person. Every past and future meeting relabels automatically.</p>
  <div id="nameclips"><span class="spin"></span></div>
  <input type="text" id="pname" placeholder="Person’s name" autocomplete="off" spellcheck="false" style="width:100%;margin-top:12px"
    oninput="pnameFilter()" onfocus="pnameFilter()" onblur="setTimeout(pnameClose,150)" onkeydown="pnameKey(event)">
  <div id="pnamedd" class="inset" style="display:none;max-height:170px;overflow-y:auto;margin-top:6px;padding:4px"></div>
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:16px">
    <button class="danger" onclick="api('/api/forget',{uid:'${escJs(uid)}'}).then(()=>{dlg.close();refresh()})">Not a real speaker</button>
    <div style="display:flex;gap:8px">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" id="pnamesave" onclick="const n=$('#pname').value.trim();if(n)api('/api/name',{uid:'${escJs(uid)}',name:n}).then(()=>{dlg.close();refresh()})">Save name</button>
    </div>
  </div>`;
  dlg.showModal();
  $('#pname').focus();
  const r=await api('/api/voice_clips?speaker='+encodeURIComponent(uid));
  const box=$('#nameclips');
  if(!box)return;  // dialog closed while loading
  const clips=(r.clips||[]);
  box.innerHTML=clips.map(c=>`<div class="row" style="display:block">
    <div class="name" title="${esc(c.meeting)}">${esc(c.meeting)}</div>
    <div class="sub">their longest turn · at ${mmss(c.start)} · ${Math.round(c.dur)}s</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
      <audio controls preload="none" style="height:32px;flex:1;min-width:0" onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
        src="/api/snippet?meeting=${encodeURIComponent(c.meeting)}&speaker=${encodeURIComponent(uid)}&secs=45"></audio>
      <button onclick="openTranscript('${escJs(c.meeting)}',${c.index})" title="Open the transcript at this moment — hear as much as you like, with the conversation around it">Read</button>
    </div>
  </div>`).join('')
    ||`<audio controls src="/api/snippet?speaker=${encodeURIComponent(uid)}&secs=45"></audio>`;
}
function reassignSample(name,index){
  // move a misattributed sample to the right person instead of discarding it
  const others=S.enrolled.map(e=>e.name).filter(x=>x!==name);
  const hint=others.length?`\nExisting people: ${others.slice(0,6).join(', ')}`:'';
  const to=(prompt(`This voice sample is really whose? Type an existing name to add it there, or a new name to start a profile.${hint}`)||'').trim();
  if(!to||to===name)return;
  api('/api/reassign_sample',{name,index,to}).then(r=>{
    if(!r.ok){alert(r.error||'failed');return;}
    dlg.close();refresh();
  });
}
function renameSpeaker(oldName){
  const n=$('#rname').value.trim();
  if(!n||n===oldName)return;
  // renaming onto an existing person silently merges their voiceprints — say so
  const clash=S.enrolled.find(e=>e.name.toLowerCase()===n.toLowerCase()&&e.name!==oldName);
  if(clash&&!confirm(`${n} is already a saved person. Renaming “${oldName}” to “${n}” MERGES their voice samples into one profile. Continue?`))return;
  api('/api/rename_speaker',{name:oldName,new:n}).then(()=>{dlg.close();refresh()});
}
function openSpeakerActions(key,display,meeting){
  const isName=key.startsWith('name:');
  const others=[...S.enrolled.map(e=>({k:'name:'+e.name,d:e.name})),
                ...S.unknowns.map(u=>({k:'uid:'+u.uid,d:u.display}))]
               .filter(o=>o.k!==key&&(isName?o.k.startsWith('name:'):true));
  const enr=isName?S.enrolled.find(x=>x.name===key.slice(5)):null;
  let samplerows='';
  if(enr){
    const n=enr.samples,srcs=enr.sources||[],cap=S.max_samples||5;
    samplerows=`<div class="sub" style="margin-top:8px;font-weight:600">Voice samples (${n} of ${cap})</div>
      <div class="sub" style="margin:2px 0 6px;color:var(--muted,#8a8f98)">A profile keeps up to ${cap} samples. A varied set, from different meetings, rooms, and mics, identifies this person more reliably than several clips from one recording.</div>`+
      Array.from({length:n},(_,i)=>{
        const src=srcs.length===n?srcs[i]:(srcs[srcs.length-n+i]||null);
        return `<div class="row">
          ${src?`<button class="playbtn" data-key="${esc(enr.name)}" data-meeting="${esc(src)}" onclick="playVoice(this)">▶</button>`:'<span style="width:30px" title="Source unknown (enrolled before tracking)"></span>'}
          <div class="grow"><div class="sub">Sample ${i+1} — ${src?esc(src):'source unknown'}</div></div>
          <button title="Reassign — this sample is really someone else's voice; move it to the right person instead of deleting it" onclick="reassignSample('${escJs(enr.name)}',${i})">→</button>
          ${n>1?`<button title="Remove this sample (e.g. a bad recording); the person keeps their other samples" onclick="api('/api/remove_sample',{name:'${escJs(enr.name)}',index:${i}}).then(r=>{if(!r.ok)alert(r.error||'failed');dlg.close();refresh()})">✕</button>`:''}
        </div>`}).join('');
  }
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(display)}</h1>
  ${meeting?`<audio controls src="/api/snippet?meeting=${encodeURIComponent(meeting)}&speaker=${encodeURIComponent(key.split(':')[1])}" style="margin-top:10px"></audio>`:''}
  ${samplerows}
  ${isName?`
  <div class="row"><div class="grow"><div class="name">Rename</div><div class="sub">Fix the name everywhere</div></div>
    <input type="text" id="rname" value="${esc(display)}" style="width:150px">
    <button onclick="renameSpeaker('${escJs(display)}')">Apply</button></div>`:''}
  <div class="row"><div class="grow"><div class="name">Merge into…</div>
    <div class="sub">This voice is really the same person as</div></div>
    <select id="mtarget">${others.map(o=>`<option value="${esc(o.k)}">${esc(o.d)}</option>`).join('')}</select>
    <button ${others.length?'':'disabled'} onclick="api('/api/merge_speakers',{src:'${escJs(key)}',dst:$('#mtarget').value}).then(()=>{dlg.close();refresh()})">Merge</button></div>
  ${isName?'':`<div class="row"><div class="grow"><div class="name">Hide from this list</div>
    <div class="sub">One-time voice (focus group) — keep it matchable but out of the way; restore any time</div></div>
    <button onclick="api('/api/hide_unknown',{uid:'${escJs(key.split(':')[1])}',hide:true}).then(()=>{dlg.close();refresh()})">Hide</button></div>`}
  <div class="row" style="border:0"><div class="grow"><div class="name">Remove</div>
    <div class="sub">${isName?'Un-enroll; their lines revert to Speaker N':'Forget this voice entirely'}</div></div>
    <button class="danger" onclick="if(confirm('Remove ${escJs(display)}?'))api(${isName?`'/api/remove_speaker',{name:'${escJs(display)}'}`:`'/api/forget',{uid:'${escJs(key.split(':')[1])}'}`}).then(()=>{dlg.close();refresh()})">Remove</button></div>
  <div style="display:flex;justify-content:flex-end;margin-top:14px"><button onclick="dlg.close()">Close</button></div>`;
  dlg.showModal();
}
function openRename(base){
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Rename recording</h1>
  <p class="muted" style="margin-top:8px">Suggest a name from what was actually discussed (runs locally — nothing leaves this Mac), or type your own.</p>
  <input type="text" id="newname" value="${esc(base)}" style="width:100%;margin-top:10px">
  <label class="sub" style="display:block;margin-top:10px">Meeting date
    <input type="date" id="mdate" value="${esc((S.meetings.find(x=>x.base===base)||{}).date||'')}"
      style="margin-left:8px;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:3px 6px">
    <span class="muted" style="margin-left:6px">groups the list by month — fix it here if the recording was exported late</span></label>
  <div id="sumnote" class="muted" style="margin-top:8px"></div>
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:16px">
    <button onclick="suggest('${escJs(base)}')" ${S.llm_available?'':'disabled'}>${S.llm_available?'✨ Suggest from content':'(local LLM not installed)'}</button>
    <div style="display:flex;gap:8px">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" onclick="doRename('${escJs(base)}')">Rename</button>
    </div>
  </div>`;
  dlg.showModal();
}
function openSummary(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  const steps=(m.next_steps||[]).length?`<div style="font-weight:600;margin-top:10px">Committed next steps</div><ul style="margin:6px 0 0 18px">${m.next_steps.map(s=>`<li class="muted">${esc(s)}</li>`).join('')}</ul>`:'';
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(base)}</h1>
  <div style="margin-top:10px;max-height:340px;overflow:auto"><div id="sumbody" class="muted">${m.summary?esc(m.summary):('No summary yet — generate one below. '+(S.llm_backend==='local'?'Runs locally; nothing leaves this Mac.':'Uses your cloud assistant.'))}</div>${steps}</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="openAsk('${escJs(base)}')" ${S.llm_available?'':'disabled'}>Ask a question…</button>
    <button onclick="genSummary('${escJs(base)}')" ${S.llm_available?'':'disabled'}>${m.summary?'Regenerate':'✨ Generate summary'}</button>
    <button onclick="dlg.close()">Close</button>
  </div>`;
  dlg.showModal();
}
// ---- Ask: questions about one meeting, answered locally from its transcript ----
let ASK=null;  // {base, hist:[{q,a,err}], busy} — per-session only, never persisted
function openAsk(base){
  if(dlg.open)dlg.close();  // launched from the Summary dialog, for instance
  // same meeting: the thread survives close/reopen (it dies with the page,
  // or when you ask about a different meeting — never written to disk)
  if(!ASK||ASK.base!==base)ASK={base,hist:[],busy:false};
  $('#askmeet').textContent=base;
  $('#askpriv').textContent='Answers come from this transcript only. '
    +(S.llm_backend==='local'?'They are generated on this Mac; nothing leaves the machine.':'They are generated by your cloud assistant (the transcript text is sent to it for this).')
    +' Follow-up questions understand the earlier ones. The thread is never saved.';
  $('#asknote').textContent='';
  $('#askq').disabled=ASK.busy;$('#askbtn').disabled=ASK.busy;
  flyToggle('#askfly',true);
  askRender();
  $('#askq').focus();
}
function askRender(){
  if(!ASK)return;
  $('#askthread').innerHTML=ASK.hist.length?ASK.hist.map(h=>`
    <div class="bub q">${esc(h.q)}</div>
    <div class="bub a${h.err?' warn':''}">${
      h.a?esc(h.a):'<span class="spin"></span> Reading the transcript and thinking… '+(S.llm_backend==='local'?'usually 20-60s (the model loads fresh for each question)':'usually a few seconds')}</div>`).join('')
    :'<div class="sub" style="padding:18px 2px">Ask anything about this meeting: decisions, who said what, commitments…</div>';
  $('#askthread').scrollTop=$('#askthread').scrollHeight;
}
async function askSend(){
  if(!ASK||ASK.busy)return;
  const q=$('#askq').value.trim();
  if(!q)return;
  const t=ASK;  // this thread — the flyout may switch meetings while we wait
  // last few successful exchanges ride along so follow-ups make sense
  const hist=t.hist.filter(h=>h.a&&!h.err).slice(-3).map(h=>({q:h.q,a:h.a}));
  t.hist.push({q,a:''});t.busy=true;
  $('#askq').value='';$('#askq').disabled=true;$('#askbtn').disabled=true;
  askRender();
  const r=await api('/api/ask',{base:t.base,question:q,history:hist});
  const cur=t.hist[t.hist.length-1];
  if(r.answer){cur.a=r.answer}
  else{cur.a='⚠ '+(r.error||'No answer produced.');cur.err=true}
  t.busy=false;
  if(ASK!==t)return;  // a different meeting's thread is open now — drop quietly
  $('#asknote').textContent=r.truncated
    ?'Long meeting: middle portions were sampled, so details from the middle may be missing.':'';
  $('#askq').disabled=false;$('#askbtn').disabled=false;
  askRender();$('#askq').focus();
}
async function genSummary(base){
  $('#sumbody').innerHTML='<span class="spin"></span> Reading the transcript… '+(S.llm_backend==='local'?'(~10-20s)':'(a few seconds)');
  const r=await api('/api/suggest?base='+encodeURIComponent(base));
  $('#sumbody').textContent=r.summary||r.error||'No summary produced.';
  await refresh();
  openSummary(base);  // re-render with next steps from fresh state
}
async function suggest(base){
  $('#sumnote').innerHTML='<span class="spin"></span> Reading the transcript…';
  const r=await api('/api/suggest?base='+encodeURIComponent(base));
  if(r.suggested_name){$('#newname').value=r.suggested_name;$('#sumnote').textContent=r.summary||''}
  else $('#sumnote').textContent=r.error||'Could not suggest a name.';
}
async function doRename(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  const nm=$('#newname').value.trim(),dt=$('#mdate').value;
  let cur=base;
  if(nm&&nm!==base){
    const r=await api('/api/rename',{base,new:nm});
    if(!r.ok){$('#sumnote').textContent=r.error||'Rename failed';return}
    cur=nm;
  }
  if(dt&&dt!==m.date){
    const r=await api('/api/set_date',{base:cur,date:dt});
    if(!r.ok){$('#sumnote').textContent=r.error||'Date not saved';return}
  }
  dlg.close();refresh();
}
async function checkUpdates(){
  $('#updbtn').innerHTML='<span class="spin"></span>';
  const r=await api('/api/check_updates');
  const ups=(r.models||[]).filter(m=>m.update_available);
  $('#updnote').textContent=ups.length?('Updates available: '+ups.map(u=>u.label).join(', ')):'All models are current.';
  $('#updbtn').textContent='Check';
}
function openCloudKeys(){
  const ck=S.cloud_keys||{};
  // fixed columns (label · input · status · clear) so every row lines up, with
  // the status/clear slots RESERVED even when empty — mixed-width rows read
  // as misalignment. Hints sit under their own input.
  const row=(prov,label,hint)=>`<div style="display:flex;gap:10px;align-items:flex-start;margin-top:12px">
    <span class="sub" style="width:118px;flex:none;padding-top:7px">${label}</span>
    <div class="grow" style="min-width:0">
      <div style="display:flex;gap:8px;align-items:center">
        <input type="password" id="ck_${prov}" placeholder="${ck[prov]?'saved — paste to replace':'paste API key'}" style="flex:1;min-width:0;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:6px 8px">
        <span class="sub" style="width:16px;flex:none;text-align:center" title="${ck[prov]?'A key is saved':''}">${ck[prov]?'✓':''}</span>
        <span style="width:54px;flex:none;text-align:right">${ck[prov]?`<button onclick="clearCloudKey('${prov}','${label}')" title="Remove the saved ${label} key from this Mac">Clear</button>`:''}</span>
      </div>
      <div class="sub" style="opacity:.75;margin-top:3px">${hint}</div>
    </div></div>`;
  $('#dlg').classList.add('wide');  // room for full placeholders and hints
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Cloud transcription keys</h1>
  <p class="muted" style="margin-top:8px">Optional: transcribe with a cloud engine instead of the local models. Only the audio is uploaded — speaker identification and voiceprints stay on this Mac. <b>Strict-mode recordings never upload</b>, whatever engine is selected. Keys are stored in stt.env on this machine and never shown again.</p>
  ${row('scribe','ElevenLabs Scribe','elevenlabs.io → Profile → API keys')}
  ${row('openai','OpenAI','platform.openai.com → API keys · also used by the OpenAI assistant below')}
  ${row('voxtral','Mistral Voxtral','console.mistral.ai → API keys')}
  <h1 style="font-size:15px;margin-top:20px">Assistant (summaries &amp; Ask)</h1>
  <p class="muted" style="margin-top:6px">The assistant drafts summaries and answers Ask questions. The local model needs no key. Choosing a cloud assistant in Settings sends transcript text to that provider for these features only; <b>strict-mode recordings always use the local model</b>.</p>
  ${row('anthropic','Anthropic (Claude)','console.anthropic.com → API keys')}
  <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
    <span class="sub" style="width:118px;flex:none">OpenAI (GPT)</span>
    <div class="grow sub">uses the OpenAI key from the transcription section above · ${ck.openai?'✓ key saved':'no key yet'}</div>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="saveCloudKeys()">Save</button>
  </div>`;
  dlg.onclose=()=>{dlg.onclose=null;$('#dlg').classList.remove('wide')};
  dlg.showModal();
}
async function saveCloudKeys(){
  const r=await api('/api/cloud_keys',{scribe:$('#ck_scribe').value,openai:$('#ck_openai').value,voxtral:$('#ck_voxtral').value,anthropic:$('#ck_anthropic').value});
  if(!r.ok){alert(r.error||'Could not save');return}
  dlg.close();refresh();
}
async function clearCloudKey(prov,label){
  if(!confirm(`Remove the saved ${label} key? Cloud transcription with this provider stops working until a new key is pasted.`))return;
  const r=await api('/api/cloud_keys',{clear:[prov]});
  if(!r.ok){alert(r.error||'Could not clear the key');return}
  S.cloud_keys=r.set;   // re-render the dialog from fresh state, keep it open
  openCloudKeys();
}
const THEME_META={auto:["◐","Theme: matching macOS — click for light"],
light:["☀","Theme: light — click for dark"],dark:["☾","Theme: dark — click to match macOS"]};
function themeNow(){const q=new URLSearchParams(location.search).get("theme");
  return q==="light"||q==="dark"?q:(localStorage.getItem("stt_theme")||"auto")}
function applyTheme(t){
  if(t==="light"||t==="dark"){localStorage.setItem("stt_theme",t);document.documentElement.dataset.theme=t}
  else{localStorage.removeItem("stt_theme");delete document.documentElement.dataset.theme}
  const b=$('#themebtn');if(b){b.textContent=THEME_META[t][0];b.title=THEME_META[t][1]}
}
function cycleTheme(){
  const order=["auto","light","dark"];
  applyTheme(order[(order.indexOf(themeNow())+1)%3]);
}
applyTheme(themeNow());
{const ms=localStorage.getItem('stt_msort');if(ms&&$('#msort'))$('#msort').value=ms}
function setModel(){api('/api/model',{model:$('#modelsel').value}).then(r=>{if(!r.ok)alert(r.error||'Could not switch model');refresh()})}
function setLlm(){api('/api/llm_backend',{backend:$('#llmsel').value}).then(r=>{if(!r.ok)alert(r.error||'Could not switch the assistant');refresh()})}
function flyToggle(id,open){
  const el=$(id);
  const want=open===undefined?!el.classList.contains('open'):open;
  if(want)document.querySelectorAll('.fly.open').forEach(f=>{if(f!==el)f.classList.remove('open')});
  el.classList.toggle('open',want);
  $('#flyveil').classList.toggle('open',!!document.querySelector('.fly.open'));
}
function flyCloseAll(){
  document.querySelectorAll('.fly.open').forEach(f=>f.classList.remove('open'));
  $('#flyveil').classList.remove('open');
}
function toggleSettings(open){flyToggle('#setfly',open)}
document.addEventListener('keydown',e=>{
  // Escape closes whichever flyout is up — but never while a dialog is open
  // (the dialog's own Escape handling owns that case)
  if(e.key==='Escape'&&!dlg.open&&document.querySelector('.fly.open')){
    e.preventDefault();flyCloseAll();
  }
});
// ---- processing history flyout: the complete, permanent results list ----
let HIST=null;
async function openHistory(){
  flyToggle('#histfly',true);
  $('#histlist').innerHTML='<div style="padding:12px 0"><span class="spin"></span></div>';
  $('#histcount').textContent='';
  const r=await api('/api/history');
  HIST=r.results||[];
  histRender();
}
function histRender(){
  if(HIST===null)return;
  const q=($('#histq').value||'').trim().toLowerCase();
  const f=$('#histok').value;
  const rows=HIST.filter(r=>(!q||(r.name||'').toLowerCase().includes(q))&&(!f||(f==='ok')===!!r.ok));
  const CAP=400, nOk=rows.filter(r=>r.ok).length;
  $('#histcount').textContent=rows.length?`${rows.length} result${rows.length===1?'':'s'} · ${nOk} processed · ${rows.length-nOk} failed`:'';
  let day='';
  $('#histlist').innerHTML=rows.slice(0,CAP).map(r=>{
    const d=(r.at||'').slice(0,10);
    const hdr=d!==day?`<div class="mgroup">${d?new Date(d+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'long',day:'numeric',year:'numeric'}):'Undated'}</div>`:'';
    day=d;
    return hdr+`<div class="row"><span class="chip ${r.ok?'done':'warn'}">${r.ok?'✓':'failed'}</span>
      <div class="grow" style="min-width:0"><div class="name">${esc((r.name||'').replace(/\.[^.]+$/,''))}</div>
      ${r.summary?`<div class="sub" style="word-break:break-word">${esc(r.summary)}</div>`:''}</div>
      <span class="sub" style="flex:none">${(r.at||'').slice(11,16)}</span></div>`;
  }).join('')+(rows.length>CAP?`<div class="sub" style="padding:10px 0">Showing the first ${CAP} — narrow the filter to see older results.</div>`:'')
  ||'<div class="sub" style="padding:10px 0">No matching results.</div>';
}
function togWatch(){api('/api/automation',{watch:!S.schedule.watch}).then(r=>{if(!r.ok)alert(r.error||'Could not change the folder watch');refresh()})}
function togNightly(){api('/api/automation',{nightly:!S.schedule.nightly}).then(r=>{if(!r.ok)alert(r.error||'Could not change the nightly run');refresh()})}
async function refresh(){try{S=await api('/api/state');render()}catch(e){}}
refresh().then(()=>{
  // deep link: /?open=<meeting> opens that transcript directly
  const o=new URLSearchParams(location.search).get('open');
  if(o&&(S.meetings||[]).some(m=>m.base===o))openTranscript(o);
});
setInterval(refresh,2000);
</script></body></html>
"""

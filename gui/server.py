"""Local control panel for the STT pipeline.

A tiny stdlib HTTP server bound to 127.0.0.1 only, started as a thread inside the
menu-bar app. Serves one polished HTML page + a JSON API. All actions shell out to
the same entrypoints the CLI uses, so the panel can never bypass the pipeline's
locks and safety rails.
"""
import json
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


def _env_file_set(updates: dict):
    envp = config.PROJECT_DIR / "stt.env"
    kv, lines = _env_file_get()
    out = []
    seen = set()
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
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
    src = AGENT
    try:
        d = plistlib.loads(src.read_bytes())
        sci = d.get("StartCalendarInterval", {})
        return {"hour": sci.get("Hour"), "minute": sci.get("Minute", 0) or 0,
                "installed": AGENT.exists()}
    except Exception:
        return {"hour": None, "minute": 0, "installed": AGENT.exists()}


def write_schedule(hh, mm):
    import os
    for p in (AGENT,):
        if p.exists():
            d = plistlib.loads(p.read_bytes())
            d["StartCalendarInterval"] = {"Hour": hh, "Minute": mm}
            p.write_bytes(plistlib.dumps(d))
    if AGENT.exists():
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], capture_output=True)


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
                "audio": str(audio_p) if audio_p else None}
    except Exception:
        meta = None
    _meet_cache[key] = meta
    if len(_meet_cache) > 400:  # drop stale mtime generations
        for k in list(_meet_cache)[:200]:
            _meet_cache.pop(k, None)
    return meta


def gather_state():
    st = status.read()
    m = manifest.load()
    src_dir, dst_dir = config.source_dir(), config.meetings_dir()
    queue = []
    try:
        for p in sorted(src_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in config.AUDIO_EXTS and not p.name.startswith("."):
                done = manifest.is_processed(m, p.name, p.stat().st_mtime)
                dur = None if done else _est_duration(p)
                est_min = (round(sum(status.stage_estimates(dur).values()) / 60)
                           if dur else None)
                queue.append({"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1),
                              "video": p.suffix.lower() in config.VIDEO_EXTS,
                              "processed": done, "est_min": est_min})
    except Exception:
        pass
    meetings = []
    for j in sorted((config.meeting_file(b, ".json", dst_dir)
                     for b in config.meeting_bases(dst_dir)),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _meeting_meta(j, dst_dir)
        if meta:
            meetings.append(meta)
    reg = unknowns.load()
    unknown_list = []
    for uid, meta in sorted(reg["speakers"].items()):
        unknown_list.append({"uid": uid, "display": unknowns.display(uid),
                             "meetings": meta.get("meetings", [])})
    enrolled = [{"name": n, "samples": meta.get("n_samples", 1),
                 "sources": [s for s in meta.get("sources", []) if s and s != "?"]}
                for n, meta in identify.load_registry().items()]
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
            "schedule": read_schedule(), "model": current_model(),
            "asr_choices": ASR_CHOICES, "battery": battery,
            "paths": {"source": str(src_dir), "dest": str(dst_dir)},
            "punctuate": _env_file_get()[0].get("STT_PUNCTUATE", "1") == "1",
            "rates": rates.summary(),
            "relabel_pending": (config.PROJECT_DIR / "relabel_pending.flag").exists(),
            "llm_available": (config.PROJECT_DIR / ".venv-llm/bin/python").exists()}


def _osascript(script: str, timeout=120):
    r = subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def pick_folder(prompt: str):
    code, out = _osascript(
        f'POSIX path of (choose folder with prompt "{prompt}")')
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


def _snippet_for(meeting: str, speaker_key: str):
    """Extract a short audio snippet of `speaker_key` (display, id, name, or uid).
    Locating the right stretch — including the voiceprint fallback for people
    named before their transcripts were relabeled — lives in stt.review."""
    hit = review.find_voice_clip(speaker_key, meeting or None)
    if hit is None:
        return None
    base, start, dur = hit
    audio_f = _meeting_audio(base)
    if audio_f is None:
        return None
    out = config.PROJECT_DIR / "work" / "snippet.wav"
    out.parent.mkdir(exist_ok=True)
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
                if meeting and not self._require_base(meeting):
                    return
                f = _snippet_for(meeting, q["speaker"])
                if f is None:
                    self._json({"error": "no snippet"}, 404)
                    return
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
                    a = int(mo.group(1) or 0)
                    z = int(mo.group(2)) if mo.group(2) else len(data) - 1
                    z = min(z, len(data) - 1)
                    chunk = data[a:z + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{z}/{len(data)}")
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
                          "parallel": int(b.get("parallel", 1)), "label": label})
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
                # src/dst are "uid:U003" or "name:Toya"
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
                self._json({"ok": ok} if ok else
                           {"ok": False,
                            "error": "Can't remove the only sample — remove the person instead."})
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
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--sub:#86868b;--line:rgba(0,0,0,.08);
--accent:#0071e3;--accent-h:#0077ed;--ok:#34c759;--warn:#ff9f0a;--bad:#ff3b30;
--chip:rgba(120,120,128,.10);--inset:rgba(120,120,128,.06);--hairline:rgba(60,60,67,.12)}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f5f5f7;
--sub:#98989d;--line:rgba(255,255,255,.10);--chip:rgba(120,120,128,.22);
--inset:rgba(120,120,128,.10);--hairline:rgba(84,84,88,.5)}}
*{box-sizing:border-box;margin:0}
html{scroll-behavior:smooth}
body{font:15px/1.47 -apple-system,system-ui,"SF Pro Text",sans-serif;background:var(--bg);
color:var(--ink);padding:0 28px 40px;max-width:880px;margin:0 auto;
-webkit-font-smoothing:antialiased}
h1{font:700 26px/1.2 -apple-system,system-ui,"SF Pro Display",sans-serif;letter-spacing:-.022em}
h2{font-size:15px;font-weight:600;letter-spacing:-.01em;margin:0 0 8px;display:flex;
align-items:center;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:18px 20px;margin:16px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;
border-bottom:.5px solid var(--hairline)}
.row:last-child{border-bottom:0}
.inset{background:var(--inset);border-radius:12px;padding:2px 12px;max-height:44vh;
overflow-y:auto;overscroll-behavior:contain}
.grow{flex:1;min-width:0}.name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--sub);font-size:13px}
.chip{background:var(--chip);border-radius:20px;padding:2.5px 10px;font-size:12px;
font-weight:500;color:var(--sub);white-space:nowrap}
.chip.live{background:color-mix(in srgb,var(--accent) 13%,transparent);color:var(--accent)}
.chip.done{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.chip.warn{background:color-mix(in srgb,var(--warn) 16%,transparent);color:#c93400}
@media(prefers-color-scheme:dark){.chip.warn{color:var(--warn)}}
button{font:inherit;font-size:14px;border:0;border-radius:8px;padding:6px 13px;
cursor:pointer;background:var(--chip);color:var(--ink);transition:background .15s,transform .02s}
button:hover{background:color-mix(in srgb,var(--chip) 60%,rgba(120,120,128,.24))}
button:active{transform:scale(.97)}
button.primary{background:var(--accent);color:#fff;font-weight:600}
button.primary:hover{background:var(--accent-h)}
button.danger{background:color-mix(in srgb,var(--bad) 11%,transparent);color:var(--bad)}
button:disabled{opacity:.4;cursor:default;transform:none}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid
color-mix(in srgb,var(--accent) 40%,transparent);outline-offset:1px}
.link{background:none;padding:2px 4px;color:var(--accent);font-size:13px;font-weight:500}
.link:hover{background:none;text-decoration:underline}
input[type=text],select{font:inherit;background:var(--card);color:var(--ink);
border:1px solid var(--hairline);border-radius:8px;padding:6px 10px;
box-shadow:0 .5px 1.5px rgba(0,0,0,.06)}
.top{display:flex;align-items:center;gap:13px;padding:20px 4px 12px;position:sticky;
top:0;z-index:5;background:color-mix(in srgb,var(--bg) 78%,transparent);
backdrop-filter:blur(22px) saturate(180%);-webkit-backdrop-filter:blur(22px) saturate(180%)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--sub);flex:none}
.dot.run{background:var(--accent);animation:pulse 1.2s infinite}
.dot.paused{background:var(--warn)}
@keyframes pulse{50%{opacity:.35}}
.bar{height:4px;border-radius:2px;background:var(--chip);overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;border-radius:2px;background:var(--accent);transition:width .6s}
.stagechips{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.stagechips .s{font-size:11px;font-weight:500;padding:1.5px 9px;border-radius:10px;
background:var(--chip);color:var(--sub)}
.stagechips .s.on{background:var(--accent);color:#fff}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.checkbox{width:16px;height:16px;accent-color:var(--accent);flex:none}
dialog{margin:auto;border:0;border-radius:18px;padding:24px;background:var(--card);color:var(--ink);
box-shadow:0 24px 70px rgba(0,0,0,.35),0 0 0 .5px var(--line);max-width:440px;width:92%}
dialog::backdrop{background:rgba(0,0,0,.32);backdrop-filter:blur(6px)}
dialog h1{font-size:18px}
.timegrid{display:flex;gap:8px;align-items:center;margin:14px 0}
.seg{display:flex;background:var(--chip);border-radius:9px;padding:2px}
.seg button{background:transparent;padding:5px 12px;border-radius:7px}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.15)}
audio{width:100%;height:34px;margin-top:8px;border-radius:8px}
.muted{color:var(--sub);font-size:13px;line-height:1.5}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);
border-top-color:var(--accent);border-radius:50%;animation:rot .7s linear infinite;vertical-align:-2px}
@keyframes rot{to{transform:rotate(360deg)}}
.playbtn{width:30px;height:30px;border-radius:50%;padding:0;font-size:11px;
display:flex;align-items:center;justify-content:center;flex:none}
.playbtn:hover{background:var(--accent);color:#fff}
dialog.wide{max-width:680px}
.tseg{display:flex;gap:10px;padding:7px 8px;border-radius:8px;cursor:pointer;align-items:baseline}
.tseg:hover{background:var(--chip)}
.tseg.now{background:color-mix(in srgb,var(--accent) 10%,transparent)}
.tseg.flagged{background:color-mix(in srgb,var(--warn) 9%,transparent)}
.tseg .t{color:var(--sub);font-size:12px;font-variant-numeric:tabular-nums;flex:none;width:44px}
.tseg .w{font-weight:600;flex:none;width:118px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tseg .x{flex:1;min-width:0}
.sdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.toggle{min-width:52px}
.mgroup{font-size:12px;font-weight:600;color:var(--sub);text-transform:uppercase;
letter-spacing:.05em;padding:12px 0 2px}
mark{background:color-mix(in srgb,var(--warn) 30%,transparent);color:inherit;border-radius:3px}
.tseg .segbtn{opacity:0;flex:none;padding:1px 8px;font-size:12px;border-radius:6px}
.tgap{height:8px;margin:0 8px;border-radius:6px;text-align:center;line-height:8px;
  font-size:11px;color:transparent;cursor:pointer;transition:all .12s}
.tgap:hover,.tgap:focus-visible{height:20px;line-height:20px;color:var(--accent);
  background:color-mix(in srgb,var(--accent) 8%,transparent)}
.tgap.editing{height:auto;line-height:normal;color:var(--ink);cursor:default;
  background:var(--chip);padding:10px}
.tseg:hover .segbtn,.tseg:focus-within .segbtn{opacity:1}
.tseg.editing{cursor:default;background:var(--chip)}
</style></head><body>
<div class="top">
  <h1>STT Workflow</h1>
  <span id="statusdot" class="dot"></span><span id="statustext" class="sub"></span>
  <span class="grow"></span>
  <span id="mem" class="chip" style="display:none" title="Memory held by transcription processes right now"></span>
  <span id="battery" class="chip"></span>
</div>

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
    <label class="sub" style="white-space:nowrap" title="For sensitive recordings (hearings): never guess an uncertain speaker — flag for review instead"><input type="checkbox" id="strict" class="checkbox" style="vertical-align:-3px"> strict</label>
    <label class="sub" style="white-space:nowrap" title="A second engine transcribes too; the spots where the engines disagree get flagged for review with both versions. Adds a few minutes per hour of audio."><input type="checkbox" id="verify" class="checkbox" style="vertical-align:-3px"> verify</label>
  </div>
  <div class="muted" id="parnote" style="margin-top:6px">“Two at a time” uses ~10 CPU cores and gives ≈1.7× throughput. “Strict” never guesses an uncertain speaker — it flags for review (use for hearings). “Verify” has a second engine listen too and flags the disagreements.</div>
  <div id="recentwrap" style="display:none"><h2 style="margin-top:16px">Recent results
    <span class="chip" title="A rolling history — each new result pushes the oldest out; the 8 latest show here">last 20 kept</span></h2><div id="recent"></div></div>
</div>

<div class="grid2">
<div class="card">
  <h2>Speakers</h2>
  <div id="enrolled"></div>
  <div id="unknowns"></div>
  <div id="relnote" class="muted" style="display:none;margin-top:8px"></div>
</div>
<div class="card">
  <h2>Settings</h2>
  <div class="row"><div class="grow"><div class="name">Daily run</div>
    <div class="sub" id="schedtext"></div></div>
    <button onclick="openSchedule()">Change…</button></div>
  <div class="row"><div class="grow"><div class="name">Transcription model</div>
    <div class="sub" id="modelnote"></div></div>
    <select id="modelsel" onchange="setModel()"></select></div>
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
</div>
</div>

<div class="card">
  <h2>Transcripts <span class="grow"></span>
    <input type="text" id="mfilter" placeholder="Search words or titles…"
           oninput="render();scheduleSearch()" style="width:min(340px,45vw);font-size:13px"></h2>
  <div id="searchhits"></div>
  <div id="meetings" class="inset"></div>
</div>

<dialog id="dlg"></dialog>
<script>
const $=q=>document.querySelector(q);
function fmtEta(sec){if(sec==null)return'';if(sec<90)return'1 min';
  if(sec<3600)return Math.round(sec/60)+' min';
  return Math.floor(sec/3600)+'h '+String(Math.round(sec%3600/60)).padStart(2,'0')+'m'}
let S=null, selected=new Set();
async function api(p,body){const r=await fetch(p,body?{method:'POST',body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return (s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// For values embedded as a JS string literal INSIDE an onclick="..." attribute (e.g. onclick="f('${escJs(x)}')").
// esc() alone breaks there: an apostrophe closes the JS string early, and HTML-entity-encoding it
// (&#39;) doesn't help — the browser HTML-decodes the attribute before compiling it as JS, so the
// entity turns back into a literal quote first. Backslash-escaping survives that decode step.
function escJs(s){return esc(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")}

const STAGES=['downloading','converting','transcribing','diarizing','verifying','writing'];
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',diarizing:'Speakers',verifying:'Verifying',writing:'Writing'};

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
    <div class="stagechips">${STAGES.filter(st=>st!=='verifying'||a.stage==='verifying').map((st,i)=>`<span class="s ${i<=idx?'on':''}">${STAGE_NICE[st]}</span>`).join('')}</div>
    ${pct!=null?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}
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
    const est=(!f.processed&&f.est_min)?` · ~${f.est_min} min to process`:'';
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
  $('#recent').innerHTML=rec.slice(0,8).map(r=>`<div class="row">
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
  $('#enrolled').innerHTML=s.enrolled.map(e=>`<div class="row">
    <button class="playbtn" data-key="${esc(e.name)}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(e.name)}</div>
    <div class="sub" title="${esc((e.sources||[]).join(', '))}">${e.samples} voice sample${e.samples>1?'s':''}${e.sources&&e.sources.length?' · from '+esc(e.sources[e.sources.length-1])+(e.sources.length>1?' +'+(e.sources.length-1):''):''}</div></div>
    <button onclick="openSpeakerActions('name:${escJs(e.name)}','${escJs(e.name)}','')">⋯</button></div>`).join('')||'<div class="sub">No one enrolled yet.</div>';
  $('#unknowns').innerHTML=s.unknowns.map(u=>`<div class="row">
    <button class="playbtn" data-key="${u.uid}" data-meeting="${esc(u.meetings[0]||'')}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(u.display)}</div>
    <div class="sub">heard in ${u.meetings.length} meeting${u.meetings.length>1?'s':''}</div></div>
    <button class="primary" onclick="openName('${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">Who is this?</button>
    <button onclick="openSpeakerActions('uid:${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">⋯</button></div>`).join('')
    ||'<div class="sub" style="padding-top:8px">No unidentified voices right now.</div>';
  $('#relnote').style.display=s.relabel_pending?'block':'none';
  $('#relnote').textContent='Applying names to all transcripts… (moments)';
  // settings
  const sc=s.schedule;
  $('#schedtext').textContent=sc.hour==null?'not set':new Date(2000,0,1,sc.hour,sc.minute).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})+' · runs at next wake if the Mac is asleep';
  const sel=$('#modelsel');
  sel.innerHTML=s.asr_choices.map(c=>`<option value="${c.id}" ${c.id===s.model?'selected':''}>${c.label}</option>`).join('');
  $('#modelnote').textContent=(s.asr_choices.find(c=>c.id===s.model)||{}).note||'';
  $('#srcpath').textContent=s.paths.source.replace(/^\/Users\/[^/]+/,'~');
  $('#dstpath').textContent=s.paths.dest.replace(/^\/Users\/[^/]+/,'~');
  // meetings: filter by title/speaker, newest meeting-date first, grouped by month
  const mq=($('#mfilter').value||'').toLowerCase();
  const shown=s.meetings.filter(m=>!mq||m.base.toLowerCase().includes(mq)
    ||m.speakers.join(' ').toLowerCase().includes(mq))
    .slice().sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  let lastMon='';
  $('#meetings').innerHTML=shown.map(m=>{
    const mon=m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{month:'long',year:'numeric'}):'Undated';
    const hdr=mon!==lastMon?`<div class="mgroup">${mon}</div>`:'';lastMon=mon;
    const day=m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}):'';
    return hdr+`<div class="row"><div class="grow">
    <div class="name">${esc(m.base)}</div>
    <div class="sub">${day?day+' · ':''}${m.minutes} min · ${m.speakers.map(esc).join(', ')}${m.strict?' · strict':''}
      ${m.flagged?` <span class="chip warn" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Step through each uncertain segment with its audio — accept or fix it">⚠ ${m.flagged} to review</span>`:(m.flagged_minor?` <span class="chip" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Only sub-second crosstalk crumbs — bulk-accept or skim them">${m.flagged_minor} minor</span>`:'')}</div>
    ${m.summary?`<div class="sub" style="margin-top:3px;font-style:italic">${esc(m.summary.length>150?m.summary.slice(0,150)+'…':m.summary)}</div>`:''}</div>
    <button class="primary" onclick="openTranscript('${escJs(m.base)}')">Read</button>
    <button onclick="openSummary('${escJs(m.base)}')">Summary</button>
    <button onclick="openMeetingMenu('${escJs(m.base)}')" title="Export, rename, reprocess…">⋯</button>
  </div>`}).join('')||`<div class="sub">${mq?'No transcript titles match “'+esc(mq)+'”.':'No transcripts yet — process something above.'}</div>`;
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
function runOpts(){return {parallel:$('#par2').checked?2:1,strict:$('#strict').checked,verify:$('#verify').checked}}
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
  dlg.onclose=()=>{const a=$('#rva');if(a)a.pause();dlg.onclose=null;$('#dlg').classList.remove('wide');refresh()};
  renderReview();
  dlg.showModal();
}
function renderReview(){
  const it=RV.items[RV.i];
  const minorLeft=RV.items.slice(RV.i).filter(x=>x.minor).length;
  const alts=(it.alt||[]).map((a,k)=>`<div class="sub" style="margin-top:4px">Second engine heard “<b>${esc(a.theirs||'(nothing)')}</b>” where this says “${esc(a.ours||'(nothing)')}” <button style="font-size:12px;padding:2px 8px" onclick="rvUseAlt(${k})" title="Swap the second engine’s version into the text below">Use it</button></div>`).join('');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Review — ${esc(RV.base)}</h1>
  <div class="sub" style="margin-top:4px;display:flex;gap:8px;align-items:center">
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
  <label class="sub" style="display:block;margin-top:10px"><input type="checkbox" id="redostrict" class="checkbox" style="vertical-align:-3px"> strict mode — never guess an uncertain speaker (for hearings)</label>
  <label class="sub" style="display:block;margin-top:6px"><input type="checkbox" id="redoverify" class="checkbox" style="vertical-align:-3px"> verify — a second engine listens too; disagreements get flagged with both versions</label>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="api('/api/run',{paths:['${escJs(audio)}'],force:true,strict:$('#redostrict').checked,verify:$('#redoverify').checked}).then(()=>{dlg.close();refresh()})">Reprocess</button>
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
  sel.addEventListener('change',()=>{
    if(sel.value!=='__new__')return;
    const nm=(prompt('Who said this? (name as it should appear in the transcript)')||'').trim();
    if(!nm){sel.selectedIndex=0;return}
    const o=document.createElement('option');o.value='name:'+nm;o.textContent=nm;
    sel.insertBefore(o,sel.lastElementChild);sel.value='name:'+nm;
  });
}
const HUES=['#0071e3','#34c759','#ff9f0a','#ff375f','#bf5af2','#64d2ff','#ffd60a','#ac8e68'];
let tvTimer=null,TV=null;
function tvRow(g,i){
  const mm=Math.floor(g.start/60),ss=String(Math.floor(g.start%60)).padStart(2,'0');
  return `<div class="tseg${g.flags.length?' flagged':''}" id="ts${i}" onclick="tvSeek(${g.start})" title="${g.flags.length?'Uncertain: '+g.flags.join(', ')+' — tap to listen':'Tap to listen from here'}">
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
  dlg.onclose=null;
  const d=await api('/api/transcript?base='+encodeURIComponent(base));
  if(d.error){alert(d.error);return}
  const color={};d.speakers.forEach((w,i)=>color[w]=HUES[i%HUES.length]);
  TV={base,segs:d.segments,speakers:d.speaker_options,people:d.people||[],color};
  const legend=d.speakers.map(w=>`<span class="chip"><span class="sdot" style="background:${color[w]}"></span>${esc(w)}</span>`).join(' ');
  $('#dlg').classList.add('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(base)}</h1>
  <div class="sub" style="margin:6px 0 2px">${legend}${d.strict?' <span class="chip warn">strict</span>':''}</div>
  <div style="display:flex;gap:8px;align-items:center">
    <audio id="tva" controls src="/api/audio?base=${encodeURIComponent(base)}" style="flex:1"></audio>
    <button onclick="tvAddAt()" title="Add a line the pipeline missed, at the audio’s current position — pause where you heard it, then click">＋ Line at playhead</button>
  </div>
  <div id="tvlist" style="max-height:46vh;overflow:auto;margin-top:8px">${tvGap(-1)}${TV.segs.map((g,i)=>tvRow(g,i)+tvGap(i)).join('')}</div>
  <div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="tvClose()">Close</button></div>`;
  if(!dlg.open)dlg.showModal();
  dlg.onclose=tvClose;
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
function tvClose(){if(tvTimer){clearInterval(tvTimer);tvTimer=null}TV=null;const a=$('#tva');if(a)a.pause();dlg.onclose=null;dlg.close();$('#dlg').classList.remove('wide');refresh()}
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
  const el=$('#ts'+i);el.outerHTML=tvRow(TV.segs[i],i)}
async function tvRetrans(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i];
  const eng=$('#tvengine').value;
  $('#tvrx').innerHTML='<span class="spin"></span> listening again… ('+(eng==='parakeet'?'~15s':'~30–60s')+')';
  const r=await api('/api/retranscribe',{base:TV.base,start:g.start,end:g.end,engine:eng});
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
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Daily run time</h1>
  <p class="muted" style="margin-top:8px">The pipeline checks for new recordings every night. If the Mac is asleep at that moment, the run happens automatically at the next wake — and new files are also picked up within a minute whenever they land while the Mac is awake.</p>
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
function openName(uid,display,meeting){
  const opts=S.enrolled.map(e=>`<option value="${esc(e.name)}">`).join('');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Who is ${esc(display)}?</h1>
  <p class="muted" style="margin-top:8px">Listen to a clip of this voice from “${esc(meeting)}”, then name them. Typing an <b>existing</b> name merges this voice into that person. Every past and future meeting relabels automatically.</p>
  <audio controls src="/api/snippet?meeting=${encodeURIComponent(meeting)}&speaker=${encodeURIComponent(uid)}"></audio>
  <input type="text" id="pname" list="knownnames" placeholder="Person’s name" style="width:100%;margin-top:12px">
  <datalist id="knownnames">${opts}</datalist>
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:16px">
    <button class="danger" onclick="api('/api/forget',{uid:'${escJs(uid)}'}).then(()=>{dlg.close();refresh()})">Not a real speaker</button>
    <div style="display:flex;gap:8px">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" onclick="const n=$('#pname').value.trim();if(n)api('/api/name',{uid:'${escJs(uid)}',name:n}).then(()=>{dlg.close();refresh()})">Save name</button>
    </div>
  </div>`;
  dlg.showModal();
}
function openSpeakerActions(key,display,meeting){
  const isName=key.startsWith('name:');
  const others=[...S.enrolled.map(e=>({k:'name:'+e.name,d:e.name})),
                ...S.unknowns.map(u=>({k:'uid:'+u.uid,d:u.display}))]
               .filter(o=>o.k!==key&&(isName?o.k.startsWith('name:'):true));
  const enr=isName?S.enrolled.find(x=>x.name===key.slice(5)):null;
  let samplerows='';
  if(enr){
    const n=enr.samples,srcs=enr.sources||[];
    samplerows='<div class="sub" style="margin-top:8px;font-weight:600">Voice samples</div>'+
      Array.from({length:n},(_,i)=>{
        const src=srcs.length===n?srcs[i]:(srcs[srcs.length-n+i]||null);
        return `<div class="row">
          ${src?`<button class="playbtn" data-key="${esc(enr.name)}" data-meeting="${esc(src)}" onclick="playVoice(this)">▶</button>`:'<span style="width:30px" title="Source unknown (enrolled before tracking)"></span>'}
          <div class="grow"><div class="sub">Sample ${i+1} — ${src?esc(src):'source unknown'}</div></div>
          ${n>1?`<button title="Remove this sample (e.g. it came from a bad recording)" onclick="api('/api/remove_sample',{name:'${escJs(enr.name)}',index:${i}}).then(r=>{if(!r.ok)alert(r.error||'failed');dlg.close();refresh()})">✕</button>`:''}
        </div>`}).join('');
  }
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(display)}</h1>
  ${meeting?`<audio controls src="/api/snippet?meeting=${encodeURIComponent(meeting)}&speaker=${encodeURIComponent(key.split(':')[1])}" style="margin-top:10px"></audio>`:''}
  ${samplerows}
  ${isName?`
  <div class="row"><div class="grow"><div class="name">Rename</div><div class="sub">Fix the name everywhere</div></div>
    <input type="text" id="rname" value="${esc(display)}" style="width:150px">
    <button onclick="const n=$('#rname').value.trim();if(n&&n!=='${escJs(display)}')api('/api/rename_speaker',{name:'${escJs(display)}',new:n}).then(()=>{dlg.close();refresh()})">Apply</button></div>`:''}
  <div class="row"><div class="grow"><div class="name">Merge into…</div>
    <div class="sub">This voice is really the same person as</div></div>
    <select id="mtarget">${others.map(o=>`<option value="${esc(o.k)}">${esc(o.d)}</option>`).join('')}</select>
    <button ${others.length?'':'disabled'} onclick="api('/api/merge_speakers',{src:'${escJs(key)}',dst:$('#mtarget').value}).then(()=>{dlg.close();refresh()})">Merge</button></div>
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
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(base)}</h1>
  <div id="sumbody" class="muted" style="margin-top:10px;max-height:300px;overflow:auto">${m.summary?esc(m.summary):'No summary yet — generate one below. Runs locally; nothing leaves this Mac.'}</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="genSummary('${escJs(base)}')" ${S.llm_available?'':'disabled'}>${m.summary?'Regenerate':'✨ Generate summary'}</button>
    <button onclick="dlg.close()">Close</button>
  </div>`;
  dlg.showModal();
}
async function genSummary(base){
  $('#sumbody').innerHTML='<span class="spin"></span> Reading the transcript… (~15–30s)';
  const r=await api('/api/suggest?base='+encodeURIComponent(base));
  $('#sumbody').textContent=r.summary||r.error||'No summary produced.';
  refresh();
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
function setModel(){api('/api/model',{model:$('#modelsel').value}).then(refresh)}
async function refresh(){try{S=await api('/api/state');render()}catch(e){}}
refresh();setInterval(refresh,2000);
</script></body></html>
"""

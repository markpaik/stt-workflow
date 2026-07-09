"""Drive the on-device meeting recorder (native/stt-recorder) and hand its
output to the pipeline.

The Swift helper captures mic + system audio into a growable stereo PCM CAF
in a local staging folder. This module starts/stops it, transcodes the CAF to
a named stereo m4a, and drops that into the watched recordings folder for the
batch to pick up. Recording state lives in status.json so the menu bar (which
imports this) survives a restart and can recover an orphaned capture.

Nothing here loads a model or touches the network; recordings are sensitive and
never leave the Mac.
"""
import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import audio, config, status

BINARY = config.PROJECT_DIR / "native" / "stt-recorder"
MAX_SECONDS = 4 * 3600          # forgot-to-stop backstop (matches the helper default)
MIN_FREE_BYTES = 2 * 1024**3    # refuse to start under ~2 GB free
LOG = config.PROJECT_DIR / "logs" / "recorder.log"


def available() -> bool:
    return BINARY.exists() and os.access(BINARY, os.X_OK)


def staging_dir() -> Path:
    d = config.recordings_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _free_bytes(path: Path) -> int:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return MIN_FREE_BYTES  # can't tell -> don't block


def _stamp(now=None):
    now = now or datetime.now()
    return now.strftime("%m%d%Y"), now.strftime("%H%M")


def start() -> dict:
    """Begin a recording. Returns {ok, error?}. Refuses a second concurrent
    recording and a near-full disk."""
    if not available():
        return {"ok": False, "error": "recorder not built — run ./setup.sh build-recorder"}
    rec = status.recording()
    if rec and _pid_alive(rec.get("pid")):
        return {"ok": False, "error": "already recording"}
    staging = staging_dir()
    if _free_bytes(staging) < MIN_FREE_BYTES:
        return {"ok": False, "error": "less than 2 GB free — free up space before recording"}
    # dot-prefixed so the batch watcher never ingests the in-progress capture
    caf = staging / f".rec-{uuid.uuid4().hex[:8]}.caf"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "a")
    # caffeinate -i keeps the Mac awake for the call; start_new_session makes
    # caffeinate the group leader so one signal to the group reaches the helper
    proc = subprocess.Popen(
        ["/usr/bin/caffeinate", "-i", str(BINARY), str(caf), "--max-seconds", str(MAX_SECONDS)],
        stdout=log, stderr=log, start_new_session=True, cwd=str(config.PROJECT_DIR))
    status.set_recording({
        "pid": proc.pid, "caf": str(caf),
        "started_at": status._now(),
        "started_monotonic": time.monotonic(),
    })
    return {"ok": True, "pid": proc.pid, "caf": str(caf)}


def halt() -> Path | None:
    """End capture NOW (before naming, so we don't record the naming pause) and
    return the CAF path. Leaves the recording state set for finalize()."""
    rec = status.recording()
    if not rec:
        return None
    pid, caf = rec.get("pid"), Path(rec.get("caf", ""))
    if _pid_alive(pid):
        try:  # SIGINT the whole group -> helper finalizes the CAF, caffeinate exits
            os.killpg(os.getpgid(int(pid)), signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        for _ in range(80):  # up to ~8s for a clean finalize
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
    return caf


def stop(name=None) -> dict:
    """Stop the active recording, transcode, and name it. Returns
    {ok, path?, error?}. (halt + finalize; callers that want to prompt for a
    name mid-way call halt() first, then finalize().)"""
    caf = halt()
    if caf is None:
        return {"ok": False, "error": "not recording"}
    return finalize(caf, name)


def finalize(caf: Path, name=None) -> dict:
    """Transcode a finished CAF to a named stereo m4a in the watched folder,
    atomically (only a complete file ever becomes visible), then drop the CAF."""
    caf = Path(caf)
    if not caf.exists() or caf.stat().st_size < 8192:  # header-only = zero audio
        status.clear_recording()
        caf.unlink(missing_ok=True)
        return {"ok": False, "error": "nothing was captured (permission denied?)"}
    staging = staging_dir()
    final = final_name(name)
    # keep stereo (L=mic, R=system): Phase 2 exploits the split; Phase 1's
    # to_wav16k downmixes it to mono anyway. Same -f ipod + .part + os.replace
    # idiom as audio.extract_audio, so the watcher never sees a partial file.
    part = staging / f".{final}.m4a.part"
    dst = staging / f"{final}.m4a"
    r = subprocess.run([audio.FFMPEG, "-y", "-i", str(caf), "-ac", "2",
                        "-c:a", "aac", "-b:a", "160k", "-f", "ipod", str(part)],
                       capture_output=True)
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        return {"ok": False, "error": "could not transcode the recording",
                "detail": r.stderr[-400:].decode(errors="replace")}
    os.replace(part, dst)
    caf.unlink(missing_ok=True)
    status.clear_recording()
    return {"ok": True, "path": str(dst), "name": final}


def final_name(raw=None, now=None) -> str:
    """A safe, unique meeting name. Empty/None -> 'Recording MMDDYYYY HHMM'.
    A user name with no 8-digit run gets an MMDDYYYY suffix so dates.py can
    parse the meeting date and month-grouping works. Uniquified against files
    already in staging and in the meetings store."""
    import re

    from . import dates
    mmdd, hhmm = _stamp(now)
    raw = (raw or "").strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw).strip().lstrip(".")
    raw = re.sub(r"\s+", " ", raw)[:120].strip()
    if not raw:
        base = f"Recording {mmdd} {hhmm}"
    elif dates.meeting_date(raw) is None:
        base = f"{raw} {mmdd}"
    else:
        base = raw
    return _uniquify(base)


def _uniquify(base: str) -> str:
    staging = config.recordings_dir()

    def taken(n):
        return (staging / f"{n}.m4a").exists() or config.meeting_dir(n).exists()

    if not taken(base):
        return base
    i = 2
    while taken(f"{base} ({i})"):
        i += 1
    return f"{base} ({i})"


def recover_orphans() -> list:
    """Clean up after a crash/forced quit: a recorded CAF whose helper is gone.
    Called at menu-bar startup. Returns the names recovered."""
    recovered = []
    rec = status.recording()
    if rec and not _pid_alive(rec.get("pid")):
        caf = Path(rec.get("caf", ""))
        r = finalize(caf, None)  # default 'Recording ...' name; clears the state
        if r.get("ok"):
            recovered.append(r["name"])
        else:
            status.clear_recording()
    # stray CAFs with no live owner (e.g. status.json lost)
    staging = config.recordings_dir()
    if staging.exists():
        active_caf = (status.recording() or {}).get("caf")
        for caf in staging.glob(".rec-*.caf"):
            if str(caf) == active_caf:
                continue
            r = finalize(caf, None)
            if r.get("ok"):
                recovered.append(r["name"])
    return recovered

"""Drive the on-device meeting recorder (native/STT Recorder.app) and hand its
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

# inside a real .app bundle so TCC/LaunchServices can address it by bundle id
APP = config.PROJECT_DIR / "native" / "STT Recorder.app"
BINARY = APP / "Contents" / "MacOS" / "stt-recorder"
SWIFT_SRC = config.PROJECT_DIR / "native" / "recorder.swift"
MAX_SECONDS = 4 * 3600          # forgot-to-stop backstop (matches the helper default)
MIN_FREE_BYTES = 2 * 1024**3    # refuse to start under ~2 GB free
LOG = config.PROJECT_DIR / "logs" / "recorder.log"


def available() -> bool:
    return BINARY.exists() and os.access(BINARY, os.X_OK)


def stale() -> bool:
    """The Swift source is newer than the installed binary — the binary may
    predate features whose SIGNALS it does not handle (an old build treats the
    pause SIGUSR1 as a kill: default disposition terminates it mid-meeting).
    start() and pause() refuse instead, pointing at the rebuild."""
    try:
        return SWIFT_SRC.stat().st_mtime > BINARY.stat().st_mtime
    except OSError:
        return False


def staging_dir() -> Path:
    d = config.recordings_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_alive(pid) -> bool:
    """Basic liveness — used only to poll for exit AFTER we have already
    identified and SIGINT'd our recorder. os.kill(pid, 0): no exception = alive."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _proc_cmdline(pid) -> str:
    try:
        return subprocess.run(["/bin/ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _recorder_running(pid) -> bool:
    """True only when pid is live AND actually our recorder. A bare os.kill(pid,
    0) reads a RECYCLED pid as a live recording — which would refuse every new
    start, make halt() SIGINT an unrelated process group, and hide a genuine
    orphan from recovery. The stored pid is the caffeinate wrapper whose command
    line carries the stt-recorder binary path (see start())."""
    if not _pid_alive(pid):
        return False
    return "stt-recorder" in _proc_cmdline(pid)


def live_recording():
    """The capture ACTUALLY running right now, or None. The single source of
    truth for every surface (menu bar title, panel banner) — the raw status entry
    is NOT enough on its own: it deliberately outlives the capture until
    finalize() completes (the naming dialog blocks in between), and a recycled
    pid can fake liveness. Both UIs used to answer this question their own way,
    which is how a stopped recording kept showing as live in the panel."""
    rec = status.recording()
    return rec if rec and _recorder_running(rec.get("pid")) else None


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
    recording, a near-full disk, and a binary older than its source."""
    if not available():
        return {"ok": False, "error": "recorder not built — run ./setup.sh build-recorder"}
    if stale():
        return {"ok": False, "error": "recorder needs a rebuild — run ./setup.sh build-recorder"}
    rec = status.recording()
    if rec and _recorder_running(rec.get("pid")):
        return {"ok": False, "error": "already recording"}
    staging = staging_dir()
    if _free_bytes(staging) < MIN_FREE_BYTES:
        return {"ok": False, "error": "less than 2 GB free — free up space before recording"}
    # dot-prefixed so the batch watcher never ingests the in-progress capture
    caf = staging / f".rec-{uuid.uuid4().hex[:8]}.caf"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    # Launch through LaunchServices (open -n), NOT as our own child process.
    # TCC charges a child's permission requests to its RESPONSIBLE process —
    # spawned from the menu bar that was python3.12, which has no usage strings,
    # so macOS auto-DENIED the microphone without ever showing a prompt (the
    # empty-capture mystery: no error, no prompt, zero frames). An app launched
    # by LaunchServices is its own responsible process, so the prompts belong to
    # "STT Recorder" (which has the usage strings) and grants stick to it.
    # stderr goes nowhere under open(1), so the app appends to --log itself.
    r = subprocess.run(
        ["/usr/bin/open", "-n", "-a", str(APP), "--args",
         str(caf), "--max-seconds", str(MAX_SECONDS), "--log", str(LOG)],
        capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"ok": False, "error": f"could not launch the recorder "
                                      f"({r.stderr.strip() or r.returncode})"}
    # open(1) does not hand back the pid — find the instance recording OUR caf
    pid = None
    for _ in range(50):  # up to ~5s for LaunchServices to spawn it
        out = subprocess.run(["/usr/bin/pgrep", "-f", caf.name],
                             capture_output=True, text=True).stdout.split()
        if out:
            pid = int(out[0])
            break
        time.sleep(0.1)
    if pid is None:
        return {"ok": False, "error": "the recorder did not launch — see logs/recorder.log"}
    # keep the Mac awake for exactly the recorder's lifetime
    with open(LOG, "a") as log:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(pid)],
                         stdout=log, stderr=log, start_new_session=True)
    status.clear_recorder_note()  # a new capture supersedes the last outcome
    status.set_recording({
        "pid": pid, "caf": str(caf),
        "started_at": status._now(),
        "started_monotonic": time.monotonic(),
        "paused": False, "paused_total": 0.0,
    })
    return {"ok": True, "pid": pid, "caf": str(caf)}


def pause() -> dict:
    """Stop writing frames without ending the capture. The audio device stays
    up, so the paused span is simply absent from the recording."""
    rec = status.recording()
    if not rec or not _recorder_running(rec.get("pid")):
        return {"ok": False, "error": "not recording"}
    if stale():
        # an old build has no SIGUSR1 handler — the default disposition would
        # TERMINATE it and silently end the capture mid-meeting
        return {"ok": False, "error": "recorder was updated — pause needs a rebuild "
                                      "(./setup.sh build-recorder); Stop still works"}
    if rec.get("paused"):
        return {"ok": True, "paused": True}
    try:
        os.kill(int(rec["pid"]), signal.SIGUSR1)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"could not pause ({e})"}
    status.set_recording({**rec, "paused": True, "paused_at": time.monotonic()})
    return {"ok": True, "paused": True}


def resume() -> dict:
    rec = status.recording()
    if not rec or not _recorder_running(rec.get("pid")):
        return {"ok": False, "error": "not recording"}
    if not rec.get("paused"):
        return {"ok": True, "paused": False}
    try:
        os.kill(int(rec["pid"]), signal.SIGUSR2)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"could not resume ({e})"}
    # bank the time spent paused so the readout tracks RECORDED audio, not
    # wall-clock since Start (monotonic: a clock change can't inflate it)
    banked = float(rec.get("paused_total") or 0.0)
    at = rec.get("paused_at")
    if at is not None:
        banked += max(0.0, time.monotonic() - float(at))
    new = {**rec, "paused": False, "paused_total": banked}
    new.pop("paused_at", None)
    status.set_recording(new)
    return {"ok": True, "paused": False}


STALL_AFTER_SECS = 8


def capture_stalled(rec) -> bool:
    """True when a live recording SHOULD have audio by now but its CAF is still
    header-only. That is the signature of a TCC denial: macOS keeps the device
    running and simply delivers no frames — nothing errors, nothing prompts.
    The common trigger is a REBUILD of the recorder: the ad-hoc signature is
    pinned to the exact build (cdhash), so rebuilding orphans the old grant.
    This lets the menu bar say so ~10 seconds into the meeting, instead of the
    user discovering an empty capture at stop. A paused recording does not
    grow and does not count as stalled."""
    if not rec or rec.get("paused"):
        return False
    if elapsed_seconds(rec) < STALL_AFTER_SECS:
        return False
    try:
        return Path(rec.get("caf", "")).stat().st_size < 8192
    except OSError:
        return False


def elapsed_seconds(rec) -> int:
    """Seconds of audio actually CAPTURED so far — wall-clock since Start minus
    every paused span. One definition, shared by the menu bar and the panel, so
    the two readouts can't disagree."""
    if not rec:
        return 0
    started = rec.get("started_monotonic")
    if started is None:
        return 0
    secs = time.monotonic() - float(started) - float(rec.get("paused_total") or 0.0)
    if rec.get("paused") and rec.get("paused_at") is not None:
        secs -= max(0.0, time.monotonic() - float(rec["paused_at"]))
    return max(0, int(secs))


def halt() -> Path | None:
    """End capture NOW (before naming, so we don't record the naming pause) and
    return the CAF path. Leaves the recording state set for finalize()."""
    rec = status.recording()
    if not rec:
        return None
    pid, caf = rec.get("pid"), Path(rec.get("caf", ""))
    if _recorder_running(pid):  # identity-checked: never SIGINT a recycled pid's group
        try:  # SIGINT the whole group -> helper finalizes the CAF, caffeinate exits
            os.killpg(os.getpgid(int(pid)), signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        for _ in range(80):  # up to ~8s for a clean finalize
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
    # Capture is OVER the moment the helper exits. Drop the live pid now so no
    # surface keeps showing "recording" while the caller prompts for a name — the
    # menu bar's naming dialog is MODAL and blocks until answered, and finalize()
    # (which clears the state) only runs after it. An unanswered dialog used to
    # leave the panel showing a recording that had already stopped. The entry
    # itself stays, so recover_orphans still knows the CAF if we die here.
    status.set_recording({**rec, "pid": None, "stopped_at": status._now()})
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

    def _clear_if_ours():
        # drop the recording state ONLY when it still points at THIS capture.
        # recover_orphans finalizes stray CAFs from earlier crashes; the menu bar
        # can restart while a detached recorder keeps running, so an unconditional
        # clear here would wipe a DIFFERENT, live recording's state.
        rec = status.recording()
        if not rec or rec.get("caf") == str(caf):
            status.clear_recording()

    if not caf.exists() or caf.stat().st_size < 8192:  # header-only = zero audio
        _clear_if_ours()
        caf.unlink(missing_ok=True)
        status.set_recorder_note(False, "The recording captured NO audio — grant "
                                 "Microphone and 'System Audio Recording Only' "
                                 "(the panel has a Fix permissions button).")
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
        status.set_recorder_note(False, "The recording could not be transcoded — "
                                 "see logs/recorder.log.")
        return {"ok": False, "error": "could not transcode the recording",
                "detail": r.stderr[-400:].decode(errors="replace")}
    # declare the me/them channel layout for channel-aware diarization BEFORE
    # the audio becomes visible, so the batch always sees the sidecar with it.
    # Only when the mic speaker is configured (and expected to be enrolled);
    # otherwise the recording just processes as mono.
    mic = config.mic_speaker()
    if mic:
        (staging / f"{final}.opts.json").write_text(json.dumps(
            {"channel_layout": "mic_left_system_right", "mic_speaker": mic}))
    # Drop the CAF now that the transcode is safely captured in `part`, BEFORE
    # publishing the m4a. A crash between the os.replace and the unlink used to
    # leave the CAF behind for recover_orphans to re-finalize into a DUPLICATE
    # meeting under a second name. With the CAF gone first, the worst a crash in
    # the (microsecond) window before os.replace can do is discard the hidden,
    # watcher-skipped .part and lose this one capture — never a silent double.
    caf.unlink(missing_ok=True)
    os.replace(part, dst)
    _clear_if_ours()
    status.set_recorder_note(True, f"Saved \u201c{final}\u201d — processing; it "
                             "will wait in the panel for a name.")
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
        # archived names count too: registries reference meetings by name, so a
        # new recording reusing an archived meeting's name would make a later
        # restore ambiguous (whose voice clips are whose?)
        return ((staging / f"{n}.m4a").exists() or config.meeting_dir(n).exists()
                or (config.archive_dir() / n).exists())

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
    if rec and not _recorder_running(rec.get("pid")):  # dead OR a recycled pid
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

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


def _proc_cmdline(pid):
    """The process's command line, or None when the CHECK ITSELF failed.

    A failed ps (binary missing, fork failure, timeout) is not evidence of
    anything. Returning "" for it made a failed check read exactly like a
    genuine mismatch, and recover_orphans acts on a mismatch by finalizing and
    DELETING the CAF -- of a recorder that is still running and still writing to
    it. None keeps "we could not tell" distinguishable from "not ours"."""
    try:
        return subprocess.run(["/bin/ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _recorder_identity(pid):
    """True (this pid IS our recorder), False (it is not, or it is gone), or
    None (the identity check failed and nothing is known). Every caller that
    can DESTROY data must act only on an explicit False."""
    if not _pid_alive(pid):
        return False
    cmd = _proc_cmdline(pid)
    if cmd is None:
        return None
    return "stt-recorder" in cmd


def _recorder_running(pid) -> bool:
    """True only when pid is live AND actually our recorder. A bare os.kill(pid,
    0) reads a RECYCLED pid as a live recording — which would refuse every new
    start, make halt() SIGINT an unrelated process group, and hide a genuine
    orphan from recovery. The stored pid is the caffeinate wrapper whose command
    line carries the stt-recorder binary path (see start()).

    An UNKNOWN identity counts as running: the pid is alive, and every caller
    reads False as "no capture" -- which would let a second recorder start on
    top of a live one."""
    return _recorder_identity(pid) is not False


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


def _silence_wav() -> Path:
    """A 2s all-zero WAV, generated once. On this macOS build an aggregate
    device that contains a system-audio tap does not start IO until some
    process is WRITING audio to the output ("waiting for writers") — start a
    capture on a silent Mac and the whole device, mic included, never cycles:
    no error, no frames. Playing this inaudible file right after launch opens
    that gate deterministically (verified: the tap starts within ~10ms of a
    writer appearing, and keeps running after the writer exits)."""
    wav = staging_dir().parent / "silence.wav"
    if not wav.exists():
        import wave
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x00" * 96000)
    return wav


def _kick_writer_gate():
    """Best-effort: play the silent file so the tap's writer gate opens."""
    try:
        subprocess.Popen(["/usr/bin/afplay", str(_silence_wav())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass  # no afplay is no reason to refuse the recording


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
    _kick_writer_gate()  # see _silence_wav: a quiet Mac never starts the tap
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
STALL_QUIET_SECS = 60   # a growing CAF that stops growing for this long

# caf path -> (last size seen, monotonic time it was last seen to GROW). A
# fixed size floor can only ever fire before the CAF first crosses it, so a
# mid-recording stall -- the device swap the docstring names -- was invisible
# for the rest of the session. Growth needs memory between calls.
_growth = {}


def capture_stalled(rec) -> bool:
    """True when a live recording is not capturing audio. Two known causes,
    both silent: the tap's writer gate (macOS starts a tap-containing device
    only once some app plays audio; start() kicks it with a silent file, but a
    mid-recording device swap can re-arm it), and a TCC denial -- classically
    after a REBUILD, since the ad-hoc signature is pinned to the exact build
    (cdhash) and rebuilding orphans the old grant.

    Two readings, so a swap MID-recording is caught too: the CAF is still
    header-only past STALL_AFTER_SECS, or it grew once and has not grown for
    STALL_QUIET_SECS. This lets the menu bar say so ~10 seconds into the
    meeting, instead of the user discovering an empty capture at stop. A
    paused recording does not grow and does not count as stalled."""
    if not rec:
        return False
    caf = str(rec.get("caf", ""))
    try:
        size = Path(caf).stat().st_size
    except OSError:
        return False
    now = time.monotonic()
    prev = _growth.get(caf)
    if prev is None or size > prev[0] or rec.get("paused"):
        # a pause is not a stall: keep the clock fresh so resuming does not
        # report the paused span as silence
        _growth[caf] = (size, now)
        prev = _growth[caf]
    if rec.get("paused"):
        return False
    if elapsed_seconds(rec) < STALL_AFTER_SECS:
        return False
    if size < 8192:
        return True                      # header-only: the capture never began
    return (now - prev[1]) >= STALL_QUIET_SECS


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
    _growth.pop(str(caf), None)   # this capture is over; its growth history is too

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
        status.set_recorder_note(False, "The recording captured NO audio. Usual "
                                 "cause: nothing was playing sound, and macOS "
                                 "holds tap captures until some app plays audio "
                                 "(the recorder now plays a silent kick at start). "
                                 "Otherwise: grant Microphone and 'System Audio "
                                 "Recording Only' (the panel has a Fix "
                                 "permissions button).")
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
    # _recorder_running is False only on an EXPLICIT mismatch -- dead, or a
    # recycled pid. An UNKNOWN identity (the ps call itself failed) reads as
    # running and never reaches this branch: finalize() deletes the CAF, and
    # the real recorder would keep writing to a file that no longer exists.
    if rec and not _recorder_running(rec.get("pid")):
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
    sweep_orphan_parts()
    return recovered


PART_STALE_SECS = 600   # no write for this long = no transcode is behind it


def sweep_orphan_parts() -> int:
    """Delete hidden .m4a.part files nothing is writing any more.

    finalize() drops the CAF before it publishes the transcode, so a crash in
    between leaves a COMPLETE .part on disk with its CAF already gone.
    recover_orphans globs the CAF pattern only, and nothing else in the app
    looks at these, so they were a permanent, silent disk leak. A file written
    within PART_STALE_SECS is left alone: a long transcode may still be
    filling it. Returns how many were removed."""
    staging = config.recordings_dir()
    if not staging.exists():
        return 0
    now = time.time()
    swept = 0
    for part in staging.glob(".*.m4a.part"):
        try:
            if now - part.stat().st_mtime < PART_STALE_SECS:
                continue
            part.unlink()
            swept += 1
        except OSError:
            continue
    return swept

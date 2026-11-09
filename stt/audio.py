"""Audio normalization + probing via ffmpeg/ffprobe."""
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config


def _bin(name: str) -> str:
    """Resolve a Homebrew binary by absolute path (launchd runs with a minimal PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if Path(cand).exists():
            return cand
    return name  # last resort; will raise a clear FileNotFoundError if truly absent


FFMPEG = _bin("ffmpeg")
FFPROBE = _bin("ffprobe")


class UnreadableAudio(RuntimeError):
    """ffmpeg could not read the source: corrupt, truncated, not audio at
    all. The message is one short line with ffmpeg's reason and the file's
    name, never the command line or an absolute path."""


_FF_PREFIX = re.compile(r"^\[[^\]]* @ 0x[0-9a-fA-F]+\]\s*")


def _ffmpeg_reason(stderr, src: Path) -> str:
    """The line of ffmpeg's error output that says why, with its
    "[in#0 @ 0x...]" prefix and any path taken off. The first specific line
    wins ("moov atom not found" over the generic "Invalid data found")."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    src = Path(src)
    out = []
    for ln in (stderr or "").splitlines():
        ln = _FF_PREFIX.sub("", ln.replace("\ufffd", "")).strip()
        if not ln or ln.startswith(("Error opening input file ",
                                    "Error opening output file ")):
            continue
        for lead in ("Error opening input: ", "Error opening input files: ",
                     "Error opening output files: "):
            if ln.startswith(lead):
                ln = ln[len(lead):]
        ln = ln.replace(str(src), src.name)
        if ln and ln not in out:
            out.append(ln)
    return out[0][:300] if out else "unknown error"


def _run_ffmpeg(cmd: list, src: Path):
    """Run one ffmpeg command. A failure raises UnreadableAudio with
    ffmpeg's reason, never CalledProcessError and its command line."""
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        why = _ffmpeg_reason(r.stderr, src)
        if "does not contain any stream" in why:
            # -vn left nothing to write: the source has no audio stream
            # (a screen recording with video only)
            raise UnreadableAudio(f"no audio track in {Path(src).name}")
        raise UnreadableAudio(f"ffmpeg could not decode {Path(src).name}: {why}")
    return r


def duration_sec(src: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def to_wav16k(src: Path, dst: Path) -> Path:
    """Decode any ffmpeg-readable file (audio OR video container) to 16 kHz mono
    signed-16 PCM WAV — for video, this IS the audio-extraction step.

    One normalized artifact is fed to BOTH the ASR and diarization stages so
    their decoders can never disagree on the audio.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn",
         "-ar", str(config.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
         str(dst)], src)
    return dst


def probe_channels(src: Path) -> int:
    """Channel count of the first audio stream, or 0 if unknown. Cheap guard
    before channel-aware processing (a mono file has nothing to split)."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of", "default=nk=1:nw=1", str(src)],
            capture_output=True, text=True)
        return int(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return 0


def to_wav16k_channel(src: Path, dst: Path, channel: int) -> Path:
    """Decode ONE channel (0 = left, 1 = right) of a stereo source to 16 kHz
    mono PCM — the per-channel inputs for channel-aware diarization. The `pan`
    filter picks a single channel without averaging, so no cross-channel bleed
    is introduced by the extraction itself."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn",
         "-af", f"pan=mono|c0=c{int(channel)}",
         "-ar", str(config.SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)], src)
    return dst


def extract_audio(src: Path, dst: Path) -> Path:
    """Extract the audio track from a video into an .m4a (stream-copied when AAC,
    re-encoded otherwise) — the archival copy stored next to transcripts."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # write to a sibling .part then os.replace: an interrupted ffmpeg (Stop
    # button kills the process group mid-write) must never leave a truncated
    # dst that run_batch's existence guard would treat as a finished extract.
    tmp = dst.with_suffix(dst.suffix + ".part")
    # -f ipod (the m4a muxer) is REQUIRED with the .part name: ffmpeg infers
    # the container from the output extension, and ".part" isn't one — without
    # it both commands fail ("unable to find a suitable output format") and
    # every video import dies after transcription.
    r = subprocess.run([FFMPEG, "-y", "-i", str(src), "-vn", "-c:a", "copy",
                        "-f", "ipod", str(tmp)], capture_output=True)
    if r.returncode != 0:  # audio codec not m4a-compatible; re-encode
        _run_ffmpeg([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-vn", "-c:a", "aac", "-b:a", "160k", "-f", "ipod", str(tmp)], src)
    os.replace(tmp, dst)
    return dst

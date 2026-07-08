"""Audio normalization + probing via ffmpeg/ffprobe."""
import os
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
    subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-vn",
         "-ar", str(config.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
         str(dst)],
        check=True, capture_output=True,
    )
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
        subprocess.run([FFMPEG, "-y", "-i", str(src), "-vn", "-c:a", "aac",
                        "-b:a", "160k", "-f", "ipod", str(tmp)],
                       check=True, capture_output=True)
    os.replace(tmp, dst)
    return dst

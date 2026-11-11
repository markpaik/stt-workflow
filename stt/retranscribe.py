"""Re-transcribe one segment of a meeting with a chosen engine: the second
listen on a transcript line.

Run as a one-shot subprocess (`python -m stt.retranscribe <base> <start> <end>
[engine]`) so the long-lived GUI never loads a multi-GB ASR model into its own
memory. Engines (offered()): the local engines only, so no audio leaves the
Mac. Today that is Parakeet. A different engine from the one that wrote the
transcript gives an independent second opinion, which is what you want when
the first engine misheard.
"""
import json
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from . import config
from .audio import FFMPEG

# the local engines, as the panel and the child name them, the default
# first. A new local engine is one row here and one branch in _asr_module().
LOCAL = ("parakeet",)


def offered() -> list:
    """The engine ids a second listen may use, the default first. Only
    local engines: a second listen never sends audio off the Mac."""
    return list(LOCAL)


def engine_problem(engine: str):
    """Why `engine` may not listen again, or None."""
    if engine in offered():
        return None
    return f"unknown engine {engine!r} (choose from {', '.join(offered())})"


def _asr_module(engine: str):
    """The ASR module of a listed engine. Only the listed engines: any
    "mlxwhisper:<text>" once set <text> as the Whisper model, and the child
    loaded (or fetched) that repo or folder."""
    why = engine_problem(engine)
    if why:
        raise ValueError(why)
    if engine == "parakeet":
        from . import asr_parakeet
        return asr_parakeet
    raise ValueError(f"no module for the local engine {engine!r}")


def meeting_audio(base: str):
    return config.meeting_audio(base)


def span_problem(start: float, end: float):
    """Why `start`..`end` is no stretch of a recording, or None. An end
    before the start made a 0.5 s clip at the start, and a negative span
    clamped to the first seconds: each came back as a proposal for the line
    with a success status."""
    if not (math.isfinite(start) and math.isfinite(end)):
        return "The start and the end must be numbers."
    if start < 0:
        return "The start must be 0 or later."
    if end < start:
        return "The end comes before the start."
    return None


def _frames(clip: Path) -> int:
    try:
        with wave.open(str(clip)) as w:
            return w.getnframes()
    except (wave.Error, EOFError, OSError):
        return 0


def retranscribe(base: str, start: float, end: float,
                 engine: str = "parakeet") -> dict:
    why = span_problem(start, end) or engine_problem(engine)
    if why:
        return {"error": why}
    try:
        asr = _asr_module(engine)
    except ValueError as e:
        return {"error": str(e)}
    src = meeting_audio(base)
    if src is None:
        return {"error": f"no stored audio for {base}"}
    pad = 0.25
    with tempfile.TemporaryDirectory() as td:
        clip = Path(td) / "clip.wav"
        subprocess.run(
            [FFMPEG, "-y", "-ss", str(max(0.0, start - pad)),
             "-t", str(max(0.5, end - start + 2 * pad)), "-i", str(src),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(clip)],
            check=True, capture_output=True)
        if not _frames(clip):
            # past the end ffmpeg writes an empty wav, and the model crashed
            # on it with a Metal allocation error
            return {"error": "That span starts after the recording ends."}
        out = asr.transcribe(clip)
    return {"text": out["text"].strip(), "engine": out["engine"]}


if __name__ == "__main__":
    print(json.dumps(retranscribe(
        sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
        sys.argv[4] if len(sys.argv) > 4 else "parakeet")))

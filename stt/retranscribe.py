"""Re-transcribe one segment of a meeting with a chosen engine.

Run as a one-shot subprocess (`python -m stt.retranscribe <base> <start> <end>
[engine]`) so the long-lived GUI never loads a multi-GB ASR model into its own
memory. Engines: "parakeet", "mlxwhisper:large-v3", "mlxwhisper:turbo". Using a
DIFFERENT engine from the one that produced the transcript gives an independent
second opinion — exactly what you want when the first engine hallucinated.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config
from .audio import FFMPEG

ENGINES = ("parakeet", "mlxwhisper:large-v3", "mlxwhisper:turbo")


def _asr_module(engine: str):
    if engine == "parakeet":
        from . import asr_parakeet
        return asr_parakeet
    if engine.startswith("mlxwhisper"):
        variant = engine.split(":", 1)[1] if ":" in engine else "large-v3"
        os.environ["STT_WHISPER_MLX_MODEL"] = variant
        from . import asr_mlxwhisper
        return asr_mlxwhisper
    raise ValueError(f"unknown engine {engine!r} (choose from {ENGINES})")


def meeting_audio(base: str):
    return config.meeting_audio(base)


def retranscribe(base: str, start: float, end: float,
                 engine: str = "parakeet") -> dict:
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
        out = asr.transcribe(clip)
    return {"text": out["text"].strip(), "engine": out["engine"]}


if __name__ == "__main__":
    print(json.dumps(retranscribe(
        sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
        sys.argv[4] if len(sys.argv) > 4 else "parakeet")))

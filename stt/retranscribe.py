"""Re-transcribe one segment of a meeting with a chosen engine: the second
listen on a transcript line.

Run as a one-shot subprocess (`python -m stt.retranscribe <base> <start> <end>
[engine]`) so the long-lived GUI never loads a multi-GB ASR model into its own
memory. Engines (offered()): "parakeet", then "cloud:<provider>" for each cloud
engine whose key is set. A cloud engine gets only the clip of this stretch,
and never one from a strict meeting: its audio stays on this Mac. A different
engine from the one that wrote the transcript gives an independent second
opinion, which is what you want when the first engine misheard.
"""
import json
import math
import subprocess
import sys
import tempfile
import types
import wave
from pathlib import Path

from . import config
from .audio import FFMPEG

# the local engines, as the panel and the child name them. A new local
# engine goes here and in _asr_module().
LOCAL = ("parakeet",)


def offered(strict: bool) -> list:
    """The engine ids a second listen may use on a meeting, Parakeet first:
    the local engines, then each cloud engine whose key is set. A strict
    meeting gets the local engines only."""
    from . import asr_cloud
    if strict:
        return list(LOCAL)
    return list(LOCAL) + [f"cloud:{p}" for p in asr_cloud.PROVIDERS
                          if asr_cloud.available(p)]


def strict_meeting(base: str) -> bool:
    """The meeting's stored strict flag. Metadata that does not read fails
    PRIVATE (strict), as summarize._strict_meeting does."""
    try:
        d = json.loads(config.meeting_file(base, ".json").read_text())
    except (OSError, ValueError):
        return True
    return bool(d.get("strict")) if isinstance(d, dict) else True


def engine_problem(base: str, engine: str):
    """Why `engine` may not listen again on `base`, or None. A cloud engine
    needs its key, and a strict meeting never reaches one."""
    if engine in LOCAL:
        return None
    from . import asr_cloud
    prov = asr_cloud.provider_from_backend(engine)
    if prov not in asr_cloud.PROVIDERS:
        names = ", ".join(LOCAL + tuple(f"cloud:{p}" for p in asr_cloud.PROVIDERS))
        return f"unknown engine {engine!r} (choose from {names})"
    if strict_meeting(base):
        return ("This meeting is strict, so its audio stays on this Mac. "
                "Listen again with Parakeet.")
    if not asr_cloud.available(prov):
        return (f"Add the {asr_cloud.PROVIDERS[prov]['label']} key in Settings "
                "first.")
    return None


def _asr_module(engine: str):
    """An object whose transcribe(clip) runs `engine`. Only the listed
    engines: any "mlxwhisper:<text>" once set <text> as the Whisper model,
    and the child loaded (or fetched) that repo or folder."""
    if engine == "parakeet":
        from . import asr_parakeet
        return asr_parakeet
    from . import asr_cloud
    prov = asr_cloud.provider_from_backend(engine)
    if prov in asr_cloud.PROVIDERS:
        return types.SimpleNamespace(
            IS_CLOUD=True, label=asr_cloud.PROVIDERS[prov]["label"],
            transcribe=lambda clip, progress=None: asr_cloud.transcribe(
                clip, progress=progress, provider=prov))
    raise ValueError(f"unknown engine {engine!r} (choose from "
                     f"{', '.join(LOCAL)} or cloud:<provider>)")


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
    why = span_problem(start, end) or engine_problem(base, engine)
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
        try:
            out = asr.transcribe(clip)
        except Exception as e:
            if not getattr(asr, "IS_CLOUD", False):
                raise
            # a cloud failure (network, quota, size cap) is the person's to
            # read in one line; the detail goes to the panel log
            print(f"second listen: {engine} failed: {e}", file=sys.stderr)
            return {"error": f"{asr.label} did not transcribe this line. "
                             "Try again, or listen again with Parakeet."}
    return {"text": out["text"].strip(), "engine": out["engine"]}


if __name__ == "__main__":
    print(json.dumps(retranscribe(
        sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
        sys.argv[4] if len(sys.argv) > 4 else "parakeet")))

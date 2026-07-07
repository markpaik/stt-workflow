"""Cloud ASR providers behind the same adapter shape as the local engines:
transcribe(wav) -> {"engine", "text", "words": [{start, end, word}]}.

Privacy contract: a cloud engine is used ONLY when explicitly selected as the
transcription model in Settings — and NEVER in strict mode (sensitive
recordings), which pipeline._load_asr forces back to the local engine. Only
the audio leaves the machine (compressed for upload); diarization, speaker
naming, and voiceprints all stay local, so cloud words still get local names.

Keys live in stt.env (git-ignored), entered masked in the panel:
  STT_ELEVENLABS_KEY / STT_OPENAI_KEY / STT_MISTRAL_KEY
Model ids are overridable without code changes as providers version:
  STT_SCRIBE_MODEL / STT_OPENAI_STT_MODEL / STT_MISTRAL_STT_MODEL
"""
import os
import subprocess
import tempfile
from pathlib import Path

from . import config
from .audio import FFMPEG

IS_CLOUD = True  # pipeline checks this to know a local fallback applies
TIMEOUT = int(os.environ.get("STT_CLOUD_TIMEOUT", "900"))

PROVIDERS = {
    "scribe": {"label": "ElevenLabs Scribe", "key_env": "STT_ELEVENLABS_KEY"},
    "openai": {"label": "OpenAI", "key_env": "STT_OPENAI_KEY"},
    "voxtral": {"label": "Mistral Voxtral", "key_env": "STT_MISTRAL_KEY"},
}


def provider_from_backend(backend: str):
    """"cloud:scribe" -> "scribe"; None for local backends."""
    if isinstance(backend, str) and backend.startswith("cloud:"):
        return backend.split(":", 1)[1]
    return None


def api_key(provider: str):
    """stt.env first (the panel writes there; re-read fresh so a key added
    while the panel runs takes effect), then the process environment."""
    env_name = PROVIDERS[provider]["key_env"]
    return config._env_file().get(env_name) or os.environ.get(env_name) or None


def available(provider: str) -> bool:
    return provider in PROVIDERS and bool(api_key(provider))


def _compress_for_upload(wav) -> Path:
    """The 16 kHz PCM working WAV is ~115 MB/hour — recompress to 32 kbps
    mono MP3 (~14 MB/hour) so uploads are fast and fit provider size caps.
    MP3, not AAC: it is the one format all three providers document.
    Caller deletes the temp file."""
    out = Path(tempfile.mkstemp(suffix=".mp3", prefix="sttup_")[1])
    subprocess.run([FFMPEG, "-y", "-i", str(wav), "-ac", "1",
                    "-c:a", "libmp3lame", "-b:a", "32k", str(out)],
                   check=True, capture_output=True)
    return out


def _words_from_segments(segments):
    """Word timings synthesized from segment timings (for providers that
    return only segment granularity): tokens spread evenly inside their
    segment. Coarser than true word timestamps but keeps every downstream
    consumer (diarization merge, review edits) working on the same schema."""
    out = []
    for s in segments:
        toks = str(s.get("text", "")).split()
        if not toks:
            continue
        t0, t1 = float(s["start"]), float(s["end"])
        step = max(0.01, (t1 - t0) / len(toks))
        for i, tok in enumerate(toks):
            out.append({"start": round(t0 + i * step, 3),
                        "end": round(min(t1, t0 + (i + 1) * step), 3),
                        "word": tok})
    return out


def _scribe(path, key):
    import requests
    model = os.environ.get("STT_SCRIBE_MODEL", "scribe_v2")
    with open(path, "rb") as fh:
        r = requests.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": key},
            data={"model_id": model, "timestamps_granularity": "word",
                  "diarize": "false", "tag_audio_events": "false"},
            files={"file": fh}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    words = [{"start": float(w["start"]), "end": float(w["end"]),
              "word": w.get("text", "").strip()}
             for w in d.get("words", [])
             if w.get("type", "word") == "word" and w.get("text", "").strip()]
    return {"engine": f"elevenlabs/{model}", "text": d.get("text", ""), "words": words}


OPENAI_MAX_BYTES = 25 * 1024 * 1024  # documented API cap


def _openai(path, key):
    import requests
    size = Path(path).stat().st_size
    if size > OPENAI_MAX_BYTES:
        raise RuntimeError(
            f"audio is {size / 1e6:.0f} MB compressed — over OpenAI's 25 MB "
            "cap (~1h45m of audio); use Scribe or the local engine for this one")
    # whisper-1 is the documented word-timestamp path (verbose_json +
    # timestamp_granularities); override via env as newer models gain it
    model = os.environ.get("STT_OPENAI_STT_MODEL", "whisper-1")
    with open(path, "rb") as fh:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": model, "response_format": "verbose_json",
                  "timestamp_granularities[]": "word"},
            files={"file": fh}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    words = [{"start": float(w["start"]), "end": float(w["end"]),
              "word": str(w.get("word", "")).strip()}
             for w in d.get("words", []) if str(w.get("word", "")).strip()]
    if not words and d.get("segments"):
        words = _words_from_segments(d["segments"])
    return {"engine": f"openai/{model}", "text": d.get("text", ""), "words": words}


def _voxtral(path, key):
    import requests
    model = os.environ.get("STT_MISTRAL_STT_MODEL", "voxtral-mini-latest")
    with open(path, "rb") as fh:
        r = requests.post(
            "https://api.mistral.ai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            # repeated form fields (requests encodes the list) — word for
            # Transcribe V2, segment kept so older models still return timings
            data={"model": model,
                  "timestamp_granularities": ["word", "segment"]},
            files={"file": fh}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    words = [{"start": float(w["start"]), "end": float(w["end"]),
              "word": str(w.get("word", w.get("text", ""))).strip()}
             for w in d.get("words", [])
             if str(w.get("word", w.get("text", ""))).strip()]
    if not words:
        words = _words_from_segments(d.get("segments", []))
    return {"engine": f"mistral/{model}", "text": d.get("text", ""), "words": words}


_IMPL = {"scribe": _scribe, "openai": _openai, "voxtral": _voxtral}


def transcribe(wav, progress=None) -> dict:
    provider = provider_from_backend(config.ASR_BACKEND)
    if provider not in PROVIDERS:
        raise RuntimeError(f"unknown cloud provider {provider!r}")
    key = api_key(provider)
    if not key:
        raise RuntimeError(f"no API key set for {PROVIDERS[provider]['label']} "
                           f"(add it in Settings)")
    if progress:
        progress(0.05)
    up = _compress_for_upload(wav)
    try:
        if progress:
            progress(0.2)
        out = _IMPL[provider](up, key)
    finally:
        up.unlink(missing_ok=True)
    if not out["words"]:
        raise RuntimeError(f"{PROVIDERS[provider]['label']} returned no words")
    if progress:
        progress(1.0)
    return out

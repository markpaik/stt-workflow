"""ASR backend: Whisper large-v3 / turbo via MLX (GPU on Apple Silicon).

Better punctuation than Parakeet and, unlike WhisperX's CTranslate2 CPU path, runs
on the Metal GPU (~4x realtime for large-v3, ~9x for turbo — measured on the M5 Pro),
so it barely competes with the CPU diarization. word_timestamps=True yields the
word-level timings the diarization merge depends on.

Whisper writes a phrase from its training subtitles ('Thank you.') over
silence, a tone, or a clip too short to hold a word. Its own scores cannot
tell that from speech: on large-v3-turbo a silent 5 s file gave 'Thank you.'
with no_speech_prob 0.0 and avg_logprob -0.25, the same range as the phrase
spoken aloud. So _drop_phantoms works from the text and the audio: a phantom
phrase over a quiet stretch goes, and a transcript of phantom phrases only
is no speech, as Parakeet reports for the same audio.
"""
import os
import re
import wave

from . import config

_MODEL_REPO = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# what Whisper writes when nothing is said, lower case and without punctuation
PHANTOMS = frozenset({
    "thank you", "thank you very much", "thank you so much", "thanks",
    "thanks for watching", "thank you for watching", "you", "bye",
    "subtitles by the amaraorg community",
})
# the loudest 30 ms frame of a phantom's span must reach this to count as
# sound: room tone sits near -60 dBFS, quiet speech near -35
QUIET_DBFS = -45.0
_FRAME_SEC = 0.03


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", str(text or "").lower()).split())


def _read_wav(path):
    """(samples in -1..1, rate) of a 16-bit PCM wav, or None when it cannot
    be read: then only the quiet check is skipped."""
    try:
        import numpy as np
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            ch, rate = w.getnchannels(), w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1)
        return pcm.astype("float32") / 32768.0, rate
    except (OSError, EOFError, ValueError, wave.Error):
        return None


def _loudest_db(audio, start: float, end: float) -> float:
    """The loudest 30 ms frame of [start, end] in dBFS, -inf for silence."""
    import numpy as np
    samples, rate = audio
    a, b = max(0, int(start * rate)), min(len(samples), int(end * rate) + 1)
    part = samples[a:b]
    if not len(part):
        return float("-inf")
    n = max(1, int(_FRAME_SEC * rate))
    frames = part[: len(part) // n * n].reshape(-1, n) if len(part) >= n else part[None, :]
    rms = float(np.sqrt((frames.astype("float64") ** 2).mean(axis=1)).max())
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


def _drop_phantoms(segments, audio) -> list:
    """The segments without Whisper's phantom phrases. A phantom over a
    quiet stretch goes (a muted mic at the end of a meeting). When only
    phantoms are left, nothing was said: a tone or a 50 ms clip is loud
    and still gets 'Thank you.'. A spoken 'Thank you.' inside a meeting,
    over real sound, stays."""
    keep = [s for s in segments
            if not (_norm(s.get("text")) in PHANTOMS and audio is not None
                    and _loudest_db(audio, float(s.get("start") or 0),
                                    float(s.get("end") or 0)) < QUIET_DBFS)]
    if keep and all(_norm(s.get("text")) in PHANTOMS for s in keep):
        return []
    return keep


def transcribe(wav_path, progress=None) -> dict:
    import mlx_whisper

    variant = os.environ.get("STT_WHISPER_MLX_MODEL", "large-v3")
    repo = _MODEL_REPO.get(variant, variant)
    # condition_on_previous_text=False: Whisper's repetition loops ("now now
    # now…") propagate through the rolling prompt from one 30s window to the
    # next; cutting the conditioning contains a loop to a single window. The
    # compression_ratio_threshold retry (default 2.4) then usually clears it.
    r = mlx_whisper.transcribe(str(wav_path), path_or_hf_repo=repo,
                               word_timestamps=True,
                               condition_on_previous_text=False)
    segments = r.get("segments", [])
    kept = _drop_phantoms(segments, _read_wav(wav_path))
    words = []
    for seg in kept:
        for w in seg.get("words", []):
            txt = (w.get("word") or "").strip()
            if txt and "start" in w and "end" in w:
                words.append({"start": round(float(w["start"]), 3),
                              "end": round(float(w["end"]), 3), "word": txt})
    if len(kept) == len(segments):
        text = (r.get("text") or "").strip() or " ".join(w["word"] for w in words)
    else:
        text = " ".join((s.get("text") or "").strip() for s in kept).strip()
    return {"text": text, "words": words, "engine": f"mlx-whisper/{variant}"}

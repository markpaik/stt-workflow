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
is no speech, as Parakeet reports for the same audio, unless the sound
under it varies the way speech does. A spoken 'Thank you.' swings by 8 dB
or more from frame to frame. A tone, a hum or steady noise swings by less
than 0.3 dB.

Over steady noise Whisper can also loop ("if you got your hand here, if
you got your hand here, ..."). Its temperature fallback cannot clear it,
and it keeps the last try. A window that still fails Whisper's own
repetition test (compression ratio above 2.4) goes when the sound under
it is not speech-like.

Only the spans of suspect segments are read from the wav, never the whole
file: a 4-hour recording is about 230 million samples.
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
# a second name for a model above, and the name its speed is learned under
# (stt.rates.whisper_variant): the same repo is the same speed
VARIANT_ALIASES = {"large-v3-turbo": "turbo"}

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
# speech-like: at least this much sound, whose 30 ms frame levels spread by
# at least this many dB (standard deviation). Measured: spoken 'Thank you.',
# 'Yes.' and 'Okay, bye.' 8.1 to 10.1 dB, a tone and a hum 0.0, white and
# pink noise 0.2 to 0.3. A 50 ms clip is too short to hold a word.
SPEECH_MIN_SEC = 0.24
SPEECH_SWING_DB = 3.0
# Whisper's own compression_ratio_threshold: above it a decode is a loop
LOOP_RATIO = 2.4


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", str(text or "").lower()).split())


def _frame_db(path, start: float, end: float):
    """The level of each 30 ms frame of [start, end] of a 16-bit PCM wav,
    in dBFS (-inf for digital silence), or None when the wav cannot be
    read. Only that span is read."""
    try:
        import numpy as np
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            ch, rate, total = w.getnchannels(), w.getframerate(), w.getnframes()
            a = min(total, max(0, int(float(start) * rate)))
            b = min(total, max(a, int(float(end) * rate) + 1))
            w.setpos(a)
            pcm = np.frombuffer(w.readframes(b - a), dtype="<i2")
    except (OSError, EOFError, ValueError, TypeError, wave.Error):
        return None
    if ch > 1:
        pcm = pcm[: len(pcm) // ch * ch].reshape(-1, ch).mean(axis=1)
    part = pcm.astype("float64") / 32768.0
    if not len(part):
        return np.array([float("-inf")])
    n = max(1, int(_FRAME_SEC * rate))
    frames = part[: len(part) // n * n].reshape(-1, n) if len(part) >= n else part[None, :]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    with np.errstate(divide="ignore"):
        return 20 * np.log10(rms)


def _span(s):
    return float(s.get("start") or 0), float(s.get("end") or 0)


def _quiet(path, s):
    """True when the segment's span is quiet. None: the wav is unreadable."""
    db = _frame_db(path, *_span(s))
    return None if db is None else bool(db.max() < QUIET_DBFS)


def _speech_like(path, s) -> bool:
    """True when the sound under the segment varies the way speech does:
    enough frames above QUIET_DBFS, and their levels spread by at least
    SPEECH_SWING_DB. False for a tone, a hum, steady noise, silence, a
    clip too short to hold a word, and an unreadable wav."""
    db = _frame_db(path, *_span(s))
    if db is None:
        return False
    loud = db[db >= QUIET_DBFS]
    return (len(loud) * _FRAME_SEC >= SPEECH_MIN_SEC
            and float(loud.std()) >= SPEECH_SWING_DB)


def _drop_phantoms(segments, wav_path) -> list:
    """The segments without Whisper's phantom phrases, loops and
    punctuation-only lines. A loop
    over sound that is not speech goes. A phantom over a quiet stretch goes
    (a muted mic at the end of a meeting). When only phantoms are left,
    each stays only over speech-like sound: a tone or a 50 ms clip is loud
    and still gets 'Thank you.', and a memo of a spoken 'Thank you.' keeps
    it. A spoken 'Thank you.' inside a meeting, over real sound, stays. An
    unreadable wav skips the quiet check, and a transcript of phantoms only
    is then no speech."""
    def phantom(s):
        return _norm(s.get("text")) in PHANTOMS

    def loop(s):
        try:
            return float(s.get("compression_ratio") or 0) > LOOP_RATIO
        except (TypeError, ValueError):
            return False
    # a segment of punctuation only (" ." over a hum) holds no word
    keep = [s for s in segments if _norm(s.get("text"))
            and not (loop(s) and not _speech_like(wav_path, s))]
    keep = [s for s in keep if not (phantom(s) and _quiet(wav_path, s))]
    if keep and all(phantom(s) for s in keep):
        return [s for s in keep if _speech_like(wav_path, s)]
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
    kept = _drop_phantoms(segments, wav_path)
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

"""ASR backend: Whisper large-v3 / turbo via MLX (GPU on Apple Silicon).

Better punctuation than Parakeet and, unlike WhisperX's CTranslate2 CPU path, runs
on the Metal GPU (~4x realtime for large-v3, ~9x for turbo — measured on the M5 Pro),
so it barely competes with the CPU diarization. word_timestamps=True yields the
word-level timings the diarization merge depends on.
"""
import os

from . import config

_MODEL_REPO = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


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
    words = []
    for seg in r.get("segments", []):
        for w in seg.get("words", []):
            txt = (w.get("word") or "").strip()
            if txt and "start" in w and "end" in w:
                words.append({"start": round(float(w["start"]), 3),
                              "end": round(float(w["end"]), 3), "word": txt})
    text = (r.get("text") or "").strip() or " ".join(w["word"] for w in words)
    return {"text": text, "words": words, "engine": f"mlx-whisper/{variant}"}

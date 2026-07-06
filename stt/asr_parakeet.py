"""ASR backend: NVIDIA Parakeet TDT via MLX (native Apple Silicon GPU).

Returns a dict: {"text", "words": [{start, end, word}], "engine"}.
Word-level timestamps are reconstructed from Parakeet's sub-word AlignedTokens
(a new word begins at a token whose text starts with a space or the SentencePiece
word-boundary marker U+2581).
"""
from pathlib import Path

from . import config

_model = None


def _get_model():
    global _model
    if _model is None:
        from parakeet_mlx import from_pretrained

        _model = from_pretrained(config.PARAKEET_MODEL)
    return _model


def _tokens_to_words(sentences):
    words = []
    for sent in sentences:
        cur = None
        for tok in sent.tokens:
            txt = tok.text
            starts_word = (
                cur is None
                or txt.startswith(" ")
                or txt.startswith("▁")  # SentencePiece word boundary
            )
            if starts_word:
                if cur is not None:
                    words.append(cur)
                cur = {
                    "start": round(float(tok.start), 3),
                    "end": round(float(tok.end), 3),
                    "word": txt.lstrip(" ▁"),
                }
            else:
                cur["word"] += txt
                cur["end"] = round(float(tok.end), 3)
        if cur is not None:
            words.append(cur)
    # drop any empties produced by stray boundary tokens
    return [w for w in words if w["word"].strip()]


def transcribe(wav_path: Path, chunk_duration: float = 300.0,
               overlap_duration: float = 15.0, progress=None) -> dict:
    """Transcribe a 16 kHz mono WAV. Long files are chunked (with explicit overlap)
    to bound memory; Parakeet stitches the aligned tokens back together.
    progress: optional callable(fraction 0..1), fed by the per-chunk callback."""
    model = _get_model()
    cb = (lambda end, total: progress(min(1.0, end / max(1, total)))) if progress else None
    result = model.transcribe(str(wav_path), chunk_duration=chunk_duration,
                              overlap_duration=overlap_duration,
                              chunk_callback=cb)
    words = _tokens_to_words(result.sentences)
    # chunk-seam sanity: word starts must be (near-)monotone; a big backwards jump
    # means the overlap merge produced duplicate/disordered words at a seam
    prev = 0.0
    for w in words:
        if w["start"] < prev - 0.5:
            print(f"   warning: non-monotonic word timing near {prev:.1f}s "
                  "(possible chunk-seam artifact)")
            break
        prev = max(prev, w["start"])
    text = (result.text or "").strip() or " ".join(w["word"] for w in words)
    return {"text": text, "words": words, "engine": config.PARAKEET_MODEL}

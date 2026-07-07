"""Punctuation + truecasing restoration for Parakeet's lowercase run-ons.

Uses 1-800-BAD-CODE/punctuation_fullstop_truecase_english (Apache-2.0, ONNX, CPU,
via the `punctuators` package): inserts punctuation and fixes casing WITHOUT
changing the word sequence — a hard requirement, since word-level speaker labels
must stay aligned 1:1 and sensitive transcripts must never have words altered.
(Never use a generative LLM for this: it can silently rewrite words.)

Applied per merged speaker segment, after attribution. Input is lowercased and
stripped of the ASR's sparse punctuation first, matching the model's expected
input distribution. Fails open: any error returns the original text.
"""
import re

_model = None


def _get_model():
    global _model
    if _model is None:
        from punctuators.models.punc_cap_seg_model import (
            PunctCapSegConfigONNX, PunctCapSegModelONNX)

        # Explicit filenames: the repo was renamed from punct_cap_seg_en and its
        # files don't match the package's default names (sp.model/model.onnx).
        cfg = PunctCapSegConfigONNX(
            hf_repo_id="1-800-BAD-CODE/punctuation_fullstop_truecase_english",
            spe_filename="spe_32k_lc_en.model",
            model_filename="punct_cap_seg_en.onnx",
        )
        _model = PunctCapSegModelONNX(cfg=cfg, ort_providers=["CPUExecutionProvider"])
    return _model


def _normalize_for_model(text: str) -> str:
    text = re.sub(r"[.,!?;:]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _repair_unk(out: str, norm: str) -> str:
    """The model's SentencePiece vocab has no hyphen/dash: 'self-management'
    comes back as '<unk>management'. Word counts still match 1:1, so restore
    the ORIGINAL word at each <unk> position, keeping any punctuation the
    model attached after it."""
    if "<unk>" not in out:
        return out
    out_toks = out.split()
    norm_toks = norm.split()
    if len(out_toks) != len(norm_toks):
        return out
    for i, t in enumerate(out_toks):
        if "<unk>" in t:
            tail = re.search(r"[.,!?;:]*$", t).group()
            out_toks[i] = norm_toks[i] + tail
    return " ".join(out_toks)


def restore(text: str) -> str:
    """Punctuate + truecase one segment of text. Word-preserving; fails open."""
    if not text or not text.strip():
        return text
    try:
        model = _get_model()
        norm = _normalize_for_model(text)
        if not norm:
            return text
        results = model.infer([norm])
        sentences = results[0] if results else []
        out = " ".join(s.strip() for s in sentences if s.strip())
        out = _repair_unk(out, norm)
        # safety: the model may punctuate/truecase freely but must NEVER
        # substitute a different word — checking only the token COUNT would
        # let a same-count substitution ship silently into a transcript of a
        # possibly sensitive conversation. Compare identity word-for-word,
        # ignoring case/punctuation (exactly what the model is allowed to change).
        if _normalize_for_model(out).split() != norm.split():
            return text
        return out or text
    except Exception:
        return text


def restore_segments(segments: list) -> int:
    """Punctuate segment texts in place. Returns how many were changed."""
    changed = 0
    for seg in segments:
        new = restore(seg.get("text", ""))
        if new != seg.get("text"):
            seg["text"] = new
            changed += 1
    return changed

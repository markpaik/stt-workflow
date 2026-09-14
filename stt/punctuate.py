"""Punctuation + truecasing restoration for Parakeet's lowercase run-ons.

Uses 1-800-BAD-CODE/punctuation_fullstop_truecase_english (Apache-2.0, ONNX, CPU,
via the `punctuators` package): inserts punctuation and fixes casing WITHOUT
changing the word sequence — a hard requirement, since word-level speaker labels
must stay aligned 1:1 and sensitive transcripts must never have words altered.
(Never use a generative LLM for this: it can silently rewrite words.)

Applied per merged speaker segment, after attribution. Input is lowercased and
stripped of the ASR's sparse punctuation first, matching the model's expected
input distribution. Fails open: any error returns the original text.

Segments are punctuated in ONE batched infer() call. Measured 2026-09-13 on a
273-segment meeting: 4.6s of the 7.1s relabel was this module, and 4.0s of that
was the package reloading its SentencePiece tokenizer once per infer() call.
The same 273 segments through one call take 0.73s. The word-identity safety
check still runs PER SEGMENT — batching changes how the model is called, never
what is allowed through.
"""
import os
import re
from pathlib import Path

_model = None

_HF_REPO = "1-800-BAD-CODE/punctuation_fullstop_truecase_english"
_SPE_FILENAME = "spe_32k_lc_en.model"
_ONNX_FILENAME = "punct_cap_seg_en.onnx"


def _hf_cache_root() -> Path:
    """Where huggingface_hub keeps downloaded repos, honouring the same env
    overrides it does (so a machine with a relocated cache is read correctly)."""
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return Path(hub).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _model_files_cached() -> bool:
    """True when BOTH files this model needs are already on disk. The loader
    otherwise makes three hf_hub_download HEAD requests per process — network
    round-trips in the middle of a relabel, for files that never change."""
    repo_dir = _hf_cache_root() / ("models--" + _HF_REPO.replace("/", "--"))
    if not repo_dir.is_dir():
        return False
    try:
        names = {p.name for p in repo_dir.rglob("*") if p.is_file()}
    except OSError:
        return False
    return _SPE_FILENAME in names and _ONNX_FILENAME in names


def _get_model():
    global _model
    if _model is None:
        # offline ONLY when the files are provably here: setting it blind would
        # turn a first run (nothing cached yet) from "download once" into a
        # permanent failure. setdefault, so an explicit env choice still wins.
        if _model_files_cached():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from punctuators.models.punc_cap_seg_model import (
            PunctCapSegConfigONNX, PunctCapSegModelONNX)

        # Explicit filenames: the repo was renamed from punct_cap_seg_en and its
        # files don't match the package's default names (sp.model/model.onnx).
        cfg = PunctCapSegConfigONNX(
            hf_repo_id=_HF_REPO,
            spe_filename=_SPE_FILENAME,
            model_filename=_ONNX_FILENAME,
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


def _accept(text: str, norm: str, sentences) -> str:
    """One segment's model output, joined, repaired and SAFETY-CHECKED. Returns
    the restored text, or the original when the check refuses it. Shared by the
    single and the batched path so both can only ever ship the same thing."""
    out = " ".join(s.strip() for s in (sentences or []) if str(s).strip())
    out = _repair_unk(out, norm)
    # safety: the model may punctuate/truecase freely but must NEVER
    # substitute a different word — checking only the token COUNT would
    # let a same-count substitution ship silently into a transcript of a
    # possibly sensitive conversation. Compare identity word-for-word,
    # ignoring case/punctuation (exactly what the model is allowed to change).
    if _normalize_for_model(out).split() != norm.split():
        return text
    return out or text


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
        return _accept(text, norm, results[0] if results else [])
    except Exception:
        return text


def _restore_each(segments: list) -> int:
    """The pre-batch path: one infer() per segment. Kept as the fallback for a
    batched call that raises, so a model or package change that breaks batching
    degrades to slow-but-correct instead of leaving every segment unpunctuated."""
    changed = 0
    for seg in segments:
        new = restore(seg.get("text", ""))
        if new != seg.get("text"):
            seg["text"] = new
            changed += 1
    return changed


def restore_segments(segments: list) -> int:
    """Punctuate segment texts in place. Returns how many were changed.

    ONE infer() call for the whole meeting. Every segment still gets its own
    join, <unk> repair and word-identity check, so a segment the model rewrote
    keeps its original text while its neighbours are punctuated normally."""
    idx, norms = [], []
    for i, seg in enumerate(segments):
        text = seg.get("text", "")
        if not text or not text.strip():
            continue
        norm = _normalize_for_model(text)
        if not norm:
            continue
        idx.append(i)
        norms.append(norm)
    if not norms:
        return 0
    try:
        model = _get_model()
        results = model.infer(norms)
        if results is None or len(results) != len(norms):
            # a result list that does not line up 1:1 cannot be zipped onto the
            # segments without risking text landing on the WRONG speaker's line
            raise ValueError(
                f"punctuator returned {0 if results is None else len(results)} "
                f"results for {len(norms)} segments")
    except Exception:
        return _restore_each(segments)
    changed = 0
    for i, norm, sentences in zip(idx, norms, results):
        seg = segments[i]
        text = seg.get("text", "")
        new = _accept(text, norm, sentences)
        if new != text:
            seg["text"] = new
            changed += 1
    return changed

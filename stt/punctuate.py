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
# the loader reads a third file too: its label/length config. The package's
# default name for it matches this repo, but a local directory must hold it.
_CONFIG_FILENAME = "config.yaml"
_MODEL_FILES = (_SPE_FILENAME, _ONNX_FILENAME, _CONFIG_FILENAME)


def runs_on(engine) -> bool:
    """Whether this module runs on a transcript that `engine` wrote (the
    engine string an ASR module returns, stored as asr_engine): when
    STT_PUNCTUATE=1, except for Qwen3-ASR, which writes its own punctuation
    and casing. Measured 2026-11-11 on 7 meetings against Scribe v2
    (qa/asr-trial): on Qwen output this model lowered sentence-end F1 from
    0.82 to 0.63, comma F1 from 0.62 to 0.54, question-mark F1 from 0.74 to
    0.53, and case agreement from 0.967 to 0.937."""
    from . import config, rates
    return bool(config.PUNCTUATE) and rates.engine_key(engine) != "qwen3"


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


def _cached_model_dir():
    """The one local snapshot directory that holds EVERY file the loader reads,
    or None (a first run, a partial download, files split across revisions).

    Resolved with huggingface_hub.try_to_load_from_cache, which reads the cache
    and never the network. The loader otherwise makes three hf_hub_download
    HEAD requests per process — network round-trips in the middle of a
    relabel, for files that never change."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return None
    dirs = set()
    for name in _MODEL_FILES:
        try:
            found = try_to_load_from_cache(_HF_REPO, name,
                                           cache_dir=str(_hf_cache_root()))
        except Exception:
            return None
        # None = not cached; a sentinel object = "known missing upstream"
        if not isinstance(found, str) or not os.path.isfile(found):
            return None
        dirs.add(os.path.dirname(found))
    return dirs.pop() if len(dirs) == 1 else None


def _get_model():
    """Load the punctuator once per process.

    When every file is already cached, the package is handed the local
    snapshot directory and never calls the hub. Otherwise it downloads as
    normal: a first run must be able to fetch the model. os.environ is never
    touched — the panel is a long-lived process, and an HF_HUB_OFFLINE set
    here would leak into every child it spawns (a batch whose diarizer still
    has to download would fail)."""
    global _model
    if _model is None:
        from punctuators.models.punc_cap_seg_model import (
            PunctCapSegConfigONNX, PunctCapSegModelONNX)

        # Explicit filenames: the repo was renamed from punct_cap_seg_en and its
        # files don't match the package's default names (sp.model/model.onnx).
        local = _cached_model_dir()
        if local:
            cfg = PunctCapSegConfigONNX(
                directory=local,
                spe_filename=_SPE_FILENAME,
                model_filename=_ONNX_FILENAME,
                config_filename=_CONFIG_FILENAME,
            )
        else:
            cfg = PunctCapSegConfigONNX(
                hf_repo_id=_HF_REPO,
                spe_filename=_SPE_FILENAME,
                model_filename=_ONNX_FILENAME,
            )
        _model = PunctCapSegModelONNX(cfg=cfg, ort_providers=["CPUExecutionProvider"])
    return _model


# one model "word": a run with no whitespace and none of the punctuation the
# model is allowed to change. _normalize_for_model splits text into exactly these.
_PIECE_RE = re.compile(r"[^\s.,!?;:]+")
# an abbreviation whose last period is part of the word: 'U.S.', 'e.g.',
# 'a.m.', 'Ph.D.'. Letters only, so the period after '3.5.' still ends a
# sentence and the model's mark replaces it.
_ABBREV_RE = re.compile(r"(?:[^\W\d_]{1,4}\.){2,}")


def _normalize_for_model(text: str) -> str:
    text = re.sub(r"[.,!?;:]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _restore_inner_punct(out: str, text: str) -> str:
    """Put back the punctuation INSIDE an original token: '3.5', '9:30',
    '$1,200', 'v1.2', 'jo.doe@example.org'.

    The model input splits such a token into separate words ('3 5'), and the
    model punctuates the pieces as words. The identity check ignores
    punctuation, so '3 5' would pass it and ship. This rebuilds each such token
    from the ORIGINAL text: its inner punctuation verbatim, the model's casing
    on its first piece only (a sentence-initial 'v1.2' may become 'V1.2'),
    and the model's punctuation after it (or the original's when the model put
    none). Anything the model wrote between the pieces is dropped. The last
    period of an abbreviation ('e.g.') is part of the token: the model's
    mark goes after it ('e.g.,'), never in its place ('e.g,').

    Call it only on output that passed the identity check. Returns the
    original text if the pieces do not line up (they always should)."""
    spans, n = [], 0
    for tok in text.split():
        pieces = list(_PIECE_RE.finditer(tok))
        if len(pieces) >= 2:
            a, b = pieces[0].start(), pieces[-1].end()
            trail = re.match(r"[.,!?;:]*", tok[b:]).group()
            core, abbrev = tok[a:b], False
            if trail.startswith(".") and _ABBREV_RE.fullmatch(core + "."):
                core, trail, abbrev = core + ".", trail[1:], True
            spans.append((n, len(pieces), core, pieces[0].group(), trail, abbrev))
        n += len(pieces)
    if not spans:
        return out
    got = list(_PIECE_RE.finditer(out))
    if len(got) != n:
        return text
    res, pos = [], 0
    for first, count, core, head, trail, abbrev in spans:
        start, end = got[first].start(), got[first + count - 1].end()
        model_head = got[first].group()
        if model_head.lower() == head.lower() and len(model_head) == len(head):
            core = model_head + core[len(head):]
        if abbrev and out[end:end + 1] == ".":
            end += 1        # the model's period is the abbreviation's own
        tail = re.match(r"[.,!?;:]*", out[end:]).group()
        res += [out[pos:start], core]
        if not tail:
            res.append(trail)
        pos = end
    res.append(out[pos:])
    return "".join(res)


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
    # the check above ignores punctuation, so a token whose punctuation is
    # part of the word ('3.5' fed to the model as '3 5') must be rebuilt from
    # the original, or '3 5' ships as the transcript text
    return _restore_inner_punct(out, text) if out else text


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

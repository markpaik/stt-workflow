"""Second-opinion pass: a second ASR engine listens to the same audio, and the
regions where the two engines disagree are flagged for human review.

Why this design (measured on our own Scribe-benchmarked meetings, 07/2026):
the two engines agree on ~95% of words, and agreed words are right ~94% of the
time — so agreement needs no review. Disagreements split roughly evenly on who
is right, and in ~40% of them neither engine matches the reference (crosstalk,
mumbles) — exactly the spots a human should hear. A third ASR engine or
auto-arbitration would mostly chase noise; the review dialog (with both
candidates and the audio cued) is the arbiter.

Regions are persisted to <base>.verify.json so relabel — which rebuilds
segments from the diarization cache — can re-flag them; review decisions then
clear them exactly like any other flag.
"""
import difflib
import json
import re

from . import config

FLAG = "possible:engine_disagreement"
# pure hesitation tokens: a disagreement that only adds/drops these is not
# worth a human's time (them/him, can/could etc. still flag)
FILLERS = {"um", "uh", "uhh", "mm", "mhm", "mmhmm", "hmm", "huh", "ah", "oh", "er"}
MERGE_GAP = 0.8  # disagreement regions closer than this merge into one review item


def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s']", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(words):
    """[(normalized_token, word_index)] — a word may normalize to 0..n tokens."""
    out = []
    for i, w in enumerate(words):
        for tok in _norm(w["word"]).split():
            out.append((tok, i))
    return out


def _substantive(a_toks, b_toks) -> bool:
    return [t for t in a_toks if t not in FILLERS] != [t for t in b_toks if t not in FILLERS]


def regions(primary_words, secondary_words):
    """Align the two token streams; return the time spans where they disagree:
    [{"start","end","ours","theirs"}], merged when nearly adjacent."""
    A, B = _tokens(primary_words), _tokens(secondary_words)
    a_toks, b_toks = [t for t, _ in A], [t for t, _ in B]
    out = []
    sm = difflib.SequenceMatcher(a=a_toks, b=b_toks, autojunk=False)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal" or not _substantive(a_toks[a1:a2], b_toks[b1:b2]):
            continue
        # map the primary token range to word indices -> a time span. A pure
        # insertion (a1==a2) anchors on the words around the gap.
        idxs = [A[i][1] for i in range(a1, a2)]
        if idxs:
            start = primary_words[idxs[0]]["start"]
            end = primary_words[idxs[-1]]["end"]
        else:
            left = primary_words[A[a1 - 1][1]] if a1 > 0 else None
            right = primary_words[A[a1][1]] if a1 < len(A) else None
            start = (left["end"] if left else (right["start"] if right else 0.0))
            end = (right["start"] if right else (left["end"] if left else 0.0))
            if end < start:
                start, end = end, start
        ours = " ".join(a_toks[a1:a2])
        theirs = " ".join(b_toks[b1:b2])
        if out and start - out[-1]["end"] <= MERGE_GAP:
            out[-1]["end"] = max(out[-1]["end"], end)
            out[-1]["ours"] = (out[-1]["ours"] + " … " + ours).strip(" …")
            out[-1]["theirs"] = (out[-1]["theirs"] + " … " + theirs).strip(" …")
        else:
            out.append({"start": round(start, 3), "end": round(end, 3),
                        "ours": ours, "theirs": theirs})
    return out


def secondary_engine(primary_engine: str):
    """The most architecturally-different available engine: Parakeet (TDT) vs
    Whisper turbo (encoder-decoder). Returns (backend, whisper_variant|None)."""
    if "parakeet" in (primary_engine or ""):
        return "mlxwhisper", "turbo"
    return "parakeet", None


def run(wav, primary_words, primary_engine, progress=None):
    """Transcribe `wav` with the second engine and return (regions, engine_name)."""
    import os

    from . import sanitize
    backend, variant = secondary_engine(primary_engine)
    if backend == "mlxwhisper":
        from . import asr_mlxwhisper as asr
        # the variant env var is read at call time — restore it afterwards so the
        # NEXT file's primary Whisper run doesn't silently inherit "turbo"
        prev = os.environ.get("STT_WHISPER_MLX_MODEL")
        os.environ["STT_WHISPER_MLX_MODEL"] = variant
        try:
            out = asr.transcribe(wav, progress=progress)
        finally:
            if prev is None:
                os.environ.pop("STT_WHISPER_MLX_MODEL", None)
            else:
                os.environ["STT_WHISPER_MLX_MODEL"] = prev
    else:
        from . import asr_parakeet as asr
        out = asr.transcribe(wav, progress=progress)
    sec_words, _ = sanitize.collapse_repeats(out["words"])
    if len(sec_words) < 0.3 * max(len(primary_words), 1):
        # the second engine clearly failed on this audio — flagging the entire
        # transcript as "disagreement" would be noise, not signal
        return [], out["engine"]
    return regions(primary_words, sec_words), out["engine"]


def apply_flags(segments, regs):
    """Flag every segment a disagreement region touches and attach the second
    engine's candidate text so the review dialog can offer it."""
    n = 0
    for seg in segments:
        # >= 0, not > 0: a zero-width region (a pure insertion with no
        # inter-word gap, r["start"] == r["end"]) always computes an overlap
        # of exactly 0 against ANY segment it sits inside — never negative
        # unless it's genuinely outside the segment — so a strict > 0 check
        # let every zero-width disagreement escape flagging entirely.
        hits = [r for r in regs
                if min(seg["end"], r["end"]) - max(seg["start"], r["start"]) >= 0]
        if not hits or seg.get("reviewed"):
            continue
        if FLAG not in seg.setdefault("flags", []):
            seg["flags"].append(FLAG)
        seg["alt"] = [{"start": r["start"], "end": r["end"],
                       "ours": r["ours"], "theirs": r["theirs"]} for r in hits]
        n += 1
    return n


def sidecar_path(base: str, dest_dir=None):
    return config.meeting_file(base, ".verify.json", dest_dir)


def save_sidecar(base: str, regs, engine: str, dest_dir=None):
    import os
    p = sidecar_path(base, dest_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # atomic: a kill mid-write must not leave a torn file that load_sidecar
    # silently degrades to None (losing the verify pass's flags)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"engine": engine, "regions": regs}, indent=2))
    os.replace(tmp, p)


def load_sidecar(base: str):
    p = sidecar_path(base)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None

"""Evaluation metrics for tuning the STT+diarization pipeline against a reference.

Reference = ElevenLabs Scribe diarized output. We compute three numbers:
  - WER   (jiwer): transcription only, speaker-agnostic, normalized text.
  - DER   (pyannote.metrics): diarization only, optimal speaker mapping.
  - cpWER (meeteval): concatenated minimum-permutation WER — folds transcription
          AND speaker attribution into one number (the objective we minimize).

Both hypothesis (our .json) and reference (Scribe .json) are reduced to a common
shape: words [{start,end,word,speaker}] and speaker turns [{start,end,speaker}].
Speaker LABELS need not match between hyp and ref — DER and cpWER both solve the
optimal label permutation internally.
"""
import json
import re
from collections import defaultdict

import jiwer

_norm = jiwer.Compose([
    jiwer.ToLowerCase(),
    jiwer.RemovePunctuation(),
    jiwer.RemoveMultipleSpaces(),
    jiwer.Strip(),
    jiwer.ReduceToListOfListOfWords(),
])


def _text_norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------- parsers ----------

def parse_scribe(path) -> dict:
    """ElevenLabs Scribe JSON -> {words, turns, by_speaker}. Tolerant of field names."""
    data = json.loads(open(path).read()) if isinstance(path, str) else path
    words = []
    for w in data.get("words", []):
        typ = w.get("type", "word")
        if typ != "word":
            continue
        txt = (w.get("text") or w.get("word") or "").strip()
        if not txt:
            continue
        spk = str(w.get("speaker_id", w.get("speaker", "spk0")))
        words.append({"start": float(w.get("start", 0.0)),
                      "end": float(w.get("end", w.get("start", 0.0))),
                      "word": txt, "speaker": spk})
    return _finalize(words, full_text=data.get("text"))


def parse_ours(path) -> dict:
    """Our pipeline .json -> {words, turns, by_speaker}."""
    data = json.loads(open(path).read()) if isinstance(path, str) else path
    words = []
    for w in data.get("words", []):
        txt = (w.get("word") or "").strip()
        if not txt:
            continue
        spk = w.get("speaker")
        words.append({"start": float(w["start"]), "end": float(w["end"]),
                      "word": txt, "speaker": str(spk) if spk is not None else "none"})
    return _finalize(words)


def _finalize(words, full_text=None) -> dict:
    words.sort(key=lambda w: w["start"])
    # turns: merge consecutive same-speaker words
    turns = []
    for w in words:
        if turns and turns[-1]["speaker"] == w["speaker"]:
            turns[-1]["end"] = w["end"]
        else:
            turns.append({"start": w["start"], "end": w["end"], "speaker": w["speaker"]})
    by_speaker = defaultdict(list)
    for w in words:
        by_speaker[w["speaker"]].append(w["word"])
    text = full_text if full_text else " ".join(w["word"] for w in words)
    return {"words": words, "turns": turns,
            "by_speaker": {k: " ".join(v) for k, v in by_speaker.items()},
            "text": text}


# ---------- metrics ----------

def wer(ref: dict, hyp: dict) -> float:
    r, h = _text_norm(ref["text"]), _text_norm(hyp["text"])
    if not r:
        return float("nan")
    return float(jiwer.wer(r, h))


def der(ref: dict, hyp: dict) -> float:
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    def _ann(turns):
        a = Annotation()
        for i, t in enumerate(turns):
            if t["end"] > t["start"]:
                a[Segment(t["start"], t["end"]), i] = t["speaker"]
        return a
    metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    return float(metric(_ann(ref["turns"]), _ann(hyp["turns"])))


def cpwer(ref: dict, hyp: dict) -> float:
    from meeteval.wer import cp_word_error_rate

    def _norm_dict(bs):
        return {k: _text_norm(v) for k, v in bs.items() if _text_norm(v)}
    r = _norm_dict(ref["by_speaker"]) or {"a": ""}
    h = _norm_dict(hyp["by_speaker"]) or {"a": ""}
    res = cp_word_error_rate(r, h)
    return float(res.error_rate) if res.error_rate is not None else float("nan")


def score(ref_obj: dict, hyp_obj: dict) -> dict:
    return {"wer": wer(ref_obj, hyp_obj),
            "der": der(ref_obj, hyp_obj),
            "cpwer": cpwer(ref_obj, hyp_obj),
            "ref_speakers": len(ref_obj["by_speaker"]),
            "hyp_speakers": len(hyp_obj["by_speaker"]),
            "ref_words": len(ref_obj["words"]),
            "hyp_words": len(hyp_obj["words"])}

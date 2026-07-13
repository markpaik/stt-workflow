"""Merge ASR words with diarization turns into speaker-labeled segments.

Words are assigned by MAXIMUM temporal overlap with a turn (not midpoint —
midpoint flips boundary words under small ASR/diarization clock skew), with the
previous word's speaker as tie-break, then nearest turn as fallback.

Uncertainty is span-based: words inside overlap regions or uncertainty spans
(from refinement) are flagged individually; a segment is marked only when flagged
words make up at least half of it — so a 0.2s doubtful fragment marks itself, not
the 30-second clean turn it sits beside.
"""
import re

# flags that mean "verify against audio" (vs. confident provenance like "reassigned")
FRAGILE_FLAGS = {"overlap", "smoothed", "short_low_confidence", "protected_answer",
                 "id_mismatch"}


def _clean(text: str) -> str:
    text = re.sub(r"\s+([,.!?;:%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text.strip()


def _assign(word, turns, prev_speaker):
    ws, we = word["start"], word["end"]
    if we == ws:
        # a zero-duration word (start==end after 3-decimal rounding) can
        # never produce a positive overlap below, even sitting squarely
        # inside a turn — ov comes out exactly 0 either way, indistinguishable
        # from "no overlap at all" under a strict > comparison, so it always
        # fell through to the nearest-turn distance fallback instead of the
        # turn it's actually inside — non-deterministic at a speaker boundary.
        containing = [t for t in turns if t["start"] <= ws <= t["end"]]
        if containing:
            for t in containing:
                if t["speaker"] == prev_speaker:
                    return t
            return containing[0]
    best, best_ov = None, 0.0
    for t in turns:
        ov = min(we, t["end"]) - max(ws, t["start"])
        if ov > best_ov or (ov == best_ov and ov > 0 and t["speaker"] == prev_speaker):
            best, best_ov = t, ov
    if best is not None and best_ov > 0:
        return best
    # no overlap at all: nearest turn
    mid = (ws + we) / 2.0
    nearest, ndist = None, None
    for t in turns:
        d = t["start"] - mid if mid < t["start"] else max(0.0, mid - t["end"])
        if ndist is None or d < ndist:
            ndist, nearest = d, t
    return nearest


def _word_flags(word, overlaps, spans):
    mid = (word["start"] + word["end"]) / 2.0
    flags = {s["flag"] for s in spans if s["start"] <= mid <= s["end"]}
    if any(s <= mid <= e for s, e in overlaps):
        flags.add("overlap")
    return sorted(flags)


def assign_and_group(words, turns, names, overlaps=None, spans=None,
                     overlap_min_sec=0.0):
    """words: [{start,end,word}]; turns: [{start,end,speaker}]; names:
    {key: {"name","score"}}; overlaps: [(start,end)] multi-speaker regions;
    spans: [{"start","end","flag"}] uncertainty/provenance from refinement;
    overlap_min_sec: a SEGMENT gets the "overlap" review flag only when the
    crosstalk inside it sums to at least this long (words keep their flags
    regardless). 0.0 = any crosstalk flags, the strict-mode behavior.

    Returns (segments, labeled_words):
      segments: [{start,end,speaker,name,text,attribution,flags,overlap}]
      labeled_words: words annotated with "speaker" (+ "flags" where nonempty)
    """
    overlaps = overlaps or []
    spans = spans or []
    labeled, prev = [], None
    for w in words:
        t = _assign(w, turns, prev) if turns else None
        lw = dict(w)
        lw["speaker"] = t["speaker"] if t else None
        wf = _word_flags(w, overlaps, spans)
        if wf:
            lw["flags"] = wf
        labeled.append(lw)
        prev = lw["speaker"]

    segments, cur, cur_flag_words = [], None, []
    def _finish():
        if cur is None:
            return
        cur["text"] = _clean(cur["text"])
        n = len(cur_flag_words)
        flag_counts = {}
        for wf in cur_flag_words:
            for f in wf:
                flag_counts[f] = flag_counts.get(f, 0) + 1
        # a flag marks the segment only when it covers >= half its words
        seg_flags = sorted(f for f, c in flag_counts.items() if c >= max(1, n) / 2)
        cur["flags"] = [f for f in seg_flags if f in FRAGILE_FLAGS or f.startswith("possible:")]
        if overlap_min_sec > 0 and "overlap" in cur["flags"]:
            # sub-floor crosstalk (a backchannel brushing a turn boundary) keeps
            # its word-level provenance but must not demand review of the whole
            # segment — drop the flag HERE so cur["overlap"] below derives from
            # the final flag list and the two can never disagree
            inside = sum(max(0.0, min(e, cur["end"]) - max(s, cur["start"]))
                         for s, e in overlaps)
            if inside < overlap_min_sec:
                cur["flags"].remove("overlap")
        cur["attribution"] = ("smoothed" if "smoothed" in seg_flags else
                              "reassigned" if "reassigned" in seg_flags else "diarized")
        cur["overlap"] = "overlap" in cur["flags"]
        segments.append(cur)

    for lw in labeled:
        if cur is None or lw["speaker"] != cur["speaker"]:
            _finish()
            cur = {"start": lw["start"], "end": lw["end"], "speaker": lw["speaker"],
                   "text": lw["word"]}
            cur_flag_words = [lw.get("flags", [])]
        else:
            cur["end"] = lw["end"]
            cur["text"] += " " + lw["word"]
            cur_flag_words.append(lw.get("flags", []))
    _finish()

    for seg in segments:
        info = names.get(seg["speaker"], {}) if seg["speaker"] else {}
        seg["name"] = info.get("name")
        if info.get("display"):
            seg["display"] = info["display"]
    return segments, labeled

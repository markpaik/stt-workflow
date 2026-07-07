"""Clean raw ASR output: collapse hallucination loops, strip vocab artifacts.

Loops: on silence or crosstalk Whisper can get stuck emitting one token — "now
now now now…" for hundreds of words — or a short phrase on repeat. We collapse
any run of the same 1-3 word pattern beyond a small natural limit, keep a couple
of repeats (people really do say "no no no"), and flag the span for human review
("possible:asr_loop").

Artifacts: Parakeet emits "<unk>" for symbols outside its vocabulary — almost
always a hyphen/dash ("two<unk>year" = "two-year"). Between letters it becomes a
hyphen; anywhere else it's dropped.

Applied after ASR in the pipeline AND on relabel, so already-processed
transcripts self-heal on the next relabel pass.
"""
import re

MAX_NATURAL_REPEATS = 3   # keep up to this many repeats of a pattern
MIN_LOOP_REPEATS = 5      # a run this long is a loop, not speech

_UNK_HYPHEN = re.compile(r"(?<=\w)<unk>(?=\w)")


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def strip_unk(word: str) -> str:
    if "<unk>" not in word:
        return word
    return _UNK_HYPHEN.sub("-", word).replace("<unk>", "")


def collapse_repeats(words: list) -> tuple:
    """words: [{start,end,word,...}] -> (clean_words, loop_spans).
    loop_spans: [{"start","end","flag":"possible:asr_loop"}] covering what was cut."""
    cleaned = []
    for w in words:
        txt = strip_unk(w["word"])
        if txt != w["word"]:
            if not txt.strip():
                continue  # a lone "<unk>" word: nothing left to keep
            w = {**w, "word": txt}
        cleaned.append(w)
    words = cleaned
    n = len(words)
    keep = [True] * n
    spans = []
    i = 0
    while i < n:
        matched = False
        for plen in (1, 2, 3):
            if i + plen * MIN_LOOP_REPEATS > n:
                continue
            pat = [_norm(words[i + k]["word"]) for k in range(plen)]
            if not all(pat):
                continue
            reps = 1
            j = i + plen
            while (j + plen <= n and
                   [_norm(words[j + k]["word"]) for k in range(plen)] == pat):
                reps += 1
                j += plen
            if reps >= MIN_LOOP_REPEATS:
                cut_from = i + plen * MAX_NATURAL_REPEATS
                for k in range(cut_from, i + reps * plen):
                    keep[k] = False
                spans.append({"start": words[i]["start"],
                              "end": words[i + reps * plen - 1]["end"],
                              "flag": "possible:asr_loop"})
                i = i + reps * plen
                matched = True
                break
        if not matched:
            i += 1
    if not spans:
        return words, []
    return [w for k, w in zip(keep, words) if k], spans

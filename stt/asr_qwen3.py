"""ASR backend: Qwen3-ASR 1.7B through mlx-audio, in a child process.

Returns what asr_parakeet.transcribe returns:
{"text", "words": [{start, end, word}], "engine"}.

Why a child: mlx-audio needs a newer mlx than parakeet-mlx pins, so the
model runs under its own interpreter, STT_QWEN_ASR_PY (stt/qwen3_worker.py
is the child). This module imports neither; it plans the chunks, runs the
loop guard and merges the result, so all of that is testable without MLX.

The steps, measured on 7 meetings against Scribe v2 (qa/asr-bench):
- 300 s chunks with 15 s overlap, as Parakeet runs.
- Loop guard. Qwen3-ASR can fall into a repeat loop on a chunk (one
  meeting, the same chunk on every run). A chunk fails when one 3- to
  6-word run repeats more than 8 times in a row, or when it has more than
  5 words per second of audio. A failed chunk is decoded again as two
  halves with 15 s overlap. A half that still fails is decoded once more
  with repetition_penalty 1.15. The halves are joined on their longest
  shared word run.
- Qwen3-ASR gives no word times. The forced aligner
  (Qwen3-ForcedAligner-0.6B) aligns each chunk's final text to its audio.
  The aligner sees words with their punctuation removed; each aligned
  item is matched back to the word it came from, so the words keep
  Qwen's own punctuation and casing.
- The aligner holds its accuracy for about the first 260 s of a stretch
  (median offset 0.06 s against Scribe), then squeezes the rest onto
  ~270 s: on 300 s chunks of one meeting the last 30 s were 10 to 30 s
  early. So a stretch longer than ALIGN_FULL_SEC is aligned in windows:
  the words up to the widest pause between ALIGN_CUT_SEC[0] and
  ALIGN_CUT_SEC[1] are kept, and the rest of the text is aligned again
  from that pause.
- Repair. In some short stretches the aligner still puts words 2 to 20 s
  off (2.3 percent of matched words on 7 meetings, against 0.1 percent for
  Parakeet), and those words then get the wrong speaker far more often.
  Such a stretch has a shape: a word stretched LONG_WORD_SEC or more while
  the aligner catches up, 8 words packed into PACKED_SEC, or several words
  with no length. Each flagged stretch is aligned again on its own, between
  the words on either side of it, and the new times are kept only when
  they have fewer of those marks. A stretch that does not improve is tried
  once more with REPAIR_WIDEN words more on each side. Two passes. Scored
  on two meetings (qa/asr-trial): words 1 s or more off fell 45 percent
  (780 to 429), and the share with the right speaker rose 0.16 and 0.23
  points, for about 3 percent more run time. Much of the rest is speech
  that Qwen did not transcribe: the aligner cannot skip it, so it pulls
  the next words into it.
- Neighbouring chunks are joined by word time: inside the overlap, the
  longest run of the same words at the same times (within ANCHOR_TOL_SEC)
  is the seam, cut at the word nearest the middle of the overlap. Then a
  run of 4 or more of the same words at other times, then the middle of
  the overlap (merge_chunk_words).

A missing interpreter, package or model is one EngineUnavailable error.
The pipeline then warns once and transcribes with Parakeet (FIX says what
to do), and the doctor reports it as asr_fallback. After the first such
error a process does not try again.
"""
import difflib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import wave
from pathlib import Path

from . import config

CHUNK_SEC = 300.0
OVERLAP_SEC = 15.0
MAX_WPS = 5.0              # words per second of audio, above any real speech rate
MAX_NGRAM_REPEATS = 8      # back-to-back repeats of one 3- to 6-word run
REPETITION_PENALTY = 1.15
ANCHOR_TOL_SEC = 0.5       # two chunks agree on a word's time within this
ANCHOR_MIN_WORDS = 2
MIN_DECODE_SEC = 0.5       # a stretch shorter than this has no speech to decode
ALIGN_FULL_SEC = 240.0     # a stretch up to this long is aligned in one call
ALIGN_CUT_SEC = (150.0, 190.0)  # a longer one is cut at a pause in this window
LONG_WORD_SEC = 1.5        # repair: a word stretched this long marks a catch-up
PACKED_SEC = 1.0           # repair: 8 words starting within this are packed
REPAIR_PASSES = 2
REPAIR_WIDEN = 20          # words added on each side when a stretch does not improve
# progress within one chunk: share of its time spent decoding (the rest is
# alignment and repair). Measured in the trial: decode ~85 percent.
DECODE_SHARE = 0.85
WORKER = Path(__file__).resolve().parent / "qwen3_worker.py"


class EngineUnavailable(RuntimeError):
    """The engine cannot run on this Mac: no interpreter, no mlx-audio, or a
    model that is not downloaded. One plain sentence, no traceback."""


FIX = ("run ./init.sh, which builds .venv-qwen and downloads the two models, "
       "or set STT_ASR_BACKEND=parakeet")
_unavailable = None   # why the engine could not start in this process, once it failed


# ---------- pure helpers (no child process) ----------

def chunk_plan(duration, chunk=CHUNK_SEC, overlap=OVERLAP_SEC):
    """[(start, end)] covering [0, duration], step chunk - overlap. The last
    chunk ends at the end of the audio."""
    if duration <= 0:
        return []
    out, s = [], 0.0
    while True:
        e = min(duration, s + chunk)
        out.append((round(s, 3), round(e, 3)))
        if e >= duration:
            return out
        s += chunk - overlap


def _key(w):
    return re.sub(r"[^a-z0-9']", "", str(w).lower())


def max_ngram_run(words):
    """The most back-to-back repeats of one n-gram (n 3..6): (reps, n)."""
    w = [_key(x) for x in words]
    best = (1, 0)
    n_words = len(w)
    for n in range(3, 7):
        i = 0
        while i + n <= n_words:
            reps = 1
            while w[i + reps * n: i + (reps + 1) * n] == w[i: i + n]:
                reps += 1
            if reps > best[0]:
                best = (reps, n)
            i += n * reps if reps > 1 else 1
    return best


def guard_reasons(text, seconds):
    """Why a decoded stretch looks like a loop ([] when it passes)."""
    words = str(text or "").split()
    reasons = []
    reps, n = max_ngram_run(words)
    if reps > MAX_NGRAM_REPEATS:
        reasons.append(f"a {n}-word run repeated {reps} times")
    wps = len(words) / max(seconds, 1e-9)
    if wps > MAX_WPS:
        reasons.append(f"{wps:.2f} words/s")
    return reasons


def merge_text(a, b, window=80, min_block=4):
    """Join two word lists that overlap in time, on their longest shared run
    of words (4 or more) within `window` words of the seam; with no such run
    they are simply joined."""
    if not a:
        return list(b)
    tail = [_key(x) for x in a[-window:]]
    head = [_key(x) for x in b[:window]]
    m = difflib.SequenceMatcher(None, tail, head, autojunk=False).find_longest_match(
        0, len(tail), 0, len(head))
    if m.size >= min_block:
        base = len(a) - len(tail)
        return list(a[: base + m.a + m.size]) + list(b[m.b + m.size:])
    return list(a) + list(b)


def _kept(ch):
    """The aligner's rule for a character it keeps: letters, digits and the
    apostrophe (mlx_audio ForceAlignProcessor.is_kept_char)."""
    if ch == "'":
        return True
    cat = unicodedata.category(ch)
    return cat.startswith("L") or cat.startswith("N")


def _aligner_form(token):
    return "".join(ch for ch in token if _kept(ch))


def attach_times(text, items, offset, limit):
    """Words with times from the aligner's items for one chunk.

    `items` are [word, start, end] in seconds from the chunk start; the
    aligner dropped the punctuation, so each item is matched back to its
    word in `text` (the text keeps its own punctuation and casing). A
    token with no letter or digit ('-', '...') is kept at the end of the
    word before it, with no length. Times are clamped to [0, limit] and
    shifted by `offset`. When the items do not line up with the text, the
    items themselves are the words: the times stay, the punctuation goes."""
    toks = str(text or "").split()
    out, j = [], 0
    ok = True
    for tok in toks:
        form = _aligner_form(tok)
        if not form:
            t = out[-1]["end"] if out else round(offset, 3)
            out.append({"start": t, "end": t, "word": tok})
            continue
        if j >= len(items):
            ok = False
            break
        acc, st, en = str(items[j][0]), float(items[j][1]), float(items[j][2])
        j += 1
        # a token the aligner split into pieces (CJK characters) joins back
        while acc != form and j < len(items) and len(acc) < len(form):
            acc += str(items[j][0])
            en = float(items[j][2])
            j += 1
        if acc != form:
            ok = False
            break
        st = min(max(st, 0.0), limit)
        en = min(max(en, st), limit)
        out.append({"start": round(offset + st, 3), "end": round(offset + en, 3),
                    "word": tok})
    if ok and j == len(items):
        return out
    words = []
    for w, st, en in items:
        st = min(max(float(st), 0.0), limit)
        en = min(max(float(en), st), limit)
        if str(w).strip():
            words.append({"start": round(offset + st, 3), "end": round(offset + en, 3),
                          "word": str(w)})
    return words


def merge_chunk_words(prev, nxt, ov_start, ov_end):
    """Join the words so far (`prev`) with the next chunk's (`nxt`), whose
    audio overlaps on [ov_start, ov_end].

    The seam, in order of trust:
    1. a block of ANCHOR_MIN_WORDS or more of the same words at the same
       times (each pair within ANCHOR_TOL_SEC): the longest, cut at its word
       nearest the middle of the overlap;
    2. a block of 4 or more of the same words whose times disagree (one
       side's aligner is off): the longest, cut the same way;
    3. the middle of the overlap, by word start.
    The words up to the cut come from `prev`, the rest from `nxt`."""
    if not prev:
        return list(nxt)
    if not nxt:
        return list(prev)
    mid = (ov_start + ov_end) / 2.0
    pa = [i for i, w in enumerate(prev) if w["start"] >= ov_start - 1.0]
    nb = [i for i, w in enumerate(nxt) if w["start"] <= ov_end + 1.0]
    timed = loose = None
    if pa and nb:
        ka = [_key(prev[i]["word"]) for i in pa]
        kb = [_key(nxt[i]["word"]) for i in nb]
        sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
        for blk in sm.get_matching_blocks():
            if blk.size < ANCHOR_MIN_WORDS:
                continue
            pairs = [(pa[blk.a + k], nb[blk.b + k]) for k in range(blk.size)]
            seam = min(pairs, key=lambda p: abs(prev[p[0]]["start"] - mid))
            rank = (blk.size, -abs(prev[seam[0]]["start"] - mid))
            if all(abs(prev[i]["start"] - nxt[j]["start"]) <= ANCHOR_TOL_SEC
                   for i, j in pairs):
                if timed is None or rank > timed[0]:
                    timed = (rank, seam)
            elif blk.size >= 4 and (loose is None or rank > loose[0]):
                loose = (rank, seam)
    best = timed or loose
    if best is not None:
        i, j = best[1]
        return list(prev[: i + 1]) + list(nxt[j + 1:])
    return ([w for w in prev if w["start"] < mid]
            + [w for w in nxt if w["start"] >= mid])


# ---------- the child process ----------

def _setting(name, default) -> str:
    """The environment, then stt.env read fresh (the panel is long-lived and
    launchd gives it no stt.env), then the default."""
    return os.environ.get(name) or config._env_file().get(name) or str(default)


def interpreter() -> Path:
    return Path(_setting("STT_QWEN_ASR_PY", config.QWEN_ASR_PY)).expanduser()


def asr_model() -> str:
    return _setting("STT_QWEN_ASR_MODEL", config.QWEN_ASR_MODEL)


def aligner_model() -> str:
    return _setting("STT_QWEN_ALIGNER_MODEL", config.QWEN_ALIGNER_MODEL)


def model_dir(repo):
    """The local folder that holds `repo`'s config and weights: the path
    itself, or its snapshot in the Hugging Face cache. None when it is not
    downloaded. Reads the cache only, never the network."""
    p = Path(repo).expanduser()
    if p.is_dir():
        return p if (p / "config.json").is_file() else None
    try:
        from huggingface_hub import try_to_load_from_cache
        found = try_to_load_from_cache(repo, "config.json")
    except Exception:
        return None
    if not isinstance(found, str) or not os.path.isfile(found):
        return None
    d = Path(found).parent
    return d if any(d.glob("*.safetensors")) else None


def problem(deep=False):
    """Why this engine cannot run on this Mac, or None. Quick checks: the
    interpreter and both models in the local cache. deep=True also runs
    the interpreter to import mlx_audio (a second or two)."""
    py = interpreter()
    if not (py.is_file() and os.access(py, os.X_OK)):
        return f"there is no Python environment at STT_QWEN_ASR_PY ({py})"
    for repo in (asr_model(), aligner_model()):
        if model_dir(repo) is None:
            return f"the model {repo} is not downloaded"
    if deep:
        try:
            r = subprocess.run([str(py), "-I", "-c", "import mlx_audio"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode:
                return f"mlx-audio does not import in {py}"
        except (OSError, subprocess.SubprocessError):
            return f"{py} does not run"
    return None


class _Worker:
    """The running child: one request line out, one reply line back."""

    def __init__(self, wav_path):
        why = problem()
        if why:
            raise EngineUnavailable(why)
        py = interpreter()
        self.err = tempfile.TemporaryFile(mode="w+b")
        env = dict(os.environ)
        env["HF_HUB_OFFLINE"] = "1"          # cache only: never a download mid-run
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen(
            [str(py), "-I", str(WORKER), "--wav", str(wav_path),
             "--asr", asr_model(), "--aligner", aligner_model()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
            env=env, text=True, bufsize=1)
        first = self._read()
        if first is None or first.get("unavailable") or not first.get("ready"):
            why = (first or {}).get("error") or self._stderr_tail() or "no reply"
            self.close()
            raise EngineUnavailable(f"the Qwen3-ASR child did not start: {why}")

    def _stderr_tail(self, n=6) -> str:
        try:
            self.err.seek(0)
            lines = self.err.read().decode("utf-8", "replace").strip().splitlines()
            return " | ".join(lines[-n:])
        except (OSError, ValueError):
            return ""

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            return {"error": "an unreadable reply from the Qwen3-ASR child"}

    def ask(self, req: dict) -> dict:
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        rep = self._read()
        if rep is None:
            raise RuntimeError(f"the Qwen3-ASR child stopped: {self._stderr_tail()}")
        if "error" in rep:
            raise RuntimeError(f"Qwen3-ASR: {rep['error']}")
        return rep

    def transcribe(self, start, end, repetition_penalty=None) -> str:
        if end - start < MIN_DECODE_SEC:
            return ""
        return self.ask({"op": "transcribe", "start": start, "end": end,
                         "repetition_penalty": repetition_penalty}).get("text", "")

    def align(self, start, end, text) -> list:
        if not str(text).strip():
            return []
        return self.ask({"op": "align", "start": start, "end": end,
                         "text": text}).get("items", [])

    def close(self):
        """Ask the child to quit; returns its bye reply, or None."""
        bye = None
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                self.proc.stdin.flush()
                bye = self._read()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for fh in (self.proc.stdin, self.proc.stdout):
            try:
                fh.close()
            except OSError:
                pass
        self.err.close()
        return bye


def _decode(worker, s, e, note, step=lambda sub: None):
    """One chunk's text, through the loop guard and its fallbacks. `step`
    gets the share of the chunk done (0..DECODE_SHARE) after each decode."""
    text = worker.transcribe(s, e)
    step(DECODE_SHARE)
    reasons = guard_reasons(text, e - s)
    if not reasons:
        return text
    half = (e - s) / 2.0
    halves = ((s, min(e, s + half + OVERLAP_SEC / 2)), (max(s, s + half - OVERLAP_SEC / 2), e))
    merged, unresolved = [], False
    # the re-decodes take about as long again: the bar holds near the
    # decode share and creeps, so it never runs ahead and steps back
    extra = (1.0 - DECODE_SHARE) / 4
    for h, (hs, he) in enumerate(halves):
        ht = worker.transcribe(hs, he)
        if guard_reasons(ht, he - hs):
            ht = worker.transcribe(hs, he, repetition_penalty=REPETITION_PENALTY)
            unresolved = unresolved or bool(guard_reasons(ht, he - hs))
        step(DECODE_SHARE + extra * (h + 1))
        merged = merge_text(merged, ht.split())
    note(f"   qwen3: the chunk at {s:.0f} s failed the loop guard ({'; '.join(reasons)}); "
         f"decoded again as two halves" + (", and a half still fails" if unresolved else ""))
    return " ".join(merged)


def _cut_index(words, t0):
    """Where to cut a long aligned stretch that starts at t0: the index of
    the last word kept, at the widest pause after a word that ends inside
    ALIGN_CUT_SEC. None when no word ends there."""
    lo, hi = t0 + ALIGN_CUT_SEC[0], t0 + ALIGN_CUT_SEC[1]
    best, best_gap = None, -1.0
    for i in range(len(words) - 1):
        if lo <= words[i]["end"] <= hi:
            gap = words[i + 1]["start"] - words[i]["end"]
            if gap > best_gap:
                best, best_gap = i, gap
    return best


def align_chunk(worker, s, e, text):
    """Word times for one chunk's text, aligned in windows (module notes)."""
    toks = str(text or "").split()
    out, t0 = [], s
    while toks:
        part = " ".join(toks)
        cw = attach_times(part, worker.align(t0, e, part), t0, e - t0)
        if e - t0 <= ALIGN_FULL_SEC or len(cw) != len(toks):
            # short enough, or the items did not line up with the words
            return out + cw
        i = _cut_index(cw, t0)
        if i is None:
            return out + cw
        out += cw[: i + 1]
        nxt = (cw[i]["end"] + cw[i + 1]["start"]) / 2.0
        t0 = max(t0, min(nxt, e))
        toks = toks[i + 1:]
    return out


def _no_length(w):
    """A word with no length that should have one (a lone '-' has none)."""
    return w["end"] - w["start"] <= 0.0 and any(ch.isalnum() for ch in w["word"])


def _marks(words):
    """How many repair marks a run of aligned words carries."""
    n = sum(1 for w in words if w["end"] - w["start"] >= LONG_WORD_SEC)
    n += sum(1 for k in range(len(words) - 7)
             if words[k + 7]["start"] - words[k]["start"] < PACKED_SEC)
    return n + sum(1 for w in words if _no_length(w))


def suspect_stretches(words):
    """[(i0, i1)] index ranges of aligned words that look misplaced: around a
    word stretched LONG_WORD_SEC or more (12 words each side), around 8 words
    packed into PACKED_SEC, and around two or more words with no length
    within 13 words. Ranges closer than 4 words are joined."""
    n = len(words)
    flag = [False] * n

    def mark(a, b):
        for t in range(max(0, a), min(n, b)):
            flag[t] = True
    for k, w in enumerate(words):
        if w["end"] - w["start"] >= LONG_WORD_SEC:
            mark(k - 12, k + 13)
        if (_no_length(w) and sum(1 for t in range(max(0, k - 6), min(n, k + 7))
                                  if _no_length(words[t])) >= 2):
            mark(k - 8, k + 9)
    for k in range(n - 7):
        if words[k + 7]["start"] - words[k]["start"] < PACKED_SEC:
            mark(k - 4, k + 12)
    out, i = [], 0
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and any(flag[j + 1:j + 4]):
            j += 1
        out.append((i, j))
        i = j + 1
    return out


def _realign(worker, words, i0, i1, s, e):
    """Words i0..i1 aligned again on their own, between the end of the word
    before and the start of the word after; None when there is no room or
    the items do not line up."""
    a = words[i0 - 1]["end"] if i0 > 0 else s
    b = words[i1 + 1]["start"] if i1 + 1 < len(words) else e
    part = words[i0:i1 + 1]
    if b - a < 0.1 * len(part):
        return None
    text = " ".join(w["word"] for w in part)
    new = attach_times(text, worker.align(a, b, text), a, b - a)
    return new if len(new) == len(part) else None


def repair_chunk(worker, words, s, e, counts=None):
    """Align each suspect stretch of one chunk's words again (module notes).
    Keeps new times only when they carry fewer marks. `counts`, a dict,
    gets "flagged" and "repaired" added."""
    words = list(words)
    counts = {} if counts is None else counts
    for _ in range(REPAIR_PASSES):
        for i0, i1 in suspect_stretches(words):
            counts["flagged"] = counts.get("flagged", 0) + 1
            new = _realign(worker, words, i0, i1, s, e)
            if new is not None and _marks(new) < _marks(words[i0:i1 + 1]):
                words[i0:i1 + 1] = new
                counts["repaired"] = counts.get("repaired", 0) + 1
                continue
            j0, j1 = max(0, i0 - REPAIR_WIDEN), min(len(words) - 1, i1 + REPAIR_WIDEN)
            new = _realign(worker, words, j0, j1, s, e)
            if new is not None and _marks(new) < _marks(words[j0:j1 + 1]):
                words[j0:j1 + 1] = new
                counts["repaired"] = counts.get("repaired", 0) + 1
    return words


def _duration(wav_path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def transcribe(wav_path: Path, chunk_duration: float = CHUNK_SEC,
               overlap_duration: float = OVERLAP_SEC, progress=None) -> dict:
    """Transcribe a 16 kHz mono WAV (see the module notes).
    progress: optional callable(fraction 0..1), called after every step of
    every chunk: its decode (and any guard re-decode), its alignment, and
    its repair. Each chunk counts by its audio length."""
    note = lambda msg: print(msg, flush=True)  # noqa: E731
    global _unavailable
    if _unavailable:
        raise EngineUnavailable(_unavailable)
    plan = chunk_plan(_duration(wav_path), chunk_duration, overlap_duration)
    total = sum(e - s for s, e in plan) or 1.0
    try:
        worker = _Worker(wav_path)
    except EngineUnavailable as e:
        _unavailable = str(e)
        raise
    words, bye, counts = [], None, {}
    try:
        prev_end, before = None, 0.0
        for s, e in plan:
            def step(sub, before=before, length=e - s):
                if progress:
                    progress(min(1.0, (before + sub * length) / total))
            text = _decode(worker, s, e, note, step)
            cw = align_chunk(worker, s, e, text)
            step(DECODE_SHARE + (1.0 - DECODE_SHARE) * 0.6)
            cw = repair_chunk(worker, cw, s, e, counts)
            step(1.0)
            if prev_end is None:
                words = cw
            else:
                words = merge_chunk_words(words, cw, s, prev_end)
            prev_end, before = e, before + (e - s)
    finally:
        bye = worker.close()
    if counts.get("flagged"):
        note(f"   qwen3: word times repaired in {counts.get('repaired', 0)} of "
             f"{counts['flagged']} flagged stretches")
    if bye and bye.get("peak_gb"):
        note(f"   qwen3: child peak memory {bye['peak_gb']:.1f} GB")
    words = [w for w in words if str(w["word"]).strip()]
    prev = 0.0
    for w in words:
        if w["start"] < prev - 0.5:
            note(f"   warning: non-monotonic word timing near {prev:.1f}s "
                 "(possible chunk-seam artifact)")
            break
        prev = max(prev, w["start"])
    text = " ".join(w["word"] for w in words)
    return {"text": text, "words": words, "engine": asr_model()}

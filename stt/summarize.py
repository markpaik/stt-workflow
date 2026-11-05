"""LLM features: title/summary suggestions and per-meeting Q&A.

The DEFAULT backend is local and fully on-device: Qwen 3.8 (27B) served by
Ollama on this Mac, with Qwen3-8B (4-bit, MLX) as the fallback when Ollama is
not reachable (local_engine). Transcripts never leave the machine, which
matters because some recordings are sensitive. A cloud
assistant (Anthropic Claude or OpenAI) can be selected in Settings for machines
that can't run the local model or want faster answers; transcript text is then
uploaded to that provider for these features ONLY, and STRICT-mode recordings
always use the local model whatever the setting says (mirroring transcription's
strict rule). Used by the control panel's Rename flow (suggest a title from the
transcript, the user edits/approves, then rename_meeting() renames every
artifact consistently) and its Ask dialog (answer_question: grounded answers
about one meeting's transcript).
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config

MODEL = "mlx-community/Qwen3-8B-4bit"
# mlx-lm lives in its OWN venv (.venv-llm): its transformers pin conflicts with the
# audio stack's. We shell out rather than import.
LLM_PY = config.PROJECT_DIR / ".venv-llm" / "bin" / "python"

_RUNNER = r"""
import json, sys
from mlx_lm import load, generate
req = json.load(sys.stdin)
model, tokenizer = load(req["model"])
msgs = [{"role": "user", "content": req["prompt"]}]
text = tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False,
                                     enable_thinking=req.get("think", True))
out = generate(model, tokenizer, prompt=text, max_tokens=req["max_tokens"], verbose=False)
print(json.dumps({"text": out}))
"""


# --- assistant backend selection ------------------------------------------
LLM_BACKENDS = ("local", "anthropic", "openai")


def _setting(name: str, default: str = None) -> str:
    """stt.env wins over the process env (same precedence as asr_cloud keys),
    read fresh each call so a panel change applies without restarts."""
    return config._env_file().get(name) or os.environ.get(name) or default


def llm_backend() -> str:
    """Which assistant answers summaries/Ask: 'local' (default), 'anthropic',
    or 'openai'. The panel's Settings picker writes STT_LLM_BACKEND."""
    b = _setting("STT_LLM_BACKEND", "local")
    return b if b in LLM_BACKENDS else "local"


# --- the local engines -----------------------------------------------------
# "local" is two engines behind one name. Qwen 3.8 (27B) served by Ollama on
# this Mac is the default. Qwen3-8B through mlx-lm (.venv-llm) is the
# fallback when Ollama is not reachable. Both run on-device, so both keep
# the strict rule: Ollama is only ever called on a loopback address.
# STT_LOCAL_LLM picks: "auto" (the default: Ollama when it answers, else
# MLX), "ollama", or "mlx". STT_OLLAMA_URL="off" turns Ollama off.
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3.8:27b-mlx"
# Measured 2026-11-05 on this Mac (48 GB): prompt ~350-450 tokens/s, answer
# ~25 tokens/s, 3.7-4.2 transcript characters a token. The model's own
# context is 262k tokens. 64k bounds memory (~29 GB resident at 64k).
OLLAMA_NUM_CTX = 65536
OLLAMA_TIMEOUT = 1800
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_ollama_seen = {"at": 0.0, "up": False, "key": None}


def _ollama_url():
    """The Ollama base URL, or None when it is off or not a loopback
    address. A remote host would carry transcripts off this Mac, so it is
    refused, never used."""
    from urllib.parse import urlparse
    url = (_setting("STT_OLLAMA_URL", OLLAMA_URL) or "").strip().rstrip("/")
    if not url or url.lower() in ("off", "0", "none", "false"):
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return url if host in _LOOPBACK else None


def ollama_model() -> str:
    return _setting("STT_OLLAMA_MODEL", OLLAMA_MODEL)


def ollama_up(ttl: float = 10.0) -> bool:
    """Ollama answers on a loopback address and has the model pulled.
    Cached for `ttl` seconds: the panel asks on every poll."""
    url, model = _ollama_url(), ollama_model()
    if not url:
        return False
    key = (url, model)
    now = time.monotonic()
    if _ollama_seen["key"] == key and now - _ollama_seen["at"] < ttl:
        return _ollama_seen["up"]
    up = False
    try:
        import urllib.request
        with urllib.request.urlopen(url + "/api/tags", timeout=1.5) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = {m.get("name") for m in tags.get("models", [])} | {
            m.get("model") for m in tags.get("models", [])}
        up = model in names
    except Exception:
        up = False
    _ollama_seen.update(at=now, up=up, key=key)
    return up


def local_engine() -> str:
    """Which on-device engine a local request uses now: "ollama" or "mlx"."""
    pick = (_setting("STT_LOCAL_LLM", "auto") or "auto").strip().lower()
    if pick == "mlx":
        return "mlx"
    if pick == "ollama":
        return "ollama" if _ollama_url() else "mlx"
    return "ollama" if ollama_up() else "mlx"


def local_model_name(engine: str = None) -> str:
    engine = engine or local_engine()
    return ollama_model() if engine == "ollama" else MODEL


def backend_available(backend: str) -> bool:
    if backend == "local":
        return LLM_PY.exists() or ollama_up()
    if backend == "anthropic":
        return bool(_setting("STT_ANTHROPIC_KEY"))
    if backend == "openai":
        return bool(_setting("STT_OPENAI_KEY"))
    return False


def available() -> bool:
    return backend_available(llm_backend())


def _strict_meeting(base: str) -> bool:
    """Strict recordings must NEVER reach a cloud LLM. Unreadable metadata
    fails PRIVATE: treat it as strict rather than risk an upload."""
    try:
        return bool(json.loads(
            config.meeting_file(base, ".json").read_text()).get("strict"))
    except Exception:
        return True


def _backend_for(base: str) -> str:
    """The backend a given meeting may use: the selected one, except that a
    strict meeting downgrades to local (or refuses if local isn't installed)."""
    backend = llm_backend()
    if backend != "local" and _strict_meeting(base):
        if LLM_PY.exists() or ollama_up():
            return "local"
        raise RuntimeError(
            "this is a strict recording — its transcript never leaves this "
            "Mac, and the local model isn't installed. Install .venv-llm or "
            "switch the assistant back to the local model.")
    return backend


class LLMBusy(RuntimeError):
    """The local LLM is already loaded by another request or process."""


@contextlib.contextmanager
def _llm_lock(timeout: float | None = None):
    """One 8B model in RAM at a time, across processes — the panel's threads
    AND run_batch's auto_summarize (separate process, hence flock rather than
    a threading.Lock). timeout=None blocks until free (batch and /api/suggest
    keep their wait-your-turn semantics); timeout=N tries for N seconds and
    then raises LLMBusy, so an interactive question fails fast with a clear
    message instead of hanging a dialog behind a minutes-long batch pass."""
    with open(config.PROJECT_DIR / "llm.lock", "w") as fh:
        if timeout is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LLMBusy("the local model is busy with another request")
                    time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _generate(prompt: str, max_tokens: int = 2000, lock_timeout: float | None = None,
              backend: str = None, think: bool = True) -> str:
    """One prompt in, one answer out, whichever assistant is selected. Cloud
    backends need no llm.lock (nothing loads into RAM) and no busy path.
    think=False skips Qwen3's reasoning pass (measured ~4x faster). Use it for
    extraction-shaped work (summaries), where grounding held on real meetings;
    keep it on for Ask, where the no-think model gave up on a question the
    thinking model answered well. Cloud backends ignore the flag. Ollama
    gets it as its own think flag."""
    backend = backend or llm_backend()
    if backend == "anthropic":
        return _generate_anthropic(prompt, max_tokens)
    if backend == "openai":
        return _generate_openai(prompt, max_tokens)
    # "local" picks its engine now; "ollama" and "mlx" name one outright
    # (a caller that sized its prompt for one engine keeps it)
    engine = backend if backend in ("ollama", "mlx") else local_engine()
    if engine == "ollama":
        try:
            with _llm_lock(lock_timeout):
                return _generate_ollama(prompt, max_tokens, think)
        except OllamaDown:
            if backend == "ollama" or not LLM_PY.exists():
                raise RuntimeError("the local model (Ollama) is not reachable")
            _ollama_seen.update(at=time.monotonic(), up=False)
            # the fallback: Qwen3-8B, under the same lock semantics below

    import subprocess
    if not LLM_PY.exists():
        raise RuntimeError(".venv-llm missing — run: uv venv --python 3.12 .venv-llm && "
                           "uv pip install --python .venv-llm/bin/python mlx-lm 'transformers<5'")
    with _llm_lock(lock_timeout):
        # the MLX fallback runs at lowered priority too: background QoS
        # (taskpolicy -b) and nice (config.llm_nice)
        r = subprocess.run(["taskpolicy", "-b", "nice", "-n", str(config.llm_nice()),
                            str(LLM_PY), "-c", _RUNNER],
                           input=json.dumps({"model": MODEL, "prompt": prompt,
                                             "max_tokens": max_tokens, "think": think}),
                           capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"LLM runner failed: {r.stderr[-400:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])["text"]
    # Qwen3 emits <think>...</think> before the answer; keep only the answer
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL)
    return out.strip()


class OllamaDown(RuntimeError):
    """Ollama did not answer: the caller may fall back to MLX."""


# Ollama reasons apart from its answer: with think on, the reasoning
# trace counts against num_predict, so it gets its own budget.
OLLAMA_THINK_TOKENS = 4000


def renice_ollama() -> int:
    """Lower the Ollama server and runner processes to nice
    config.llm_nice() and background QoS before a call, so local LLM work yields the CPU and
    keeps the fan quiet. Best effort: a failure never stops the run.
    Returns how many processes were reniced."""
    import subprocess
    pids = set()
    for args in (["pgrep", "-x", "ollama"], ["pgrep", "-f", "ollama runner"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
            pids.update(int(x) for x in out.split() if x.strip().isdigit())
        except Exception:
            pass
    n = 0
    for pid in sorted(pids):
        ok = False
        # renice lowers the CPU priority; taskpolicy -b puts the process on
        # background QoS, which also lowers its GPU priority (the fan)
        for cmd in (["renice", "-n", str(config.llm_nice()), "-p", str(pid)],
                    ["taskpolicy", "-b", "-p", str(pid)]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                ok = ok or r.returncode == 0
            except Exception:
                pass
        n += ok
    return n


def _generate_ollama(prompt: str, max_tokens: int, think: bool = True) -> str:
    """One chat turn to Ollama on this Mac. The caller holds llm.lock: one
    large model works at a time, whichever engine serves it. A connection
    failure raises OllamaDown. A reply with an error raises RuntimeError.
    One request at a time: llm.lock serializes every caller, so nothing
    sends to Ollama in parallel."""
    import urllib.error
    import urllib.request
    url = _ollama_url()
    if not url:
        raise OllamaDown("Ollama is off")
    renice_ollama()
    body = {"model": ollama_model(), "stream": False, "think": bool(think),
            "messages": [{"role": "user", "content": prompt}],
            # long enough that a run over many meetings never reloads the
            # 18 GB model between them
            "keep_alive": _setting("STT_OLLAMA_KEEP_ALIVE", "30m"),
            # extraction wants a steady hand: low temperature, Qwen's
            # non-thinking top_p
            # the reasoning trace counts against num_predict: give it its own
            # budget so a long answer is never cut by the thinking before it
            # num_thread and num_gpu are never set: the server's defaults
            # are the ceiling (the owner's hardware rule, stt/config.py)
            "options": {"num_ctx": OLLAMA_NUM_CTX,
                        "num_predict": int(max_tokens) + (OLLAMA_THINK_TOKENS if think else 0),
                        "temperature": 0.3, "top_p": 0.8}}
    req = urllib.request.Request(url + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise RuntimeError(f"Ollama request failed ({e.code}): {detail[:200]}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise OllamaDown(str(e)) from e
    if d.get("error"):
        raise RuntimeError(f"Ollama request failed: {str(d['error'])[:200]}")
    out = (d.get("message") or {}).get("content") or ""
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL)
    return out.strip()


def _generate_anthropic(prompt: str, max_tokens: int) -> str:
    import anthropic
    key = _setting("STT_ANTHROPIC_KEY")
    if not key:
        raise RuntimeError("no Anthropic API key — add one under Cloud keys in Settings")
    # Haiku: these are brief summaries and single-transcript Q&A. No sampling
    # params and no thinking config — both portable across every current model
    # if STT_ANTHROPIC_MODEL points somewhere else (Opus rejects temperature).
    model = _setting("STT_ANTHROPIC_MODEL", "claude-haiku-4-5")
    try:
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude request failed: {e}") from e
    if r.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _generate_openai(prompt: str, max_tokens: int) -> str:
    import requests
    key = _setting("STT_OPENAI_KEY")
    if not key:
        raise RuntimeError("no OpenAI API key — add one under Cloud keys in Settings")
    model = _setting("STT_OPENAI_LLM_MODEL", "gpt-4o-mini")
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "max_completion_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI request failed ({r.status_code}): {r.text[:200]}")
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


# the .txt header line (output.txt_header): '<source file>  |  N min  |  N
# speaker(s) ...'. It leads with the SOURCE FILE NAME, which the model copied
# into titles, and a Voice Memos name can be a street address.
_TXT_HEADER = re.compile(r"^[^\n]*  \|  [\d.]+ min  \|  \d+ speaker\(s\)[^\n]*\n+")


def _model_text(base: str) -> str:
    """The meeting's .txt as the model may read it: the transcript lines,
    without the header line that names the source file."""
    txt = config.meeting_file(base, ".txt").read_text(encoding="utf-8")
    return _TXT_HEADER.sub("", txt, count=1)


def _transcript_sample(base: str, max_chars: int = 9000) -> tuple[str, bool]:
    """(text, truncated). Long meetings keep their head, middle, and tail with
    [...] where spans were dropped — the flag lets callers say so honestly.
    The header line with the source file name is never part of it."""
    txt = _model_text(base)
    if len(txt) <= max_chars:
        return txt, False
    third = max_chars // 3
    return (txt[:third] + "\n[...]\n" + txt[len(txt)//2 - third//2: len(txt)//2 + third//2]
            + "\n[...]\n" + txt[-third:]), True


QA_MAX_CHARS = 80_000   # ~20k tokens, so Qwen3-8B's 32k context still has room
                        # for history + a 2000-token answer. Most meetings fit
                        # whole, but multi-hour sessions do not (transcripts on
                        # disk reach ~234k chars, ~58k tokens): those go through
                        # _transcript_sample's head/middle/tail cut, and Ask
                        # tells the model and the user that parts were omitted
QA_MAX_HISTORY = 3      # last N exchanges sent back so follow-ups make sense


def answer_question(base: str, question: str, history: list | None = None) -> dict:
    """Answer one question about one meeting, from its transcript only.
    Ephemeral by design: this READS the .txt and writes nothing, so no
    lock_meeting is needed and nothing is stored. `history` is the last few
    {"q","a"} exchanges from the panel's Ask dialog (clipped here regardless
    of what the client sends); pass None for an independent question.
    Fully local, like everything else in this module."""
    question = (question or "").strip()[:2000]
    if not question:
        return {"ok": False, "error": "empty question"}
    if not config.meeting_file(base, ".txt").exists():
        return {"ok": False, "error": f"no transcript for '{base}'"}
    try:
        backend = _backend_for(base)  # strict meetings never reach a cloud LLM
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    sample, truncated = _transcript_sample(base, max_chars=QA_MAX_CHARS)
    hist = ""
    for h in (history or [])[-QA_MAX_HISTORY:]:
        q = str(h.get("q", ""))[:2000].strip()
        a = str(h.get("a", ""))[:4000].strip()
        if q and a:
            hist += f"Q: {q}\nA: {a}\n\n"
    prompt = (
        "You are answering questions about ONE meeting, using only its "
        "transcript below.\n"
        "Rules:\n"
        "- Answer ONLY from the transcript. If it does not contain the answer, "
        "say so plainly (e.g. \"The transcript doesn't cover that.\"). Never "
        "guess or use outside knowledge.\n"
        "- When you reference a specific moment, cite its timestamp in square "
        "brackets exactly as written in the transcript, e.g. [12:34].\n"
        "- Attribute statements to speakers by the names used in the transcript.\n"
        + ("- Parts of this long transcript were omitted where marked [...]. "
           "If the answer likely falls in an omitted part, say the excerpt "
           "may not cover it.\n" if truncated else "")
        + "- Be concise: a few sentences, or a short list if the question asks "
        "for several things.\n\n"
        f"TRANSCRIPT:\n{sample}\n\n"
        + (f"EARLIER QUESTIONS THIS SESSION (context only):\n{hist}" if hist else "")
        + f"QUESTION: {question}"
    )
    t0 = time.monotonic()
    answer = _generate(prompt, max_tokens=2000, lock_timeout=15.0, backend=backend)
    if not answer.strip():
        # Ollama returns its reasoning apart from the answer: a reply that
        # spent every token reasoning comes back empty
        return {"ok": False, "error": "the model ran out of room reasoning "
                                      "about this one; try a more specific question"}
    if "<think>" in answer:
        # generation stopped mid-reasoning: the closing tag never arrived, so
        # _generate's strip didn't match and raw chain-of-thought would leak
        answer = answer.split("</think>")[-1].strip()
        if not answer or "<think>" in answer:
            return {"ok": False, "error": "the model ran out of room reasoning "
                                          "about this one; try a more specific question"}
    return {"ok": True, "answer": answer, "truncated": truncated,
            "elapsed_sec": round(time.monotonic() - t0, 1)}


def _date_suffix(base: str) -> str:
    m = re.search(r"(\d{8})\s*$", base)
    if m:
        return m.group(1)
    j = config.meeting_file(base, ".json")
    try:
        ts = json.loads(j.read_text()).get("generated_at", "")
        return datetime.fromisoformat(ts).strftime("%m%d%Y")
    except Exception:
        return datetime.now().strftime("%m%d%Y")


NO_SPEECH_TEXT = "No speech found in this recording."


def has_speech(base: str) -> bool:
    """False when the meeting's transcript holds 0 segments, or says
    no_speech. A model given an empty transcript writes a plausible summary
    of nothing, which is worse than no summary. Unreadable json reads as
    speech: the guard must never hide a real transcript."""
    try:
        d = json.loads(config.meeting_file(base, ".json").read_text())
    except (OSError, ValueError):
        return True
    if d.get("no_speech"):
        return False
    return bool(d.get("segments"))


# Whole-transcript summaries (unified plan Phase 4G). The summary used to
# read a 12,000-character head/middle/tail sample, so a decision near the end
# of a long meeting never reached it. Now the model reads every line:
#   - a transcript up to SUMMARY_WHOLE_CHARS goes in one prompt, whole;
#   - a longer one is cut at line breaks into parts of SUMMARY_PART_CHARS,
#     the model writes notes on each part (topics, decisions, numbers, and
#     every commitment in the next-steps line format), and the final prompt
#     reads all the notes in order. Notes longer than the one-prompt limit
#     are cut and noted again, at most SUMMARY_MAX_LEVELS times.
# The limits keep each local prompt plus its answer inside Qwen3-8B's 32k
# context at the module's ~4 characters a token. A cloud assistant takes up
# to SUMMARY_CLOUD_WHOLE_CHARS in one prompt. The output format, and so the
# ai_next_steps lines chief-of-staff reads, is unchanged.
SUMMARY_WHOLE_CHARS = 80_000          # ~20k tokens + a 3000-token answer
SUMMARY_PART_CHARS = 60_000           # ~15k tokens + a 1500-token note
SUMMARY_NOTE_TOKENS = 1500
SUMMARY_CLOUD_WHOLE_CHARS = 400_000   # ~100k tokens, inside every cloud model
# Qwen 3.8 on Ollama, sized from a measured run (2026-11-05): with num_ctx
# 65536 the server loads a 65536-token context (api/ps), and a 230,000-char
# transcript (61,762 prompt tokens, ~3.7 characters a token) kept a note
# placed at its start AND one at its end. The whole-transcript limit leaves
# room for the ~2,000-token fixed prompt and the 3,000-token answer:
# (65,536 - 5,500) tokens x 3.5 characters, rounded down. Only the two
# longest meetings in the library go through part notes.
SUMMARY_OLLAMA_WHOLE_CHARS = 210_000
SUMMARY_OLLAMA_PART_CHARS = 150_000
SUMMARY_MAX_LEVELS = 3


def _parts(text: str, size: int) -> list:
    """`text` cut into pieces of at most `size` characters, at line breaks
    where a line allows it."""
    out, cur, n = [], [], 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:               # one enormous line: hard cut
            if cur:
                out.append("".join(cur))
                cur, n = [], 0
            out.append(line[:size])
            line = line[size:]
        if n + len(line) > size and cur:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line)
    if cur:
        out.append("".join(cur))
    return [p for p in out if p.strip()]


def _part_prompt(i: int, n: int, text: str, notes: bool) -> str:
    what = "notes on one long meeting" if notes else "one meeting transcript"
    return (
        f"Below is part {i} of {n} of {what}, in order. Write notes on THIS "
        "part only, for someone who will summarize the whole meeting later. "
        "Start with a line 'DECISIONS:' and list every decision made in this "
        "part (or 'none'), then:\n"
        "- the topics discussed and what was said about each;\n"
        "- every open question;\n"
        "- names, numbers, dates, and places exactly as they appear;\n"
        "- every concrete commitment, one per line, formatted exactly as:  "
        "- [<speaker name as labeled>] will <specific action> by <stated "
        "deadline, or 'no date given'>\n"
        "Plain lines, no preamble. Do not invent anything.\n\n"
        f"PART {i} OF {n}:\n{text}"
    )


def _summary_source(base: str, backend: str) -> tuple:
    """(heading, body, parts) for the final summary prompt: the whole
    transcript when it fits, else the notes on every part. `parts` is 1 for
    a whole transcript and the number of transcript parts otherwise. The
    header line with the source file name is left out: the model copied
    its words into the title."""
    txt = _model_text(base)
    if backend == "ollama":
        whole, part_size = SUMMARY_OLLAMA_WHOLE_CHARS, SUMMARY_OLLAMA_PART_CHARS
    elif backend in ("local", "mlx"):
        whole, part_size = SUMMARY_WHOLE_CHARS, SUMMARY_PART_CHARS
    else:
        whole, part_size = SUMMARY_CLOUD_WHOLE_CHARS, SUMMARY_PART_CHARS
    if len(txt) <= whole:
        return "Below is a whole meeting transcript.", f"TRANSCRIPT:\n{txt}", 1
    body, notes, parts = txt, False, 0
    for _ in range(SUMMARY_MAX_LEVELS):
        chunks = _parts(body, part_size)
        parts = parts or len(chunks)
        got = []
        for i, c in enumerate(chunks, 1):
            got.append(f"PART {i} OF {len(chunks)}:\n" + _generate(
                _part_prompt(i, len(chunks), c, notes),
                max_tokens=SUMMARY_NOTE_TOKENS, backend=backend, think=False).strip())
        body, notes = "\n\n".join(got), True
        if len(body) <= whole:
            break
    body = body[:whole]
    return ("Below are notes on every part of one long meeting, in order, taken "
            "from its whole transcript. Treat them as the meeting. The summary "
            "must state the main decisions the notes list, wherever in the "
            "meeting they came.",
            f"NOTES:\n{body}", parts)


# --- the description (gist, sections, tags) and the catalog title ---------
# One model call per meeting gives everything: the series and topic the
# title is built from (stt/series.py), a gist for the list row, the
# Decisions / Open questions / Next steps sections, and 3-5 topic tags.
#
# Stored fields. The new ones: ai_gist, ai_decisions, ai_open_questions,
# ai_tags, ai_series, ai_topic, ai_person, ai_tmpl (the speaker template,
# stt/speaker_text.py), ai_model. Kept for every existing reader:
# ai_title (the catalog title without its date), ai_next_steps (the same
# "[name] will ... by ..." lines chief-of-staff parses), and ai_summary,
# which is now the gist plus the Decisions and Open questions lists as
# plain text (speaker_text.compose_summary). ai_stale marks a description
# a Redo left out of date.
GIST_MAX_WORDS = 20
DESCRIPTION_KEYS = ("ai_title", "ai_summary", "ai_next_steps", "ai_gist",
                    "ai_decisions", "ai_open_questions", "ai_tags", "ai_series",
                    "ai_topic", "ai_person", "ai_tmpl", "ai_model",
                    "ai_summary_parts", "ai_generated_at")
_NONE_ITEMS = {"none", "none stated", "none stated.", "none.", "n/a", "no date given"}
_GIST_OPENERS = re.compile(
    r"^(the|this|a|an|our|in|during|at)\s+(meeting|call|session|discussion|"
    r"check[\s-]?in|conversation|dialogue|workshop|team|group|board)\b", re.I)
_GIST_DATE = re.compile(
    r"^(on\s+)?(\d|(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d"
    r"|(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|yesterday)\b)",
    re.I)


def _owner() -> str:
    return config.mic_speaker() or ""


def _known_people() -> list:
    """Every enrolled person's name (the voiceprint registry's keys)."""
    try:
        from . import identify
        return sorted(str(k) for k in identify.load_registry())
    except Exception:
        return []


def _named_roster(d: dict) -> list:
    """The meeting's named speakers, most talk first, each once."""
    from . import titles
    return titles._talk_by_name(d)


def description_prompt(heading: str, source: str, old_title: str = "",
                       people=(), owner: str = "", known_series: str = None) -> str:
    """The one prompt per meeting. Everything that is the same for every
    meeting comes first (the rules, the series catalog, the tags, the
    format), and the transcript comes last, so Ollama reuses the cached
    prefix from one meeting to the next. Only the short block before the
    transcript and the transcript itself differ."""
    from . import series
    who = ", ".join(people) if people else "none named"
    fixed = (
        "You describe one recorded meeting. "
        f"The owner of these recordings is {owner or 'unknown'}.\n"
        "Produce:\n"
        "1. SERIES: which meeting series this recording belongs to, as one id "
        "from this catalog. Use the current title and the content. Use "
        f"{series.ONE_OFF} when none fits. When the meeting block below says "
        "the series is known, write that id.\n"
        f"{series.catalog_text()}\n"
        "2. PERSON: for a series whose name has <Person>, the full name of the "
        "other participant as labeled in the transcript (never the owner). "
        "Two people: 'A and B'. Otherwise: none\n"
        "3. TOPIC: the main subject in 2-5 words, Title Case, no dates, no "
        "people's names, no meeting-type words such as Weekly, Check-in, "
        "Meeting, Call. For a one-off, 3-7 words that may name an outside "
        "organization when it is central.\n"
        f"4. GIST: one sentence of at most {GIST_MAX_WORDS} words that a person "
        "scanning a list reads first. Lead with the topic or the main decision "
        "itself. Never open with 'The meeting', 'This meeting', a date, a "
        "person's name, or the meeting type.\n"
        "5. TAGS: 3 to 5 tags, comma-separated, ONLY from this list:\n"
        f"{series.tags_text()}\n"
        "6. DECISIONS: the decisions actually made, most important first, at "
        "most 6, one per line, short and specific. If none, write exactly:  "
        "- none\n"
        "7. OPEN QUESTIONS: the questions left unresolved, at most 4, one per "
        "line. If none, write exactly:  - none\n"
        "8. NEXT STEPS: every concrete commitment someone made, one per line, "
        "formatted exactly as:  - [<speaker name as labeled in the transcript>] "
        "will <specific action> by <stated deadline, or 'no date given'>\n"
        "Only include commitments actually stated; if there are none, write "
        "exactly:  - none\n"
        "Do not invent anything. Answer in exactly this format:\n"
        "SERIES: <id>\nPERSON: <name or none>\nTOPIC: <topic>\nGIST: <gist>\n"
        "TAGS: <tag>, <tag>, <tag>\nDECISIONS:\n- ...\nOPEN QUESTIONS:\n- ...\n"
        "NEXT STEPS:\n- ...\n\n")
    meeting = (
        "THE MEETING\n"
        "The recording's current title (it may be a default name, messy, or "
        f"wrong): {old_title or 'none'}\n"
        + (f"The series is known: {known_series}\n" if known_series else "")
        + f"Named speakers: {who}\n"
        f"{heading}\n\n")
    return fixed + meeting + source


def parse_description(out: str) -> dict:
    """The model's reply as fields. Tolerates a missing section, bullets
    of any kind, and an older reply in the TITLE/SUMMARY format (its
    summary becomes the gist source)."""
    f = {"series": "", "person": "", "topic": "", "gist": "", "tags": "",
         "decisions": [], "open_questions": [], "next_steps": [],
         "title": "", "summary": []}
    heads = {"SERIES": "series", "PERSON": "person", "TOPIC": "topic",
             "GIST": "gist", "TAGS": "tags", "TITLE": "title"}
    lists = {"DECISIONS": "decisions", "OPEN QUESTIONS": "open_questions",
             "NEXT STEPS": "next_steps"}
    mode = None
    for line in (out or "").splitlines():
        raw = line.strip()
        u = re.sub(r"^[#*\s]+|[*]+", "", raw).upper()
        hit = False
        for h, k in lists.items():
            if u.startswith(h):
                mode, hit = k, True
                rest = raw.split(":", 1)[1].strip() if ":" in raw else ""
                if rest and rest.lower() not in _NONE_ITEMS and rest != "-":
                    f[k].append(rest.lstrip("-•* ").strip())
                break
        if hit:
            continue
        for h, k in heads.items():
            if u.startswith(h + ":"):
                f[k] = raw.split(":", 1)[1].strip().strip("*").strip()
                mode, hit = (k if k == "gist" else None), True
                break
        if hit:
            continue
        if u.startswith("SUMMARY:"):
            f["summary"].append(raw.split(":", 1)[1].strip())
            mode = "summary"
            continue
        if not raw:
            continue
        if mode in lists.values() and re.match(r"^([-•*]|\d+[.)])\s*", raw):
            item = re.sub(r"^([-•*]|\d+[.)])\s*", "", raw).strip()
            if item and item.lower() not in _NONE_ITEMS:
                f[mode].append(item)
        elif mode == "summary":
            f["summary"].append(raw)
        elif mode == "gist" and not f["gist"]:
            f["gist"] = raw
    f["summary"] = " ".join(x for x in f["summary"] if x)
    return f


def normalize_step(line: str) -> str:
    """A next-steps line in the one format chief-of-staff parses: a line
    the model left without its 'by <deadline>' gets 'by no date given'."""
    s = re.sub(r"\s+", " ", str(line or "")).strip().rstrip(".")
    # a stray "no date given" inside the action ("..., no date given by no
    # date given", "... (no date given) by ...") is noise: one ending says it
    s = re.sub(r"\s*[,(]\s*no date given\)?(?=\s+by\b|$)", "", s, flags=re.I)
    if s.startswith("[") and " will " in s and not re.search(r"\bby\b", s.split(" will ", 1)[1]):
        s += " by no date given"
    return s


def clip_gist(gist: str, limit: int = GIST_MAX_WORDS) -> str:
    """At most `limit` words, ending on a full stop."""
    words = re.sub(r"\s+", " ", str(gist or "")).strip().split()
    if len(words) > limit:
        words = words[:limit]
        words[-1] = words[-1].rstrip(",;:-")
    g = " ".join(words)
    if g and g[-1] not in ".!?":
        g += "."
    return g


def gist_problems(gist: str, people=(), series_name: str = "") -> list:
    """What breaks the gist rule: too long, or it opens with 'The meeting',
    a date, a person, or the meeting type."""
    g = str(gist or "").strip()
    out = []
    if not g:
        return ["empty"]
    if len(g.split()) > GIST_MAX_WORDS:
        out.append("long")
    if _GIST_OPENERS.match(g):
        out.append("opens with the meeting")
    if _GIST_DATE.match(g):
        out.append("opens with a date")
    first = g.split()[0].strip(",.'’s")
    if any(first and first == (p.split() or [""])[0] for p in people):
        out.append("opens with a person")
    if series_name and g.lower().startswith(series_name.lower()[:12]):
        out.append("opens with the meeting type")
    return out


# think on or off for the description call, chosen by an A/B on four
# meetings of 8-27 minutes (2026-11-05). Think on found 20 next steps where
# think off found 12, cleared one gist-rule miss, and kept every series;
# wall time was 544 s against 479 s. Part notes stay off: they only collect.
DESCRIPTION_THINK = True


def describe(base: str, old_title: str = None, backend: str = None,
             think: bool = None) -> dict:
    """Run the model over one meeting and return its description, title
    parts and speaker template. Writes nothing (the retitle dry run uses
    this). Raises on a model failure. `backend` defaults to the meeting's
    allowed backend, so a strict meeting never reaches a cloud LLM."""
    from . import dates, series, speaker_text
    backend = backend or _backend_for(base)
    engine = local_engine() if backend == "local" else backend
    j = config.meeting_file(base, ".json")
    snap = json.loads(j.read_text())         # the roster the model will read
    raw_turns = speaker_text.load_raw_turns(base)
    heading, source, parts = _summary_source(base, engine)
    owner = _owner()
    people = _named_roster(snap)
    old_title = dates.title_of(base) if old_title is None else old_title
    # a title that IS a catalog series name: code makes the series choice
    exact_sid, exact_person = series.exact_series(old_title)
    prompt = description_prompt(heading, source, old_title, people, owner,
                                known_series=exact_sid)
    t0 = time.monotonic()
    think = DESCRIPTION_THINK if think is None else bool(think)
    out = _generate(prompt, max_tokens=3000, backend=engine, think=think)
    f = parse_description(out)
    # a reply in the older TITLE/SUMMARY shape (a cloud model that kept to
    # it) keeps its prose summary and free title: there is no series to
    # build from, and cutting the prose to a gist would lose most of it
    legacy = not f["series"] and not f["gist"] and bool(f["summary"] or f["title"])
    sid = None if legacy else (exact_sid or series.normalize_series(f["series"]))
    if exact_sid and exact_person and (not f["person"] or f["person"].lower() == "none"):
        f["person"] = exact_person
    known = _known_people()
    person_full = ""
    if sid in series.BY_ID and series.BY_ID[sid].has_person:
        person_full = series.resolve_person(f["person"], roster=people,
                                            known=known, me=owner)
    person = series.label_people(person_full, known + people) if person_full else ""
    topic_src = f["topic"] or f["title"]
    if legacy:
        title = re.sub(r'[<>:"/\\|?*]', "", f["title"]).strip() or "Untitled Meeting"
        gist = ""
        fields = {"summary": f["summary"], "next_steps": f["next_steps"]}
    else:
        title = series.build_title(sid, topic_src, person)
        gist = clip_gist(f["gist"] or f["summary"].split(". ")[0])
        fields = {"gist": gist, "decisions": f["decisions"],
                  "open_questions": f["open_questions"], "next_steps": f["next_steps"]}
    if fields.get("next_steps"):
        fields["next_steps"] = [normalize_step(x) for x in fields["next_steps"]]
    tags = series.normalize_tags(f["tags"])
    tmpl = speaker_text.make(fields, snap, raw_turns)
    rendered = speaker_text.render(tmpl, snap)
    series_name = (series.BY_ID[sid].name(person) if sid in series.BY_ID else "")
    return {
        "series": sid, "series_raw": f["series"], "series_by_code": bool(exact_sid),
        "person": person,
        "person_raw": f["person"], "person_full": person_full,
        "topic": series.split_title(title)[1] if sid in series.BY_ID else title,
        "topic_raw": topic_src, "title": title,
        "gist": rendered.get("ai_gist", gist), "gist_problems": [] if legacy else
            gist_problems(gist, people + ([owner] if owner else []), series_name),
        "tags": tags, "tags_raw": f["tags"],
        "decisions": rendered.get("ai_decisions", []),
        "open_questions": rendered.get("ai_open_questions", []),
        "next_steps": rendered.get("ai_next_steps", []),
        "summary": rendered.get("ai_summary", gist or f["summary"]),
        "tmpl": tmpl, "parts": parts, "engine": engine,
        "model": local_model_name(engine) if engine in ("ollama", "mlx") else engine,
        "elapsed_sec": round(time.monotonic() - t0, 1), "raw": out,
    }


def store_description(base: str, desc: dict, title: str = None) -> dict:
    """Write a description from describe() into the meeting json, under
    the meeting's lock with a fresh read, re-rendered against the CURRENT
    roster (the speakers may have changed while the model ran). `title`
    overrides ai_title. Clears ai_stale. Returns the rendered fields."""
    from . import review, speaker_text
    j = config.meeting_file(base, ".json")
    with review.lock_meeting(base):
        d = json.loads(j.read_text())
        raw_turns = speaker_text.load_raw_turns(base)
        tmpl = desc["tmpl"]
        labels = (speaker_text.labels_for(d, raw_turns) if raw_turns
                  else dict(tmpl.get("labels") or {}))
        for k, v in (tmpl.get("labels") or {}).items():
            labels.setdefault(k, v)
        rendered = speaker_text.render(tmpl, d, labels)
        d.pop("ai_stale", None)
        if "gist" not in (tmpl.get("fields") or {}):
            # an older-shaped reply: no gist or sections, so none may linger
            for k in ("ai_gist", "ai_decisions", "ai_open_questions"):
                d.pop(k, None)
        d["ai_tmpl"] = {**tmpl, "labels": {k: labels.get(k, v) for k, v
                                           in (tmpl.get("labels") or {}).items()}}
        d.update(rendered)
        d["ai_title"] = title if title is not None else desc["title"]
        d["ai_tags"] = list(desc.get("tags") or [])
        if desc.get("series"):
            d["ai_series"] = desc["series"]
        else:
            d.pop("ai_series", None)
        d["ai_topic"] = desc.get("topic")
        d["ai_person"] = desc.get("person") or None
        d["ai_model"] = desc.get("model")
        d["ai_summary_parts"] = desc.get("parts", 1)
        d["ai_generated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(j, d)
    return rendered


def suggest_title(base: str) -> dict:
    """Write a meeting's description from its WHOLE transcript (see
    SUMMARY_WHOLE_CHARS) and draft its catalog title. The result is
    persisted into the meeting's .json (see DESCRIPTION_KEYS) so it stays
    visible in the panel afterwards. It never renames the folder: the
    title is only a suggestion here (apply_auto_title uses it for a
    meeting that arrived with no typed title).

    A transcript with 0 segments gets no call to the model at all: the result
    is an error that says no speech was found, and nothing is written."""
    from . import dates
    if not has_speech(base):
        return {"ok": False, "no_speech": True, "error": NO_SPEECH_TEXT}
    desc = describe(base)
    rendered = {}
    try:
        rendered = store_description(base, desc)
    except Exception:
        pass
    title = desc["title"]
    return {"title": title,
            "summary": rendered.get("ai_summary", desc["summary"]),
            "gist": rendered.get("ai_gist", desc["gist"]),
            "decisions": rendered.get("ai_decisions", desc["decisions"]),
            "open_questions": rendered.get("ai_open_questions", desc["open_questions"]),
            "next_steps": rendered.get("ai_next_steps", desc["next_steps"]),
            "tags": desc["tags"], "series": desc["series"],
            "suggested_name": f"{title} {_date_suffix(base)}"
                              if dates.strip_stamp(title) == title else title,
            "parts": desc["parts"]}


def mark_stale(d: dict) -> bool:
    """Mark a meeting json's description out of date (a Redo re-clustered
    the voices). The text stays readable. The speaker template goes: its
    cluster ids name voices the Redo replaced. Returns True if marked."""
    if not (d.get("ai_summary") or d.get("ai_gist")):
        return False
    d["ai_stale"] = True
    d.pop("ai_tmpl", None)
    return True


def stale_bases() -> list:
    """Live meetings whose description a Redo left out of date, shortest
    transcript first (the batch regenerates them when idle)."""
    out = []
    for b in config.meeting_bases():
        j = config.meeting_file(b, ".json")
        try:
            # a bounded read, as config.id_of does: the pipeline writes the
            # flag among the metadata, ahead of the multi-megabyte segments
            # and words, so a library scan never parses whole transcripts
            with open(j, "rb") as fh:
                head = fh.read(STALE_HEAD_BYTES)
            if not re.search(rb'"ai_stale":\s*true', head):
                continue
            if not json.loads(j.read_text()).get("ai_stale"):
                continue
            size = config.meeting_file(b, ".txt").stat().st_size
        except (OSError, ValueError):
            continue
        out.append((size, b))
    return [b for _, b in sorted(out)]


STALE_HEAD_BYTES = 262_144


def _unique_base(candidate: str, current: str) -> str:
    """A folder name not already taken by ANOTHER meeting — live OR archived. A
    genuine same-name same-date duplicate (recurring meeting recorded twice in
    one day) gets a ' (2)' suffix rather than being refused. Archived names
    count as taken so a base stays unique across the whole store: the speaker
    registries reference meetings by name, and a live meeting reusing an
    archived one's name would make a later restore ambiguous."""
    def taken(n):
        return config.meeting_dir(n).exists() or (config.archive_dir() / n).exists()

    # case-insensitive self-match: APFS is case-insensitive by default, so a
    # capitalization fix ('board prep' -> 'Board Prep') sees its own folder via
    # Path.exists() — an exact-string compare here appended a spurious ' (2)'
    # to every case-only retitle
    if candidate.lower() == current.lower() or not taken(candidate):
        return candidate
    i = 2
    while taken(f"{candidate} ({i})"):
        i += 1
    return f"{candidate} ({i})"




CATEGORIES = ("work", "personal")


def _sanitize_name(s: str) -> str:
    """A safe folder/file name (mirrors recorder.final_name): drop path/wildcard/
    control chars, then LEADING DOTS — a bare '.' or '..' would otherwise survive
    the class strip and resolve the meeting folder to the parent dir or a hidden
    name. Collapse whitespace runs and cap the length."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s)).strip().lstrip(".")
    return re.sub(r"\s+", " ", s)[:120].strip()


def _write_json(j: Path, d: dict):
    tmp = j.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    os.replace(tmp, j)


def _is_active(base: str) -> bool:
    from . import status as _status
    return _status.meeting_active(base)


def _move_folder(base: str, new_base: str) -> list:
    """Rename the meeting folder and every file inside it, then follow the move
    everywhere the old name is referenced: the speaker registries (which key
    meetings BY NAME, so a stale ref is a dead ▶ play button), the stored
    source_file, and the manifest's output paths — without that last one, a
    source still sitting in a watched folder reads as unprocessed and the next
    run silently re-transcribes the meeting into a duplicate.
    The caller holds lock_meeting(base)."""
    from . import dupes, identify, manifest, unknowns
    old_dir, new_dir = config.meeting_dir(base), config.meeting_dir(new_base)
    renamed = []
    prefix = base + "."
    for f in sorted(old_dir.iterdir()):
        if f.is_file() and f.name.startswith(prefix):
            dst = old_dir / (new_base + f.name[len(base):])
            f.rename(dst)
            renamed.append(dst.name)
    old_dir.rename(new_dir)
    try:
        unknowns.rename_meeting_refs(base, new_base)
        identify.rename_source_refs(base, new_base)
        # the "keep both" list keys pairs by base name too: without this, a
        # title edit or a date correction re-offers a pair the user already
        # reviewed, as a fresh 100 percent match
        dupes.rename_pair_refs(base, new_base)
    except Exception as e:
        print(f"   rename: registry references not updated ({e})", file=sys.stderr)
    manifest.retarget(old_dir, new_dir, base, new_base)
    j = config.meeting_file(new_base, ".json")
    if j.exists():
        try:
            d = json.loads(j.read_text())
            d["source_file"] = new_base + (Path(d.get("source_file", "")).suffix or ".m4a")
            d["renamed_from"] = base
            _write_json(j, d)
        except (OSError, ValueError):
            pass
    # the one reference that is NOT best-effort chasing: the id index. The
    # chase above follows the NAME into each store; this records where the
    # meeting actually went, so anything holding the id (a queued relabel, a
    # registry source, a lock) resolves to the new folder even when a chase
    # above failed. Last, because it must describe the move that happened.
    config.index_set(config.id_of(new_base), new_base)
    # the search index keys lines by folder name: move them now, so a hit
    # never opens the old name. A miss here is repaired by the next query.
    from . import search
    search.touch(base, new_base)
    return renamed


_UNSET = object()
# pass as recorded_at= to remove the key (an undo back to a meeting that
# never had one)
DROP = "$drop"


def apply_meeting_edits(base: str, *, title=None, date=None, category=None,
                        reviewed=None, title_state=None, date_source=None,
                        recorded_at=_UNSET, auto_title=False, retitle=False) -> dict:
    """The ONE place a meeting's human-owned fields change: title, date,
    Work/Personal category, whether it still needs review, and the title's
    state.

    Every title or date edit also keeps the `title` key equal to the folder
    name it lands on, without the date stamp or twin suffix.

    A date edit records where the date came from. `date_source` defaults to
    `manual`, because the panel is a person. A repair passes the real
    source (`recorder`, `short_typed`, ...). A date equal to the stored one
    leaves `date_source` alone unless the caller passes it explicitly. `recorded_at` sets the true
    start time with that date, None stores null, and DROP removes the key.
    Left out, a stored `recorded_at` on another day is cleared to null,
    because `date` must equal its date part whenever it is set.

    A title edit is a person's choice, so it also sets title_state=confirmed
    and title_source=manual. `auto_title=True` marks the title as the
    pipeline's own (titles.AUTO_TITLE_STATE and AUTO_TITLE_SOURCE) instead. `title_state` sets the state alone (a Keep on one
    meeting or a bulk Keep). Neither touches `reviewed`: only the caller's
    own `reviewed` argument does, so a bulk Keep never claims a person looked.
    `retitle=True` is the catalog retitle (tools/retitle_all.py): the title
    is the model's, so title_source becomes `model`, and a needs_title state
    becomes provisional. Any other state is kept as it was, and a meeting
    from before the title rule keeps no state at all.

    The invariant it enforces: the folder is named "<title> MMDDYYYY". That is
    what keeps recurring meetings ('LT Weekly Meeting') unique on disk, and it is
    the whole reason the date lives in the name. So correcting a date RE-STAMPS
    the folder instead of letting the name and the stored date drift apart — they
    used to, because set_meeting_date only ever rewrote the json.

    Only a title or date edit moves the folder. Tagging a category (or accepting
    from the inbox without renaming) must not silently rename anything.
    Returns {ok, base, renamed, date, category, reviewed}.
    """
    from . import dates, review, titles

    if base not in config.meeting_bases():
        return {"ok": False, "error": f"no meeting '{base}'"}
    if _is_active(base):
        return {"ok": False,
                "error": "this meeting is being processed right now — try again in a moment"}

    iso = None
    if date is not None:
        from datetime import date as _date
        try:
            iso = _date.fromisoformat(str(date).strip()).isoformat()
        except ValueError:
            return {"ok": False, "error": "date should look like 2026-07-04"}

    cat = None
    if category is not None:
        cat = str(category or "").strip().lower()
        if cat and cat not in CATEGORIES:
            return {"ok": False, "error": "category must be work, personal, or empty"}

    if title_state is not None and title_state not in titles.TITLE_STATES:
        return {"ok": False, "error": f"unknown title state '{title_state}'"}

    from . import provenance
    if date_source is not None and date_source not in provenance.DATE_SOURCES:
        return {"ok": False, "error": f"unknown date source '{date_source}'"}
    if recorded_at is not _UNSET and recorded_at not in (None, DROP):
        try:
            _ra = datetime.fromisoformat(str(recorded_at))
        except ValueError:
            return {"ok": False, "error": "recorded_at should be an ISO time"}
        if iso is not None and _ra.date().isoformat() != iso:
            return {"ok": False, "error": "recorded_at must fall on the meeting date"}

    new_title = None
    if title is not None:
        new_title = _sanitize_name(title)
        if not new_title:
            return {"ok": False, "error": "empty name"}

    j = config.meeting_file(base, ".json")

    def _apply(d) -> str:
        """Fold the requested edits into `d` and return the folder name they
        imply. Writes nothing, so it can be re-run under a wider set of locks
        without repeating a side effect."""
        date_before = d.get("date")
        if iso is not None:
            d["date"] = iso
        if category is not None:
            if cat:
                d["category"] = cat
            else:
                d.pop("category", None)
        if reviewed is not None:
            d["reviewed"] = bool(reviewed)
        if title is not None and retitle:
            if d.get("title_state"):
                d["title_source"] = titles.AUTO_TITLE_SOURCE
                if d["title_state"] == "needs_title":
                    d["title_state"] = titles.AUTO_TITLE_STATE
        elif title is not None and auto_title:
            d["title_state"] = titles.AUTO_TITLE_STATE
            d["title_source"] = titles.AUTO_TITLE_SOURCE
        elif title is not None:
            d["title_state"], d["title_source"] = "confirmed", "manual"
        elif title_state is not None:
            d["title_state"] = title_state

        want = base
        if title is not None or date is not None:
            # a typed date counts only as a TRAILING stamp ('Review 06152026').
            # meeting_date() over the whole title also matched mid-name digit
            # runs — accepting the recorder default 'Recording 07112026 1032'
            # with a corrected date silently threw the correction away.
            trailing = (new_title is not None
                        and dates.strip_stamp(new_title) != new_title)
            if trailing:
                # the name is taken exactly as typed. It also becomes the stored
                # date — UNLESS the date picker was set too (an explicit date
                # always wins), and never a FUTURE date (a name like 'Planning
                # Retreat 12312026' describes an event, not when it was recorded)
                typed = dates.meeting_date(new_title)
                from datetime import date as _today
                if iso is None and typed and typed <= _today.today().isoformat():
                    d["date"] = typed
                want = new_title
            else:
                stem = new_title if new_title is not None else base
                on = d.get("date") or ""
                # restamp: replaces an existing stamp (peeling any ' (N)' twin
                # suffix first) rather than appending a second date
                want = dates.restamp(stem, on) if on else stem
            want = _unique_base(want, base)
            d["title"] = dates.title_of(want)
        moved = d.get("date") != date_before
        if moved or (date is not None and date_source is not None):
            # the date changed (the picker, a repair, or a typed stamp in a
            # title), or a repair names its source: record where it came
            # from. Passing the date a meeting ALREADY has (an Accept with
            # the prefilled date, a restore, a restamp) is no evidence of
            # anything, so the stored source stays as it was.
            d["date_source"] = date_source or "manual"
        if moved:
            if recorded_at is _UNSET:
                ra = d.get("recorded_at")
                if ra and str(ra)[:10] != d.get("date"):
                    d["recorded_at"] = None
        if recorded_at is not _UNSET and recorded_at == DROP:
            d.pop("recorded_at", None)
        elif recorded_at is not _UNSET:
            d["recorded_at"] = recorded_at
        return want

    def _dest_taken(want: str) -> bool:
        # mirrors _unique_base's case-insensitive self-match: on APFS a
        # capitalization fix sees its OWN folder through Path.exists()
        if want.lower() == base.lower():
            return False
        return (config.meeting_dir(want).exists()
                or (config.archive_dir() / want).exists())

    # A move needs the DESTINATION locked as well as the source. Two meetings
    # restamping onto one name each saw it free, both started moving, and the
    # loser's folder rename raised OSError with its data stranded under the
    # winner's filenames -- the meeting then vanished from every list.
    # Phase one reads and plans under the source's own lock. Phase two takes
    # source and destination in NAME order (one fixed order can never deadlock)
    # and re-checks the destination is still free while holding both.
    for _ in range(8):
        with review.lock_meeting(base):
            try:
                d = json.loads(j.read_text())
            except (OSError, ValueError):
                return {"ok": False, "error": f"no transcript for '{base}'"}
            want = _apply(d)
            if want == base:
                _write_json(j, d)          # no move: the source lock is enough
                return {"ok": True, "base": base, "renamed": [],
                        "date": d.get("date"), "category": d.get("category"),
                        "reviewed": d.get("reviewed"),
                        "title_state": d.get("title_state")}

        # NAME order, and case-folded to one lock when the move is only a
        # capitalization fix: APFS is case-insensitive, so 'board prep' and
        # 'Board Prep' are the same lock FILE, and taking it twice on one
        # thread deadlocks against itself (flock is per-fd, not per-thread).
        targets = ([base] if want.lower() == base.lower()
                   else sorted((base, want)))
        with contextlib.ExitStack() as locks:
            for t in targets:
                locks.enter_context(review.lock_meeting(t))
            if _dest_taken(want):
                continue      # claimed between the plan and the lock: re-plan
            try:
                d = json.loads(j.read_text())
            except (OSError, ValueError):
                return {"ok": False, "error": f"no transcript for '{base}'"}
            if _apply(d) != want:
                continue      # the meeting moved under us: re-plan and re-lock
            _write_json(j, d)
            renamed = _move_folder(base, want)
        return {"ok": True, "base": want, "renamed": renamed, "date": d.get("date"),
                "category": d.get("category"), "reviewed": d.get("reviewed"),
                "title_state": d.get("title_state")}

    return {"ok": False,
            "error": "another edit keeps taking that name -- try again in a moment"}


def rename_meeting(base: str, new_base: str) -> dict:
    """Retitle a meeting. The meeting's date is appended (MMDDYYYY) when the
    typed name carries none, so 'Weekly Check-in' lands as
    'Weekly Check-in 07102026' on disk while the panel still shows the clean
    name. A name that already has a date is taken as typed (and becomes the
    stored date). Returns {ok, renamed, base}."""
    r = apply_meeting_edits(base, title=new_base)
    if not r["ok"]:
        return r
    return {"ok": True, "renamed": r["renamed"], "base": r["base"]}


def set_meeting_date(base: str, date_str: str) -> dict:
    """Correct a meeting's date. This RE-STAMPS the folder so its name keeps
    matching the date (that is what keeps recurring meetings unique); the panel
    shows the clean title either way. Returns {ok, date, base}."""
    r = apply_meeting_edits(base, date=date_str)
    if not r["ok"]:
        return r
    return {"ok": True, "date": r["date"], "base": r["base"]}


def set_meeting_category(base: str, category: str) -> dict:
    """Flag a meeting Work or Personal (or clear it with ""). A json field, not a
    folder move: the folder name is the meeting's identity everywhere (registries,
    locks, endpoints), so organizing by folders would make a flag change a
    rename-level operation. Preserved across a Redo like the corrected date."""
    r = apply_meeting_edits(base, category=category)
    if not r["ok"]:
        return r
    return {"ok": True, "category": r["category"]}


def _cluster_ids(speaker_id) -> list:
    """One cluster id, or a list of them (every cluster of ONE voice), as a
    list in order with repeats dropped. An empty entry stays in, so the
    caller refuses the request instead of silently acting on less."""
    raw = speaker_id if isinstance(speaker_id, (list, tuple)) else [speaker_id]
    return list(dict.fromkeys(str(x or "").strip() for x in raw))


def _set_dismissed(base: str, speaker_id, dismissed: bool) -> dict:
    """Add or drop a cluster id in the meeting's top-level "dismissed_voices".

    What it means: "leave this voice as unknown and stop asking me about it."
    Some clusters are grumbling, a cough, a hallway echo, music. They are not
    people, so naming them is wrong, but their lines are real audio and the
    transcript attribution stays exactly as it is. This ONLY silences the "?"
    prompts (row chips, transcript legend, the naming panel) for this meeting.

    Why TOP-LEVEL and not a per-speaker field: relabel.py and review._rewrite
    both rebuild "speakers"/"segments"/"words" from scratch and pass every OTHER
    key through untouched. A flag parked on a speaker entry would be erased by
    the next relabel; a top-level list survives both.

    Why a Redo deliberately loses it: process_file re-clusters the audio, so
    "SPEAKER_03" after a Redo is a DIFFERENT voice than "SPEAKER_03" before it.
    Carrying the list forward (the way the date and category are carried) would
    silence the wrong voice, which is worse than asking again. So it resets.

    The id must already be one of this meeting's speakers: never store junk a
    client made up, or the list slowly fills with ids that match nothing.

    `speaker_id` may be a list: every cluster of one split voice. Every id is
    checked before any is written, and one write takes them all. Sent one
    request per cluster, a refused second id left the first already
    dismissed (or restored) while the panel reported the whole action failed.
    """
    from . import review

    if base not in config.meeting_bases():
        return {"ok": False, "error": f"no meeting '{base}'"}
    sids = _cluster_ids(speaker_id)
    if not sids or not all(sids):
        return {"ok": False, "error": "no speaker"}
    j = config.meeting_file(base, ".json")
    if not j.exists():
        return {"ok": False, "error": f"no transcript for '{base}'"}
    with review.lock_meeting(base):
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            return {"ok": False, "error": f"no transcript for '{base}'"}
        roster = {s.get("id") for s in d.get("speakers", [])}
        for sid in sids:
            if sid not in roster:
                return {"ok": False,
                        "error": f"'{sid}' is not a voice in this meeting"}
        cur = {str(x) for x in d.get("dismissed_voices", []) or []}
        if dismissed:
            cur.update(sids)
        else:
            cur.difference_update(sids)
        if cur:
            d["dismissed_voices"] = sorted(cur)
        else:
            d.pop("dismissed_voices", None)   # empty means no key at all
        _write_json(j, d)
    return {"ok": True, "dismissed": sorted(cur)}


def dismiss_voice(base: str, speaker_id) -> dict:
    """"Not a real speaker", per meeting: stop prompting to name this cluster
    (or each cluster in a list) while leaving its lines attributed exactly as
    they are."""
    return _set_dismissed(base, speaker_id, True)


def restore_voice(base: str, speaker_id) -> dict:
    """Undo a dismissal: the voice becomes nameable again in this meeting."""
    return _set_dismissed(base, speaker_id, False)


def set_local_name(base: str, speaker_id, name: str) -> dict:
    """Name a cluster for THIS meeting only: a transcript label, never a
    voiceprint. The quality floor refuses to ENROLL a voice with seconds of
    speech (a thin embedding would mislabel people in every future meeting),
    but the human still knows who spoke, and the transcript is theirs to label.

    Stored in the meeting's top-level "local_names" {cluster_id: name}, the
    same persistence contract as "dismissed_voices": relabel passes top-level
    keys through and re-applies the names onto the rebuilt roster, and a Redo
    deliberately resets it because re-clustering reshuffles the ids.

    An empty name clears the entry (the undo). The id must be one of this
    meeting's speakers: never store junk a client made up.

    `speaker_id` may be a list: every cluster of one split voice. Every id is
    checked before any is written, and one write labels them all. Labeled
    one id at a time, a refused second id left the first one's name on disk,
    and the next relabel of the meeting stamped a name the user saw fail."""
    from . import review

    if base not in config.meeting_bases():
        return {"ok": False, "error": f"no meeting '{base}'"}
    sids = _cluster_ids(speaker_id)
    if not sids or not all(sids):
        return {"ok": False, "error": "no speaker"}
    if any(sid.startswith("MANUAL_") for sid in sids):
        # a MANUAL_ speaker is a person a human ADDED to a line; the diarizer
        # never heard them, so there is no cluster and relabel's local_names
        # overlay (which only walks diarized clusters) can never apply the
        # name. Accepting it reported ok:true for a save that does nothing.
        return {"ok": False,
                "error": "that speaker was added by hand, so it has no voice "
                         "cluster to name -- edit the line's speaker instead"}
    nm = str(name or "").strip()
    if len(nm) > 80:
        return {"ok": False, "error": "that name is too long"}
    j = config.meeting_file(base, ".json")
    if not j.exists():
        return {"ok": False, "error": f"no transcript for '{base}'"}
    if not config.meeting_file(base, ".diar.npz").exists():
        # only a relabel can carry the label onto the roster, the segments,
        # and the .txt, and relabel_one skips a meeting with no diar cache.
        # Accepting the write would store a name that can never apply.
        return {"ok": False, "error": "this meeting has no speaker cache, so "
                                      "the name could never be applied"}
    with review.lock_meeting(base):
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            return {"ok": False, "error": f"no transcript for '{base}'"}
        roster = {s.get("id") for s in d.get("speakers", [])}
        gone = {str(x) for x in (d.get("dismissed_voices") or [])}
        for sid in sids:
            if sid not in roster:
                return {"ok": False,
                        "error": f"'{sid}' is not a voice in this meeting"}
            if nm and sid in gone:
                # dismissed means "this is not a person": naming it anyway
                # leaves the meeting both dismissed and named, a state no
                # surface renders. Clearing a name (nm == "") stays allowed:
                # that is the undo.
                return {"ok": False,
                        "error": "that voice is dismissed as not a real speaker "
                                 "in this meeting -- restore it first, then name it"}
        cur = dict(d.get("local_names") or {})
        for sid in sids:
            if nm:
                cur[sid] = nm
            else:
                cur.pop(sid, None)
        if cur:
            d["local_names"] = cur
        else:
            d.pop("local_names", None)   # empty means no key at all
        _write_json(j, d)
    return {"ok": True, "local_names": cur}


def apply_auto_title(base: str) -> dict:
    """Give a meeting with no typed title its automatic title, once, after
    the summary (the owner's title chain, plan row 20): the named speakers
    plus the topic, else the model's draft. The folder moves through
    apply_meeting_edits, so the registries, the manifest, the id index and
    search follow it. `source_name` keeps the raw arrival name.

    Returns {ok, base, renamed?, skipped?}. A meeting that a person already
    titled, or that carries a typed label, is skipped."""
    from . import titles
    j = config.meeting_file(base, ".json")
    try:
        d = json.loads(j.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"no transcript for '{base}'"}
    if not titles.wants_auto_title(d):
        return {"ok": True, "base": base, "skipped": "not a model-titled meeting"}
    t = titles.auto_title(d, me=config.mic_speaker())
    if not t:
        return {"ok": True, "base": base, "skipped": "no draft title"}
    return apply_meeting_edits(base, title=t, auto_title=True)

"""Local LLM features: title/summary suggestions and per-meeting Q&A.

Runs Qwen3-8B (4-bit, MLX) fully on-device — transcripts never leave the machine,
which matters because some recordings are sensitive. Used by the control panel's
Rename flow (suggest a title from the transcript, the user edits/approves, then
rename_meeting() renames every artifact consistently) and its Ask dialog
(answer_question: grounded answers about one meeting's transcript).
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
text = tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
out = generate(model, tokenizer, prompt=text, max_tokens=req["max_tokens"], verbose=False)
print(json.dumps({"text": out}))
"""


def available() -> bool:
    return LLM_PY.exists()


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


def _generate(prompt: str, max_tokens: int = 2000, lock_timeout: float | None = None) -> str:
    import subprocess
    if not available():
        raise RuntimeError(".venv-llm missing — run: uv venv --python 3.12 .venv-llm && "
                           "uv pip install --python .venv-llm/bin/python mlx-lm 'transformers<5'")
    with _llm_lock(lock_timeout):
        r = subprocess.run([str(LLM_PY), "-c", _RUNNER],
                           input=json.dumps({"model": MODEL, "prompt": prompt,
                                             "max_tokens": max_tokens}),
                           capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"LLM runner failed: {r.stderr[-400:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])["text"]
    # Qwen3 emits <think>...</think> before the answer; keep only the answer
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL)
    return out.strip()


def _transcript_sample(base: str, max_chars: int = 9000) -> tuple[str, bool]:
    """(text, truncated). Long meetings keep their head, middle, and tail with
    [...] where spans were dropped — the flag lets callers say so honestly."""
    txt = config.meeting_file(base, ".txt").read_text(encoding="utf-8")
    if len(txt) <= max_chars:
        return txt, False
    third = max_chars // 3
    return (txt[:third] + "\n[...]\n" + txt[len(txt)//2 - third//2: len(txt)//2 + third//2]
            + "\n[...]\n" + txt[-third:]), True


QA_MAX_CHARS = 80_000   # ~20k tokens: every meeting on disk so far fits whole,
                        # and Qwen3's 32k context still has room for history +
                        # a 2000-token answer
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
    answer = _generate(prompt, max_tokens=2000, lock_timeout=15.0)
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


def suggest_title(base: str) -> dict:
    """Suggest a filename + detailed summary for a processed meeting from its
    transcript. The result is persisted into the meeting's .json (ai_title /
    ai_summary) so it stays visible in the panel afterwards."""
    sample, _ = _transcript_sample(base, max_chars=12000)
    prompt = (
        "Below is a (partial) meeting transcript. Produce:\n"
        "1. A SHORT descriptive title for the recording file: 3-6 words, Title Case, "
        "no punctuation except spaces, naming the meeting type and main topic (key "
        "participant names only if clearly central).\n"
        "2. A BRIEF summary of 2-3 sentences covering the main topics, any "
        "decisions made, and open questions. Plain prose, no bullet points, "
        "specific rather than generic. Never begin with 'The meeting', 'This "
        "meeting', or similar; open with the substance itself (the main topic, "
        "a decision, or who wanted what) so different meetings read differently.\n"
        "3. NEXT STEPS: every concrete commitment someone made, one per line, "
        "formatted exactly as:  - [<speaker name as labeled in the transcript>] "
        "will <specific action> by <stated deadline, or 'no date given'>\n"
        "Only include commitments actually stated; if there are none, write "
        "exactly:  - none\n"
        "Answer in exactly this format:\n"
        "TITLE: <title>\nSUMMARY: <summary>\nNEXT STEPS:\n- ...\n\n"
        f"TRANSCRIPT:\n{sample}"
    )
    out = _generate(prompt, max_tokens=3000)
    title, summary, steps = "", [], []
    mode = None
    for line in out.splitlines():
        u = line.upper()
        if u.startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
            mode = None
        elif u.startswith("SUMMARY:"):
            summary.append(line.split(":", 1)[1].strip())
            mode = "summary"
        elif u.startswith("NEXT STEPS"):
            mode = "steps"
        elif mode == "summary" and line.strip():
            summary.append(line.strip())
        elif mode == "steps" and line.strip().startswith("-"):
            item = line.strip().lstrip("-").strip()
            if item and item.lower() not in ("none", "none stated", "none stated."):
                steps.append(item)
    summary = " ".join(s for s in summary if s)
    title = re.sub(r'[<>:"/\\|?*]', "", title).strip() or "Untitled Meeting"
    result = {"title": title, "summary": summary, "next_steps": steps,
              "suggested_name": f"{title} {_date_suffix(base)}"}
    # persist so the panel can show it without regenerating. Take the per-meeting
    # lock and re-read INSIDE it (the LLM call above can run for minutes, so a
    # read taken earlier would be stale) and write atomically, so this can't
    # clobber a concurrent human review edit of the same json.
    from . import review
    j = config.meeting_file(base, ".json")
    try:
        with review.lock_meeting(base):
            d = json.loads(j.read_text())
            d["ai_title"], d["ai_summary"] = title, summary
            d["ai_next_steps"] = steps
            d["ai_generated_at"] = datetime.now().isoformat(timespec="seconds")
            tmp = j.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
            os.replace(tmp, j)
    except Exception:
        pass
    return result


def rename_meeting(base: str, new_base: str) -> dict:
    """Rename a meeting: every file inside its folder (whatever the suffix —
    transcript, audio, caches, review/verify sidecars) AND the folder itself,
    so the on-disk name always matches what the GUI shows. Refuses
    collisions. Returns {ok, renamed: [...]}."""
    new_base = re.sub(r'[<>:"/\\|?*]', "", new_base).strip()
    if not new_base or new_base == base:
        return {"ok": False, "error": "empty or unchanged name"}
    old_dir = config.meeting_dir(base)
    new_dir = config.meeting_dir(new_base)
    if not old_dir.is_dir():
        return {"ok": False, "error": f"no meeting folder for '{base}'"}
    if new_dir.exists():
        return {"ok": False, "error": f"'{new_base}' already exists"}
    # Serialize against a concurrent relabel_one(base), which holds the same
    # per-base lock for its whole read->rewrite span; the lock file lives in
    # meetings_dir/.locks (a sibling of the folder), so it survives the rename.
    from . import review
    renamed = []
    prefix = base + "."
    with review.lock_meeting(base):
        for f in sorted(old_dir.iterdir()):
            if f.is_file() and f.name.startswith(prefix):
                dst = old_dir / (new_base + f.name[len(base):])
                f.rename(dst)
                renamed.append(dst.name)
        old_dir.rename(new_dir)
        # the speaker registries reference meetings BY NAME: every unknown's
        # "heard in" list and every enrolled sample's source drive their ▶
        # voice playback, so a rename that skips them leaves dead play buttons
        from . import identify, unknowns
        try:
            unknowns.rename_meeting_refs(base, new_base)
            identify.rename_source_refs(base, new_base)
        except Exception as e:
            print(f"   rename: registry references not updated ({e})",
                  file=sys.stderr)
        # keep the source_file field coherent for future relabels
        j = config.meeting_file(new_base, ".json")
        if j.exists():
            try:
                d = json.loads(j.read_text())
                old_sf = d.get("source_file", "")
                suf = Path(old_sf).suffix or ".m4a"
                d["source_file"] = new_base + suf
                d["renamed_from"] = base
                j.write_text(json.dumps(d, indent=2, ensure_ascii=False))
            except Exception:
                pass
    return {"ok": bool(renamed), "renamed": renamed}


def set_meeting_date(base: str, date_str: str) -> dict:
    """Correct a meeting's stored date (drives month grouping/sorting in the
    panel). The pipeline stamps its best guess at process time; a human fixes
    the odd one here — never by renaming files."""
    from datetime import date as _date

    from . import review
    try:
        iso = _date.fromisoformat(str(date_str).strip()).isoformat()
    except ValueError:
        return {"ok": False, "error": "date should look like 2026-07-04"}
    j = config.meeting_file(base, ".json")
    if not j.exists():
        return {"ok": False, "error": f"no transcript for '{base}'"}
    with review.lock_meeting(base):
        d = json.loads(j.read_text())
        d["date"] = iso
        tmp = j.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
        os.replace(tmp, j)
    return {"ok": True, "date": iso}

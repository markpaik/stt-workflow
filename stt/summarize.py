"""LLM features: title/summary suggestions and per-meeting Q&A.

The DEFAULT backend is Qwen3-8B (4-bit, MLX) fully on-device — transcripts never
leave the machine, which matters because some recordings are sensitive. A cloud
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


def backend_available(backend: str) -> bool:
    if backend == "local":
        return LLM_PY.exists()
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
        if LLM_PY.exists():
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
    thinking model answered well. Cloud backends ignore the flag."""
    backend = backend or llm_backend()
    if backend == "anthropic":
        return _generate_anthropic(prompt, max_tokens)
    if backend == "openai":
        return _generate_openai(prompt, max_tokens)

    import subprocess
    if not LLM_PY.exists():
        raise RuntimeError(".venv-llm missing — run: uv venv --python 3.12 .venv-llm && "
                           "uv pip install --python .venv-llm/bin/python mlx-lm 'transformers<5'")
    with _llm_lock(lock_timeout):
        r = subprocess.run([str(LLM_PY), "-c", _RUNNER],
                           input=json.dumps({"model": MODEL, "prompt": prompt,
                                             "max_tokens": max_tokens, "think": think}),
                           capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"LLM runner failed: {r.stderr[-400:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])["text"]
    # Qwen3 emits <think>...</think> before the answer; keep only the answer
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
    backend = _backend_for(base)  # strict meetings never reach a cloud LLM
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
    out = _generate(prompt, max_tokens=3000, backend=backend, think=False)
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
    from . import identify, manifest, unknowns
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
    return renamed


def apply_meeting_edits(base: str, *, title=None, date=None, category=None,
                        reviewed=None) -> dict:
    """The ONE place a meeting's human-owned fields change: title, date,
    Work/Personal category, and whether it still needs review.

    The invariant it enforces: the folder is named "<title> MMDDYYYY". That is
    what keeps recurring meetings ('LT Weekly Meeting') unique on disk, and it is
    the whole reason the date lives in the name. So correcting a date RE-STAMPS
    the folder instead of letting the name and the stored date drift apart — they
    used to, because set_meeting_date only ever rewrote the json.

    Only a title or date edit moves the folder. Tagging a category (or accepting
    from the inbox without renaming) must not silently rename anything.
    Returns {ok, base, renamed, date, category, reviewed}.
    """
    from . import dates, review

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

    new_title = None
    if title is not None:
        new_title = _sanitize_name(title)
        if not new_title:
            return {"ok": False, "error": "empty name"}

    j = config.meeting_file(base, ".json")
    with review.lock_meeting(base):
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            return {"ok": False, "error": f"no transcript for '{base}'"}
        if iso is not None:
            d["date"] = iso
        if category is not None:
            if cat:
                d["category"] = cat
            else:
                d.pop("category", None)
        if reviewed is not None:
            d["reviewed"] = bool(reviewed)

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

        _write_json(j, d)
        renamed = _move_folder(base, want) if want != base else []

    return {"ok": True, "base": want, "renamed": renamed, "date": d.get("date"),
            "category": d.get("category"), "reviewed": d.get("reviewed")}


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

"""Local LLM title/summary suggestions for renaming poorly-named recordings.

Runs Qwen3-8B (4-bit, MLX) fully on-device — transcripts never leave the machine,
which matters because some recordings are sensitive. Used by the control panel's
Rename flow: suggest a title from the transcript, the user edits/approves, then
rename_meeting() renames every artifact consistently.
"""
import json
import os
import re
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


def _generate(prompt: str, max_tokens: int = 2000) -> str:
    import subprocess
    if not available():
        raise RuntimeError(".venv-llm missing — run: uv venv --python 3.12 .venv-llm && "
                           "uv pip install --python .venv-llm/bin/python mlx-lm 'transformers<5'")
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


def _transcript_sample(base: str, max_chars: int = 9000) -> str:
    txt = config.meeting_file(base, ".txt").read_text(encoding="utf-8")
    if len(txt) <= max_chars:
        return txt
    third = max_chars // 3
    return (txt[:third] + "\n[...]\n" + txt[len(txt)//2 - third//2: len(txt)//2 + third//2]
            + "\n[...]\n" + txt[-third:])


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
    sample = _transcript_sample(base, max_chars=12000)
    prompt = (
        "Below is a (partial) meeting transcript. Produce:\n"
        "1. A SHORT descriptive title for the recording file: 3-6 words, Title Case, "
        "no punctuation except spaces, naming the meeting type and main topic (key "
        "participant names only if clearly central).\n"
        "2. A DETAILED summary of 4-7 sentences covering: the main topics discussed, "
        "any decisions made or positions taken, disagreements or open questions, and "
        "concrete action items / next steps with owners where stated. Write in plain "
        "prose, no bullet points, specific rather than generic.\n"
        "Answer in exactly this format:\n"
        "TITLE: <title>\nSUMMARY: <summary>\n\n"
        f"TRANSCRIPT:\n{sample}"
    )
    out = _generate(prompt, max_tokens=3000)
    title, summary = "", []
    in_summary = False
    for line in out.splitlines():
        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
            in_summary = False
        elif line.upper().startswith("SUMMARY:"):
            summary.append(line.split(":", 1)[1].strip())
            in_summary = True
        elif in_summary and line.strip():
            summary.append(line.strip())
    summary = " ".join(s for s in summary if s)
    title = re.sub(r'[<>:"/\\|?*]', "", title).strip() or "Untitled Meeting"
    result = {"title": title, "summary": summary,
              "suggested_name": f"{title} {_date_suffix(base)}"}
    # persist so the panel can show it without regenerating
    j = config.meeting_file(base, ".json")
    try:
        d = json.loads(j.read_text())
        d["ai_title"], d["ai_summary"] = title, summary
        d["ai_generated_at"] = datetime.now().isoformat(timespec="seconds")
        j.write_text(json.dumps(d, indent=2, ensure_ascii=False))
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
    renamed = []
    prefix = base + "."
    for f in sorted(old_dir.iterdir()):
        if f.is_file() and f.name.startswith(prefix):
            dst = old_dir / (new_base + f.name[len(base):])
            f.rename(dst)
            renamed.append(dst.name)
    old_dir.rename(new_dir)
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

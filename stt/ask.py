"""Library Ask (unified plan Phase 4F): one question across the meetings.

1. Retrieve. The question's content words go to the FTS5 index
   (search.ranked_lines, bm25, any word). The best lines become passages:
   each hit line with two lines before and after it, merged where they
   overlap, up to PASSAGES passages and PASSAGE_CHARS characters in all, so
   the prompt stays well inside Qwen3-8B's 32k context.
2. Answer. The passages go to the same local model stt/summarize.py uses
   (summarize._generate), each under an id such as [S3]. The model must
   answer only from them, cite an id after each claim, and reply NOT FOUND
   when they do not answer the question.
3. Check. An answer with no valid citation is treated as NOT FOUND, so the
   page never shows an answer it cannot trace. Nothing found reads "I did
   not find this in your meetings." with the closest search hits, and the
   model is not called at all when retrieval finds nothing.

Citations are stored with `meeting_id` (and the folder name at save time,
the line index, and the second), so a saved thread survives a rename.

Saved threads live in PROJECT_DIR/ask_threads/<id>.json (folder 700, files
600). tools/purge.py removes every thread that cites a purged meeting.

Scope: all recordings, one computed group, or the last N days. A strict
meeting never reaches a cloud assistant: with a cloud backend selected, its
passages are dropped and the answer says how many meetings were left out.
The thread history follows the same rule: an earlier exchange that cites a
strict meeting, or a meeting that no longer resolves, stays out of a cloud
prompt.

A reply that stops inside the model's reasoning (no closing </think>) is an
error, not an answer, and the exchange is not saved.

Busy: the local model takes one request at a time (summarize.LLMBusy after
a short wait). The caller reports it and keeps the typed question.
"""
import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, search, summarize

PASSAGES = 8            # passages sent to the model
PASSAGE_CHARS = 24_000  # ~6k tokens of excerpts
WINDOW = 2              # lines before and after a hit line
CANDIDATES = 60         # ranked lines read to build the passages
MAX_TERMS = 12
HISTORY = 3             # earlier exchanges in a thread sent as context
NOT_FOUND_TEXT = "I did not find this in your meetings."
OUT_OF_ROOM_TEXT = ("The assistant ran out of room reasoning about this one. "
                    "Try a more specific question.")
EMPTY_TEXT = "Ask starts after the first meeting finishes."
_THREAD_ID = re.compile(r"^[0-9a-f]{12}$")

# Words that carry no topic. A question's other words are its search terms.
_STOP = set("""
a about above after again all also am an and any are as at be because been
before being below between both but by can could did do does doing done down
during each few for from further had has have having he her here hers him his
how i if in into is it its itself just know let me more most my no nor not now
of off on once only or other our ours out over own said same say says she
should so some such tell than that the their them then there these they this
those through to too under until up us very was we were what when where which
while who whom why will with would you your yours meeting meetings anyone
someone something anything thing things discuss discussed talk talked mention
mentioned decide decided get got go going ever please give list show find
""".split())


# ---------------------------------------------------------------- retrieve ---

def terms(question: str) -> list:
    """The content words of a question, in order, without repeats."""
    out = []
    for w in re.findall(r"\w+", (question or "").lower()):
        if w in _STOP or (len(w) < 3 and not w.isdigit()):
            continue
        if w not in out:
            out.append(w)
    return out[:MAX_TERMS]


def scope_bases(scope: dict):
    """(bases or None for all, label, error). Scope kinds: all, group, days."""
    scope = scope if isinstance(scope, dict) else {}
    kind = scope.get("kind") or "all"
    if kind == "all":
        return None, "All recordings", None
    if kind == "group":
        from . import groups
        g = groups.by_key(str(scope.get("key") or ""))
        if not g:
            return set(), "A group that no longer exists", "that group no longer exists"
        return set(g["bases"]), g["name"], None
    if kind == "days":
        try:
            n = max(1, min(3650, int(scope.get("days") or 30)))
        except (TypeError, ValueError):
            return set(), "", "days should be a number"
        since = (date.today() - timedelta(days=n)).isoformat()
        rows = search.facet_rows()
        return ({r["base"] for r in rows if (r.get("day") or "") >= since},
                f"Last {n} days", None)
    return set(), "", f"unknown scope '{kind}'"


def passages(question: str, bases=None) -> list:
    """The excerpts for a question: [{sid, base, mid, title, day, index,
    start, lines: [{index, start, who, text}]}], best first."""
    hits = search.ranked_lines(terms(question), bases=bases, limit=CANDIDATES)
    out, used = [], 0
    for h in hits:
        if len(out) >= PASSAGES:
            break
        # a hit inside a passage already taken adds nothing new
        if any(p["base"] == h["base"]
               and p["lines"][0]["index"] <= h["index"] <= p["lines"][-1]["index"]
               for p in out):
            continue
        lines = search.neighbors(h["base"], h["index"], WINDOW, WINDOW) or [
            {"index": h["index"], "start": h["start"], "who": h["who"], "text": h["text"]}]
        size = sum(len(ln["text"] or "") + 24 for ln in lines)
        if used + size > PASSAGE_CHARS and out:
            break
        used += size
        out.append({"sid": f"S{len(out) + 1}", "base": h["base"], "mid": h.get("mid"),
                    "title": h.get("title") or h["base"], "day": h.get("day"),
                    "index": h["index"], "start": h["start"], "lines": lines})
    return out


def _clock(t) -> str:
    t = max(0, int(float(t or 0)))
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def prompt(question: str, ps: list, history=None) -> str:
    blocks = []
    for p in ps:
        head = f"[{p['sid']}] {p['title']} · {p.get('day') or 'no date'}"
        body = "\n".join(f"[{_clock(ln['start'])}] {ln.get('who') or 'Speaker'}: {ln['text']}"
                         for ln in p["lines"])
        blocks.append(head + "\n" + body)
    hist = ""
    for h in (history or [])[-HISTORY:]:
        q, a = str(h.get("q", ""))[:2000].strip(), str(h.get("a", ""))[:3000].strip()
        if q and a:
            hist += f"Q: {q}\nA: {a}\n\n"
    return (
        "You answer questions about the owner's recorded meetings, using ONLY "
        "the excerpts below. Each excerpt starts with an id in square brackets, "
        "such as [S2], then the meeting title and date, then timestamped lines.\n"
        "Rules:\n"
        "- Answer only from the excerpts. Never guess or use outside knowledge.\n"
        "- After each statement, cite the excerpt it came from with its id in "
        "square brackets, for example [S2]. Use only ids that appear below.\n"
        "- If the excerpts do not answer the question, reply with exactly: NOT FOUND\n"
        "- Name speakers as the excerpts name them.\n"
        "- Be concise: a few sentences, or a short list.\n\n"
        "EXCERPTS:\n" + "\n\n".join(blocks) + "\n\n"
        + (f"EARLIER QUESTIONS IN THIS THREAD (context only):\n{hist}" if hist else "")
        + f"QUESTION: {question}"
    )


_CITE = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]", re.I)


_ENDS_NOT_FOUND = re.compile(r"(?:^|[.!?:;\n])\s*\**\s*NOT\s+FOUND\s*\**[.!]?\s*$", re.I)


def parse_answer(text: str, ps: list) -> dict:
    """{nothing_found, answer, citations}, or {error} when the reply stopped
    inside the model's reasoning. Each [S#] becomes a numbered citation [1],
    [2] in order of first use. Ids the passages do not hold are dropped. An
    answer with no valid citation, or one whose last sentence is NOT FOUND,
    counts as nothing found."""
    text = (text or "").strip()
    if "<think>" in text or "</think>" in text:
        # generation can stop mid-reasoning: the closing tag never arrives,
        # _generate's strip does not match, and the chain of thought (with
        # any [S#] it names) must never pass for an answer
        text = text.split("</think>")[-1].strip()
        if not text or "<think>" in text:
            return {"error": OUT_OF_ROOM_TEXT}
    flat = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if (not text or flat.startswith("not found") or flat == "notfound"
            or _ENDS_NOT_FOUND.search(text)):
        return {"nothing_found": True, "answer": NOT_FOUND_TEXT, "citations": []}
    by_sid = {p["sid"].upper(): p for p in ps}
    order = []

    def sub(m):
        nums = []
        for sid in re.split(r"\s*[,;]\s*", m.group(1).upper()):
            if sid in by_sid:
                if sid not in order:
                    order.append(sid)
                nums.append(str(order.index(sid) + 1))
        return "[" + ", ".join(nums) + "]" if nums else ""
    answer = _CITE.sub(sub, text)
    answer = re.sub(r"[ \t]+([.,;:])", r"\1", answer).strip()
    if not order:
        return {"nothing_found": True, "answer": NOT_FOUND_TEXT, "citations": []}
    cites = []
    for n, sid in enumerate(order, 1):
        p = by_sid[sid]
        cites.append({"n": n, "meeting_id": p.get("mid"), "base": p["base"],
                      "title": p["title"], "date": p.get("day"),
                      "start": p["start"], "index": p["index"]})
    return {"nothing_found": False, "answer": answer, "citations": cites}


def closest(ps: list, n: int = 5) -> list:
    """The nearest search hits, for a nothing-found answer."""
    out = []
    for p in ps[:n]:
        hit = next((ln for ln in p["lines"] if ln["index"] == p["index"]), p["lines"][0])
        out.append({"meeting_id": p.get("mid"), "base": p["base"], "title": p["title"],
                    "date": p.get("day"), "start": hit["start"],
                    "snippet": (hit["text"] or "")[:200]})
    return out


# ------------------------------------------------------------------- answer ---

def _cloud_safe_history(history) -> tuple:
    """(history, dropped) for a cloud prompt: an exchange stays only when
    every citation it stored resolves to a live meeting that is not strict.
    An exchange with no stored citations cannot be checked, so it stays out."""
    keep, dropped = [], 0
    for h in history or []:
        cites = h.get("citations") if isinstance(h, dict) else None
        ok = bool(cites)
        for c in cites or []:
            base = _resolve(c).get("base") if isinstance(c, dict) else None
            if not base or summarize._strict_meeting(base):
                ok = False
                break
        if ok:
            keep.append(h)
        else:
            dropped += 1
    return keep, dropped


def ask(question: str, scope=None, history=None, generate=None) -> dict:
    """Answer one question across the meetings in `scope`.

    Returns {ok, answer, nothing_found, citations, hits, scope_label,
    left_out, elapsed_sec}, or {ok: False, error} (with `empty: True` for an
    empty library). Raises summarize.LLMBusy when the local model is busy.
    `generate` replaces the model call (tests and the evaluation)."""
    question = (question or "").strip()[:2000]
    if not question:
        return {"ok": False, "error": "empty question"}
    if not config.meeting_bases():
        return {"ok": False, "empty": True, "error": EMPTY_TEXT}
    bases, label, err = scope_bases(scope)
    if err:
        return {"ok": False, "error": err}
    t0 = time.monotonic()
    backend = summarize.llm_backend()
    ps = passages(question, bases)
    left_out = 0
    if backend != "local" and generate is None:
        # a strict meeting never leaves this Mac: drop its excerpts, and any
        # earlier answer in the thread that came from one
        strict = {p["base"] for p in ps if summarize._strict_meeting(p["base"])}
        if strict:
            left_out = len(strict)
            ps = [p for p in ps if p["base"] not in strict]
            for i, p in enumerate(ps, 1):
                p["sid"] = f"S{i}"
        history, _ = _cloud_safe_history(history)
    base_out = {"ok": True, "scope_label": label, "left_out": left_out}
    if not ps:
        return {**base_out, "nothing_found": True, "answer": NOT_FOUND_TEXT,
                "citations": [], "hits": [],
                "elapsed_sec": round(time.monotonic() - t0, 1)}
    p = prompt(question, ps, history)
    if generate is not None:
        raw = generate(p)
    else:
        raw = summarize._generate(p, max_tokens=2500, lock_timeout=15.0,
                                  backend=backend, think=True)
    r = parse_answer(raw, ps)
    if r.get("error"):
        return {"ok": False, "error": r["error"]}
    return {**base_out, **r, "hits": closest(ps) if r["nothing_found"] else [],
            "elapsed_sec": round(time.monotonic() - t0, 1)}


# ------------------------------------------------------------------ threads ---

def threads_dir() -> Path:
    return Path(config.PROJECT_DIR) / "ask_threads"


def _ensure_dir() -> Path:
    d = threads_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


@contextlib.contextmanager
def _locked():
    d = _ensure_dir()
    with open(d / ".lock", "w") as fh:
        os.chmod(d / ".lock", 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def valid_id(tid) -> bool:
    return bool(_THREAD_ID.match(str(tid or "")))


def _path(tid: str) -> Path:
    if not valid_id(tid):
        raise ValueError("bad thread id")
    return threads_dir() / f"{tid}.json"


def _read(tid: str):
    try:
        d = json.loads(_path(tid).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write(d: dict):
    p = _path(d["id"])
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(d, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_message(tid, scope: dict, label: str, question: str, result: dict) -> dict:
    """Append one exchange to thread `tid` (a new thread when tid is None).
    Returns the stored thread."""
    with _locked():
        d = _read(tid) if tid else None
        if d is None:
            tid = uuid.uuid4().hex[:12]
            d = {"id": tid, "created_at": _now(),
                 "scope": dict(scope or {"kind": "all"}, label=label), "messages": []}
        d["messages"].append({
            "q": question, "a": result.get("answer", ""),
            "nothing_found": bool(result.get("nothing_found")),
            "citations": result.get("citations") or [],
            "hits": result.get("hits") or [],
            "left_out": result.get("left_out") or 0,
            "elapsed_sec": result.get("elapsed_sec"), "at": _now()})
        d["updated_at"] = _now()
        _write(d)
    return d


def history_of(tid) -> list:
    d = _read(tid) if valid_id(tid) else None
    if not d:
        return []
    return [{"q": m.get("q", ""), "a": m.get("a", ""),
             "citations": [{"meeting_id": c.get("meeting_id"), "base": c.get("base")}
                           for c in m.get("citations") or [] if isinstance(c, dict)]}
            for m in d.get("messages", []) if not m.get("nothing_found")]


def _resolve(c: dict) -> dict:
    """A stored citation with its meeting's CURRENT folder name, or gone."""
    c = dict(c)
    live = set(config.meeting_bases())
    base = None
    if c.get("meeting_id"):
        try:
            base = config.base_of(c["meeting_id"])
        except Exception:
            base = None
    if not base and c.get("base") in live:
        base = c["base"]
    c["base"] = base
    c["gone"] = base is None
    return c


def get_thread(tid) -> dict | None:
    """One thread with every citation and hit resolved to its meeting's
    current folder name (`gone` when the meeting is no longer live)."""
    if not valid_id(tid):
        return None
    d = _read(tid)
    if not d:
        return None
    for m in d.get("messages", []):
        m["citations"] = [_resolve(c) for c in m.get("citations") or []]
        m["hits"] = [_resolve(h) for h in m.get("hits") or []]
    return d


def list_threads() -> list:
    """[{id, q, scope, updated_at, n}], newest first. No answer text."""
    out = []
    d = threads_dir()
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        if not valid_id(p.stem):
            continue
        t = _read(p.stem)
        if not t or not t.get("messages"):
            continue
        out.append({"id": t["id"], "q": t["messages"][0].get("q", ""),
                    "scope": (t.get("scope") or {}).get("label", ""),
                    "updated_at": t.get("updated_at", ""), "n": len(t["messages"])})
    out.sort(key=lambda r: r["updated_at"], reverse=True)
    return out


def delete_thread(tid) -> bool:
    if not valid_id(tid):
        return False
    with _locked():
        try:
            _path(tid).unlink()
            return True
        except FileNotFoundError:
            return False

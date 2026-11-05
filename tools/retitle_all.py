#!/usr/bin/env python
"""Retitle every meeting by the series catalog, and write the new description.

The local model (Qwen 3.8 on Ollama, else Qwen3-8B) reads each meeting once.
It names a series from stt/series.py (or one_off), the other person for a
1:1 series, and a short topic, and it writes the new description: a gist,
Decisions, Open questions, Next steps, and 3 to 5 topic tags. Code builds
the title from the catalog ("LT Weekly Meeting – Budget Reallocation"); the
folder keeps its MMDDYYYY date stamp.

  ./run.sh py tools/retitle_all.py                  # dry run: proposals only
  ./run.sh py tools/retitle_all.py --only "Check-in" --limit 5
  ./run.sh py tools/retitle_all.py --apply          # rename from the proposals
  ./run.sh py tools/retitle_all.py --undo           # dry run of the reversal
  ./run.sh py tools/retitle_all.py --undo --apply   # reverse every rename

The dry run changes nothing in the library. It writes the proposal file
work/retitle/proposals.json (with proposals.tsv beside it for reading) and
caches each model reply under work/retitle/cache/, so a stopped dry run
resumes where it stopped and a second run costs no model time. --refresh
ignores the cache.

The dry run keeps the owner's hardware limits (stt/config.py): the model
runs at lowered priority, one request at a time, and the run rests after
each meeting for STT_LLM_REST_RATIO times its wall time (at least
STT_LLM_REST_MIN_SEC), logging each rest.

--apply calls no model. It applies proposals.json as reviewed: each rename
goes through summarize.apply_meeting_edits (so the registries, the
manifest, the id index and search follow), and name collisions get the
same " (2)" rule as any rename (_unique_base). It writes a reversal journal
to config.migration_dir() first, line by line, so it resumes after a stop
and skips a meeting that is already done. It refuses to start while
status.json shows the batch running, and stops cleanly if a run starts
mid-way. A meeting renamed by a person since the dry run is skipped.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, control, dates, series, status, summarize, titles  # noqa: E402

VERSION = 1
JOURNAL_NAME = "retitle-journal.jsonl"
STRAY_DATE = re.compile(r"(?<!\d)(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{1,2}:\d{2})(?!\d)")


# ------------------------------------------------------------------ paths ---
def out_dir() -> Path:
    return config.WORK_DIR / "retitle"


def proposals_path() -> Path:
    return out_dir() / "proposals.json"


def journal_path() -> Path:
    return config.migration_dir() / JOURNAL_NAME


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _write_private(p: Path, text: str):
    _private_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def prompt_version() -> str:
    """Changes when the catalog, the tags, the prompt or the think setting
    change, so a cached
    reply to an older prompt is never reused."""
    probe = summarize.description_prompt("H", "S", "T", ["P"], "O")
    return hashlib.sha256(f"{VERSION}|{summarize.DESCRIPTION_THINK}|{probe}"
                          .encode()).hexdigest()[:12]


# ---------------------------------------------------------- batch guards ---
def batch_running() -> bool:
    return bool(status.read().get("running"))


def busy_reason():
    if batch_running():
        return "the batch is running (status.json). Try again when it is idle."
    try:
        if control.relabel_running():
            return "a relabel pass is running. Try again when it finishes."
    except Exception:
        pass
    return None


# ------------------------------------------------------------- the cache ---
def _key(base: str, d: dict) -> str:
    mid = d.get("meeting_id")
    if mid:
        return re.sub(r"[^\w.-]", "_", str(mid))
    return "b_" + hashlib.sha256(base.encode()).hexdigest()[:16]


def _txt_sig(base: str) -> str:
    try:
        st = config.meeting_file(base, ".txt").stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def _cache_get(key: str, sig: str, pv: str):
    p = out_dir() / "cache" / f"{key}.json"
    try:
        c = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if c.get("txt_sig") == sig and c.get("prompt_version") == pv:
        return c.get("desc")
    return None


def _cache_put(key: str, sig: str, pv: str, desc: dict):
    _write_private(out_dir() / "cache" / f"{key}.json",
                   json.dumps({"txt_sig": sig, "prompt_version": pv, "desc": desc},
                              ensure_ascii=False, indent=1))


# ----------------------------------------------------------------- trust ---
def _first(n: str) -> str:
    return (str(n or "").split() or [""])[0].rstrip(".")


def assess(old_base: str, d: dict, desc: dict) -> tuple:
    """(trust 0-100, flags): how far to trust a proposal. Lower is worse."""
    flags, score = [], 100
    old = dates.title_of(old_base)
    guess = series.guess_series(old)
    sid = desc.get("series")
    label = titles.is_label(old)
    if desc.get("series") is None:
        flags.append("reply not in the new format")
        score -= 50
    if guess and sid != guess:
        flags.append(f"series {sid} but the old title reads as {guess}")
        score -= 35
    elif sid in series.BY_ID and not guess and label:
        flags.append(f"series {sid} not named in the old title")
        score -= 15
    s = series.BY_ID.get(sid)
    if s is not None and s.has_person:
        if not desc.get("person"):
            flags.append("person series with no person")
            score -= 30
        else:
            if " " not in str(desc.get("person_full") or "").replace(" and ", "&"):
                # a bare first name: no enrolled or named speaker matched it
                flags.append("person not matched to a full name")
                score -= 10
            firsts = [_first(p) for p in re.split(r"\s+and\s+", desc["person"])]
            if label and not all(f.lower() in old.lower() for f in firsts if f):
                flags.append("person not in the old title")
                score -= 15
    if not desc.get("topic") or desc.get("topic") == "Untitled Meeting":
        flags.append("no topic")
        score -= 15
    elif len(str(desc.get("topic_raw") or "").split()) > 7:
        flags.append("topic was cut")
        score -= 5
    for p in desc.get("gist_problems") or []:
        flags.append(f"gist: {p}")
        score -= 10
    if len(desc.get("tags") or []) < series.TAG_MIN:
        flags.append(f"{len(desc.get('tags') or [])} tags")
        score -= 10
    if (desc.get("parts") or 1) > 1:
        flags.append(f"long meeting read in {desc['parts']} parts")
        score -= 5
    if not summarize._named_roster(d):
        flags.append("no named speakers")
        score -= 5
    m = STRAY_DATE.search(dates.strip_stamp(re.sub(r"\s+\(\d+\)$", "", old_base)))
    if m:
        flags.append(f"old title carries a date: {m.group(1)}")
    if d.get("title_state") == "confirmed":
        flags.append("a person confirmed the old title")
        score -= 10
    return max(0, score), flags


# --------------------------------------------------------------- planning ---
def _taken_names() -> set:
    names = {b.lower() for b in config.meeting_bases()}
    names |= {b.lower() for b in config.archived_bases()}
    return names


def _predict_base(title: str, d: dict, old_base: str, taken: set) -> str:
    """The folder name apply_meeting_edits would give `title`, with the
    _unique_base twin rule run against `taken` (lowercased names)."""
    on = d.get("date") or ""
    want = dates.restamp(title, on) if on else title
    want = summarize._sanitize_name(want)
    if want.lower() == old_base.lower() or want.lower() not in taken:
        return want
    i = 2
    while f"{want} ({i})".lower() in taken:
        i += 1
    return f"{want} ({i})"


def dry_run(only=None, limit=None, refresh=False, log=print) -> dict:
    """Write the proposal file. Changes nothing in the library."""
    pv = prompt_version()
    bases = config.meeting_bases()
    if only:
        bases = [b for b in bases if only.lower() in b.lower()]
    if limit:
        bases = bases[:limit]
    taken = _taken_names()
    proposals = []
    t_all = time.monotonic()
    for i, base in enumerate(bases, 1):
        try:
            d = json.loads(config.meeting_file(base, ".json").read_text())
        except (OSError, ValueError):
            proposals.append({"old_base": base, "skip": "unreadable json"})
            continue
        row = {"meeting_id": d.get("meeting_id"), "old_base": base,
               "old_title": dates.title_of(base), "date": d.get("date")}
        if not summarize.has_speech(base):
            proposals.append({**row, "skip": "no speech"})
            continue
        if status.meeting_active(base):
            proposals.append({**row, "skip": "being processed"})
            continue
        key, sig = _key(base, d), _txt_sig(base)
        desc = None if refresh else _cache_get(key, sig, pv)
        cached = desc is not None
        if desc is None:
            t_one = time.monotonic()
            try:
                desc = summarize.describe(base, old_title=dates.title_of(base))
            except Exception as e:
                proposals.append({**row, "skip": f"model failed: {str(e)[:160]}"})
                log(f"[{i}/{len(bases)}] {base}: model failed ({e})")
                if i < len(bases):
                    config.llm_rest(time.monotonic() - t_one, log=log)
                continue
            _cache_put(key, sig, pv, desc)
            # the duty cycle (stt/config.py): rest after each meeting the
            # model read, so a long dry run never runs the fans flat out
            rest_after = time.monotonic() - t_one
        else:
            rest_after = None
        # re-parse the stored reply with the current parser: a parser fix
        # reaches cached meetings with no model call
        desc = reparse(base, desc)
        trust, flags = assess(base, d, desc)
        new_base = _predict_base(desc["title"], d, base, taken)
        if new_base.lower() != base.lower():
            taken.discard(base.lower())
            taken.add(new_base.lower())
        if re.search(r" \(\d+\)$", new_base) and not re.search(r" \(\d+\)$", base):
            flags.append("name collision: twin suffix added")
            trust = max(0, trust - 10)
        s = series.BY_ID.get(desc.get("series"))
        proposals.append({
            **row, "new_base": new_base, "new_title": desc["title"],
            "series": desc.get("series"), "series_name": s.name(desc.get("person") or "") if s else None,
            "person": desc.get("person"), "topic": desc.get("topic"),
            "gist": desc.get("gist"), "tags": desc.get("tags"),
            "decisions": desc.get("decisions"), "open_questions": desc.get("open_questions"),
            "next_steps": desc.get("next_steps"), "trust": trust, "flags": flags,
            "model": desc.get("model"), "engine": desc.get("engine"),
            # what --apply checks before it writes anything: the transcript
            # and the diarization this description was written from
            "words_sig": words_sig(d), "diar_sig": diar_sig(base),
            "stale_at_dry_run": bool(d.get("ai_stale")),
            "parts": desc.get("parts"), "desc": desc, "skip": None})
        log(f"[{i}/{len(bases)}] {'(cached) ' if cached else ''}{desc.get('elapsed_sec', 0) if not cached else 0}s"
            f"  trust {trust:3d}  {desc.get('series')}")
        if rest_after is not None and i < len(bases):
            config.llm_rest(rest_after, log=log)
    doc = {"version": VERSION, "prompt_version": pv,
           "created_at": datetime.now().isoformat(timespec="seconds"),
           "library": str(config.meetings_dir()), "complete": not (only or limit),
           "elapsed_sec": round(time.monotonic() - t_all, 1),
           "proposals": proposals}
    _write_private(proposals_path(), json.dumps(doc, ensure_ascii=False, indent=1))
    _write_private(proposals_path().with_suffix(".tsv"), tsv(proposals))
    return doc


def reparse(base: str, desc: dict) -> dict:
    """`desc` rebuilt from its stored model reply with the current parser.
    A reply cached without its context is re-read against the meeting as
    it stands now. A desc with no reply is returned as it is."""
    raw = desc.get("raw")
    if not raw:
        return desc
    ctx = desc.get("ctx") or summarize.context_for(
        base, old_title=dates.title_of(base), engine=desc.get("engine"),
        parts=desc.get("parts") or 1, elapsed_sec=desc.get("elapsed_sec") or 0.0)
    return summarize.build_description(raw, ctx)


def words_sig(d: dict) -> str:
    """The transcript's own text and timing, without speaker labels: a
    naming or relabel leaves it alone, a Redo or a text edit changes it."""
    rows = [(w.get("start"), w.get("end"), w.get("word")) for w in d.get("words") or []]
    if not rows:
        rows = [(x.get("start"), x.get("end"), x.get("text")) for x in d.get("segments") or []]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def diar_sig(base: str) -> str:
    p = config.meeting_file(base, ".diar.npz")
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def tsv(proposals: list) -> str:
    cols = ("trust", "old_base", "new_base", "series", "person", "tags", "gist", "flags", "skip")
    out = ["\t".join(cols)]
    for p in sorted(proposals, key=lambda p: (p.get("trust") is None, p.get("trust") or 0)):
        vals = []
        for c in cols:
            v = p.get(c)
            if isinstance(v, list):
                v = "; ".join(str(x) for x in v)
            vals.append(re.sub(r"[\t\n]+", " ", "" if v is None else str(v)))
        out.append("\t".join(vals))
    return "\n".join(out) + "\n"


def series_counts(proposals: list) -> Counter:
    c = Counter()
    for p in proposals:
        if p.get("skip"):
            c[f"(skipped: {p['skip'].split(':')[0]})"] += 1
        else:
            c[p.get("series") or "(old format)"] += 1
    return c


# ---------------------------------------------------------------- journal ---
def _journal_append(entry: dict):
    p = journal_path()
    _private_dir(p.parent)
    new = not p.exists()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**entry, "at": datetime.now().isoformat(timespec="seconds")},
                            ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if new:
        os.chmod(p, 0o600)


def journal_entries() -> list:
    try:
        lines = journal_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def journal_state() -> dict:
    """{meeting_id: last event} over the whole journal."""
    st = {}
    for e in journal_entries():
        if e.get("meeting_id"):
            st[e["meeting_id"]] = e
    return st


def progress() -> dict:
    """{meeting_id: {begin, renamed, applied}} since the last undo of each
    meeting. `renamed` is the folder name a landed rename gave it (journaled
    as its own event, so a description that failed afterwards is still
    known to have moved the folder). `applied` is True once the description
    is written as well."""
    st = {}
    for e in journal_entries():
        mid, ev = e.get("meeting_id"), e.get("event")
        if not mid:
            continue
        if ev == "undo":
            st.pop(mid, None)
        elif ev == "begin":
            st[mid] = {"begin": e, "renamed": None, "applied": False}
        elif ev == "renamed" and mid in st:
            st[mid]["renamed"] = e.get("new_base")
        elif ev == "apply" and mid in st:
            st[mid]["applied"] = True
            st[mid]["renamed"] = e.get("new_base") or st[mid]["renamed"]
    return st


_BEFORE_KEYS = ("title", "title_state", "title_source") + summarize.DESCRIPTION_KEYS + ("ai_stale",)
_MISSING = {"$missing": True}


def _before_fields(d: dict) -> dict:
    return {k: (d[k] if k in d else _MISSING) for k in _BEFORE_KEYS}


def _current_base(mid: str, old_base: str):
    if mid:
        b = config.base_of(mid)
        if b:
            return b
    return old_base if old_base in config.meeting_bases() else None


# ------------------------------------------------------------------ apply ---
def apply(log=print, stop_on_batch=True) -> dict:
    """Apply proposals.json. Resumable: the journal says what is done.

    Per meeting: a `begin` line with the fields as they were, the rename,
    a `renamed` line, the description, an `apply` line. A rerun finishes a
    meeting whose rename landed but whose description did not. Nothing is
    written to a meeting whose transcript or diarization changed since the
    dry run, or that a Redo made out of date since then."""
    why = busy_reason()
    if why:
        return {"ok": False, "error": why}
    try:
        doc = json.loads(proposals_path().read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"no proposal file at {proposals_path()}: run the dry run first"}
    prog = progress()
    counts = Counter()
    for p in doc.get("proposals", []):
        mid, old = p.get("meeting_id"), p.get("old_base")
        if p.get("skip") or not p.get("desc"):
            counts["skipped in the proposals"] += 1
            continue
        if stop_on_batch and batch_running():
            log("the batch started: stopping here. Run --apply again when it is idle.")
            counts["stopped for the batch"] += 1
            break
        st = prog.get(mid) if mid else None
        if st and st["applied"]:
            counts["already done"] += 1
            continue
        cur = _current_base(mid, old)
        if cur is None:
            log(f"  skip {old}: the meeting is gone")
            counts["gone"] += 1
            continue
        landed = st["renamed"] if st else None
        if st and not landed and cur != old:
            # the rename landed and the run stopped before it was journaled
            landed = cur
            _journal_append({"event": "renamed", "meeting_id": mid, "old_base": old,
                             "new_base": cur, "recovered": True})
        if landed and cur != landed:
            log(f"  skip {old}: renamed by someone else after the retitle (now {cur})")
            counts["renamed since the dry run"] += 1
            continue
        if not landed and cur != old:
            log(f"  skip {old}: renamed since the dry run (now {cur})")
            counts["renamed since the dry run"] += 1
            continue
        try:
            d = json.loads(config.meeting_file(cur, ".json").read_text())
        except (OSError, ValueError):
            counts["unreadable"] += 1
            continue
        changed = _changed_since(p, d, cur)
        if changed:
            log(f"  skip {old}: {changed}. Run the dry run again for it.")
            counts["changed since the dry run"] += 1
            continue
        title = p["new_title"]
        if not landed:
            _journal_append({"event": "begin", "meeting_id": mid, "old_base": old,
                             "planned_base": p.get("new_base"),
                             "before": _before_fields(d)})
            r = summarize.apply_meeting_edits(cur, title=title, retitle=True)
            if not r.get("ok"):
                _journal_append({"event": "fail", "meeting_id": mid, "old_base": old,
                                 "error": r.get("error")})
                log(f"  FAILED {old}: {r.get('error')}")
                counts["failed"] += 1
                continue
            landed = r["base"]
            _journal_append({"event": "renamed", "meeting_id": mid, "old_base": old,
                             "new_base": landed})
        try:
            summarize.store_description(landed, p["desc"], title=title)
        except Exception as e:
            _journal_append({"event": "fail", "meeting_id": mid, "old_base": old,
                             "new_base": landed, "error": f"description: {e}"})
            log(f"  FAILED description for {landed}: {e} (the rename stays; "
                "a rerun finishes it)")
            counts["failed"] += 1
            continue
        _journal_append({"event": "apply", "meeting_id": mid, "old_base": old,
                         "new_base": landed})
        counts["applied"] += 1
        log(f"  {old}  ->  {landed}")
    return {"ok": True, "counts": dict(counts)}


def _changed_since(p: dict, d: dict, base: str):
    """Why the meeting no longer matches its proposal, else None."""
    if not p.get("words_sig"):
        return "the proposal has no transcript signature (an older dry run)"
    if words_sig(d) != p["words_sig"]:
        return "its transcript changed since the dry run"
    if diar_sig(base) != (p.get("diar_sig") or ""):
        return "its speaker diarization changed since the dry run"
    if d.get("ai_stale") and not p.get("stale_at_dry_run"):
        return "a Redo made its description out of date after the dry run"
    return None


def undo(dry: bool = True, log=print) -> dict:
    """Reverse every rename in the journal that landed, described or not:
    the old name and the fields as they were. A dry run prints the plan."""
    if not dry:
        why = busy_reason()
        if why:
            return {"ok": False, "error": why}
    counts = Counter()
    for mid, st in progress().items():
        if not st["renamed"] and not st["applied"]:
            continue
        b = st["begin"]
        old = b["old_base"]
        cur = _current_base(mid, st["renamed"])
        if cur is None:
            counts["gone"] += 1
            continue
        log(f"  {cur}  ->  {old}")
        if dry:
            counts["would undo"] += 1
            continue
        if batch_running():
            log("the batch started: stopping here.")
            break
        r = summarize.apply_meeting_edits(cur, title=old, retitle=True)
        if not r.get("ok"):
            log(f"  FAILED {cur}: {r.get('error')}")
            counts["failed"] += 1
            continue
        before = b.get("before") or {}
        from stt import review
        j = config.meeting_file(r["base"], ".json")
        with review.lock_meeting(r["base"]):
            d = json.loads(j.read_text())
            for k, v in before.items():
                if k == "title":
                    continue          # the folder name decides it
                if isinstance(v, dict) and v.get("$missing"):
                    d.pop(k, None)
                else:
                    d[k] = v
            summarize._write_json(j, d)
        if d.get("ai_stale"):
            # back out of date, as it was: the batch's index lists it again
            summarize.stale_mark(mid, r["base"])
        _journal_append({"event": "undo", "meeting_id": mid, "old_base": old,
                         "new_base": cur, "restored_base": r["base"]})
        counts["undone"] += 1
    return {"ok": True, "counts": dict(counts)}


# ------------------------------------------------------------------- main ---
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--only", help="dry run: meetings whose folder name holds this text")
    ap.add_argument("--limit", type=int, help="dry run: at most N meetings")
    ap.add_argument("--refresh", action="store_true", help="dry run: ignore the cache")
    a = ap.parse_args(argv)
    if a.undo:
        r = undo(dry=not a.apply)
    elif a.apply:
        r = apply()
    else:
        doc = dry_run(only=a.only, limit=a.limit, refresh=a.refresh)
        props = doc["proposals"]
        print(f"\nproposals: {proposals_path()}")
        print(f"readable:  {proposals_path().with_suffix('.tsv')}")
        print("series:")
        for k, n in sorted(series_counts(props).items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {n:4d}  {k}")
        low = sorted((p for p in props if not p.get("skip")), key=lambda p: p["trust"])[:15]
        print("least trusted:")
        for p in low:
            print(f"  {p['trust']:3d}  {p['old_base']}  ->  {p['new_base']}  "
                  f"[{'; '.join(p['flags'])}]")
        return 0
    if not r.get("ok"):
        print(f"refused: {r['error']}", file=sys.stderr)
        return 2
    for k, n in sorted(r["counts"].items()):
        print(f"  {n:4d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

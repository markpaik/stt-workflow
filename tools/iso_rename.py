#!/usr/bin/env python
"""Move every meeting folder to the ISO-first name, "YYYY-MM-DD <title>".

Folders named before the rule read "<title> MMDDYYYY". The code reads both
forms, but only the ISO form is written, so this moves the library across
once. A same-name same-date twin becomes "YYYY-MM-DD <title> (2)".

  ./run.sh py tools/iso_rename.py               # dry run: the plan only
  ./run.sh py tools/iso_rename.py --apply       # rename from the plan
  ./run.sh py tools/iso_rename.py --undo        # dry run of the reversal
  ./run.sh py tools/iso_rename.py --undo --apply

The dry run changes nothing in the library. It writes the plan to
config.migration_dir() (iso-rename-plan.json, with iso-rename-plan.tsv beside
it for reading), mode 600, outside the repo: the plan holds meeting names.

The new name is the stored date in front of the title (dates.restamp). The
stored date wins over the folder's own stamp, and the plan flags a meeting
where the two differ. The old recorder name 'Recording 07112026 1814' loses
its mid-name date ('2026-07-11 Recording 1814'), and the plan flags that too.

--apply renames each meeting through summarize.apply_meeting_edits, the same
path a date edit in the panel takes, so the speaker registries, voiceprint
references, the manifest, the id index, search and the kept-duplicate list
follow the folder. It also points each .txt header line at the meeting's
date and title (output.header_name), on every meeting, renamed or not. It
journals to config.migration_dir() line by line before each change, so it
resumes after a stop and skips a meeting that is done. It refuses to start
while the batch or a relabel runs, and stops cleanly if a batch starts
mid-way. A meeting renamed by a person since the dry run is skipped.

--undo puts each folder back under its old name and restores the fields the
move changed (title, source_file, renamed_from, date, date_source) and the
old header line.
"""
import argparse
import fcntl
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, control, dates, output, status, summarize  # noqa: E402

VERSION = 1
PLAN_NAME = "iso-rename-plan.json"
JOURNAL_NAME = "iso-rename-journal.jsonl"
_MISSING = {"$missing": True}
# date and date_source too: a folder whose json had no date gets the
# name's own date stored as 'typed' by the move, and undo takes it off again
_BEFORE_KEYS = ("title", "source_file", "renamed_from", "date", "date_source")


# ------------------------------------------------------------------ paths ---
def plan_path() -> Path:
    return config.migration_dir() / PLAN_NAME


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


# ---------------------------------------------------------- batch guards ---
def _batch_lock_held() -> bool:
    """True while run_batch.py holds batch.lock. Opened read-only, so the
    file's pid is never truncated."""
    p = config.PROJECT_DIR / "batch.lock"
    try:
        fd = open(p, "r")
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
    return False


def batch_running() -> bool:
    return bool(status.read().get("running")) or _batch_lock_held()


def busy_reason():
    if batch_running():
        return "the batch is running. Try again when it is idle."
    try:
        if control.relabel_running():
            return "a relabel pass is running. Try again when it finishes."
    except Exception:
        pass
    return None


# ----------------------------------------------------------------- header ---
def header_line(base: str):
    """The .txt's first line, or None."""
    try:
        with open(config.meeting_file(base, ".txt"), encoding="utf-8") as fh:
            return fh.readline().rstrip("\n")
    except OSError:
        return None


def header_stale(base: str, d: dict, new_base: str) -> bool:
    """True when the header line does not open with the name it should
    have after the move."""
    line = header_line(base)
    if line is None or not output._HEADER_LINE.match(line):
        return False
    want = output.header_name(new_base, {**d, "title": dates.title_of(new_base)})
    return output._HEADER_LINE.match(line).group(1) != want


# --------------------------------------------------------------- planning ---
def _taken_names() -> set:
    names = {b.lower() for b in config.meeting_bases()}
    names |= {b.lower() for b in config.archived_bases()}
    return names


def _predict(want: str, old_base: str, taken: set) -> str:
    """The name apply_meeting_edits gives `want`: the _unique_base twin rule
    run against `taken` (lowercased names)."""
    want = summarize._sanitize_name(want)
    if want.lower() == old_base.lower() or want.lower() not in taken:
        return want
    i = 2
    while f"{want} ({i})".lower() in taken:
        i += 1
    return f"{want} ({i})"


def plan(log=print) -> dict:
    """Write the plan file. Changes nothing in the library."""
    taken = _taken_names()
    rows = []
    for base in config.meeting_bases():
        try:
            d = json.loads(config.meeting_file(base, ".json").read_text())
        except (OSError, ValueError):
            rows.append({"old_base": base, "skip": "unreadable json"})
            continue
        row = {"meeting_id": d.get("meeting_id"), "old_base": base,
               "date": d.get("date"), "flags": []}
        if status.meeting_active(base):
            rows.append({**row, "skip": "being processed"})
            continue
        peeled = dates._UNIQ_SUFFIX.sub("", base)
        stamp_iso = dates.stamp_date(peeled) or dates.meeting_date(peeled)
        iso = d.get("date") or stamp_iso
        if not iso:
            rows.append({**row, "skip": "no stored date and no date in the name"})
            continue
        if not d.get("date"):
            row["flags"].append(f"no stored date: the name's {iso} is used")
        elif stamp_iso and stamp_iso != d["date"]:
            row["flags"].append(f"folder stamp {stamp_iso} differs from stored date "
                                f"{d['date']}: the stored date is used")
        want = dates.restamp(base, iso)
        if want == peeled:
            new_base = base           # already in the ISO form
        else:
            new_base = _predict(want, base, taken)
            if new_base.lower() != base.lower():
                taken.discard(base.lower())
                taken.add(new_base.lower())
        if dates.twin_suffix(new_base) and dates.twin_suffix(new_base) != dates.twin_suffix(base):
            row["flags"].append("name collision: twin suffix "
                                + (f"{dates.twin_suffix(base).strip()} -> " if dates.twin_suffix(base) else "")
                                + f"{dates.twin_suffix(new_base).strip()}")
        old_title = d.get("title") or dates.title_of(base)
        # the title the apply step stores: a '1:1' kept while the folder
        # holds '1-1' is no change (dates.shown_title)
        new_title = dates.shown_title(d.get("title"), new_base)
        if old_title != new_title:
            row["flags"].append(f"title changes: {old_title!r} -> {new_title!r}")
        row.update(new_base=new_base, new_title=new_title, date=iso,
                   rename=new_base != base,
                   header=header_stale(base, d, new_base), skip=None)
        rows.append(row)
    doc = {"version": VERSION,
           "created_at": datetime.now().isoformat(timespec="seconds"),
           "library": str(config.meetings_dir()), "rows": rows}
    _write_private(plan_path(), json.dumps(doc, ensure_ascii=False, indent=1))
    _write_private(plan_path().with_suffix(".tsv"), tsv(rows))
    return doc


def tsv(rows: list) -> str:
    cols = ("old_base", "new_base", "rename", "header", "flags", "skip")
    out = ["\t".join(cols)]
    for r in rows:
        vals = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, list):
                v = "; ".join(str(x) for x in v)
            vals.append(" ".join(("" if v is None else str(v)).split()))
        out.append("\t".join(vals))
    return "\n".join(out) + "\n"


def counts(rows: list) -> Counter:
    c = Counter()
    for r in rows:
        if r.get("skip"):
            c[f"skipped: {r['skip']}"] += 1
            continue
        c["rename" if r["rename"] else "name already ISO"] += 1
        if r["header"]:
            c["header rewrite"] += 1
        for f in r["flags"]:
            c["flag: " + f.split(":")[0]] += 1
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


def _jkey(e: dict):
    return e.get("meeting_id") or f"base:{e.get('old_base')}"


def progress() -> dict:
    """{key: {begin, renamed, applied}} since the last undo of each meeting."""
    st = {}
    for e in journal_entries():
        k, ev = _jkey(e), e.get("event")
        if ev == "undo":
            st.pop(k, None)
        elif ev == "begin":
            st[k] = {"begin": e, "renamed": None, "applied": False}
        elif ev == "renamed" and k in st:
            st[k]["renamed"] = e.get("new_base")
        elif ev == "apply" and k in st:
            st[k]["applied"] = True
            st[k]["renamed"] = e.get("new_base") or st[k]["renamed"]
    return st


def _current_base(mid, base):
    if mid:
        b = config.base_of(mid)
        if b:
            return b
    return base if base in config.meeting_bases() else None


def _refresh_header(base: str) -> bool:
    from stt import review
    with review.lock_meeting(base):
        try:
            d = json.loads(config.meeting_file(base, ".json").read_text())
        except (OSError, ValueError):
            d = {}
        return output.refresh_txt_header(config.meeting_file(base, ".txt"),
                                         output.header_name(base, d))


# ------------------------------------------------------------------ apply ---
def apply(log=print) -> dict:
    """Apply the plan file. Resumable: the journal says what is done."""
    why = busy_reason()
    if why:
        return {"ok": False, "error": why}
    try:
        doc = json.loads(plan_path().read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"no plan file at {plan_path()}: run the dry run first"}
    prog = progress()
    c = Counter()
    for r in doc.get("rows", []):
        if r.get("skip"):
            c["skipped in the plan"] += 1
            continue
        if batch_running():
            log("the batch started: stopping here. Run --apply again when it is idle.")
            c["stopped for the batch"] += 1
            break
        mid, old = r.get("meeting_id"), r["old_base"]
        k = _jkey(r)
        st = prog.get(k)
        if st and st["applied"]:
            c["already done"] += 1
            continue
        cur = _current_base(mid, st["renamed"] if st and st["renamed"] else old)
        if cur is None:
            log(f"  skip {old}: the meeting is gone")
            c["gone"] += 1
            continue
        landed = st["renamed"] if st else None
        if st and not landed and cur != old:
            landed = cur          # the rename landed before it was journaled
            _journal_append({"event": "renamed", "meeting_id": mid, "old_base": old,
                             "new_base": cur, "recovered": True})
        if landed and cur != landed:
            log(f"  skip {old}: renamed by someone else after the move (now {cur})")
            c["renamed since the dry run"] += 1
            continue
        if not landed and cur != old:
            log(f"  skip {old}: renamed since the dry run (now {cur})")
            c["renamed since the dry run"] += 1
            continue
        try:
            d = json.loads(config.meeting_file(cur, ".json").read_text())
        except (OSError, ValueError):
            c["unreadable"] += 1
            continue
        if not st:
            if (d.get("date") or r["date"]) != r["date"]:
                log(f"  skip {old}: its date changed since the dry run")
                c["changed since the dry run"] += 1
                continue
            _journal_append({"event": "begin", "meeting_id": mid, "old_base": old,
                             "planned_base": r["new_base"],
                             "before": {k2: (d[k2] if k2 in d else _MISSING)
                                        for k2 in _BEFORE_KEYS},
                             "header": header_line(cur)})
        if not landed:
            if r["rename"]:
                kw = {"date": r["date"]}
                if not d.get("date"):
                    kw["date_source"] = "typed"   # the name's own stamp
                res = summarize.apply_meeting_edits(cur, **kw)
                if not res.get("ok"):
                    _journal_append({"event": "fail", "meeting_id": mid, "old_base": old,
                                     "error": res.get("error")})
                    log(f"  FAILED {old}: {res.get('error')}")
                    c["failed"] += 1
                    continue
                landed = res["base"]
                if landed != r["new_base"]:
                    log(f"  note {old}: landed as {landed}, the plan said {r['new_base']}")
                    c["landed on another name"] += 1
            else:
                landed = cur
            _journal_append({"event": "renamed", "meeting_id": mid, "old_base": old,
                             "new_base": landed})
        try:
            if _refresh_header(landed):
                c["header rewritten"] += 1
        except Exception as e:
            _journal_append({"event": "fail", "meeting_id": mid, "old_base": old,
                             "new_base": landed, "error": f"header: {e}"})
            log(f"  FAILED header for {landed}: {e} (a rerun finishes it)")
            c["failed"] += 1
            continue
        _journal_append({"event": "apply", "meeting_id": mid, "old_base": old,
                         "new_base": landed})
        c["renamed" if landed != old else "header only"] += 1
        if landed != old:
            log(f"  {old}  ->  {landed}")
    return {"ok": True, "counts": dict(c)}


# ------------------------------------------------------------------- undo ---
def _restore_header(base: str, line):
    """Put the journaled header line back as the .txt's first line."""
    if not line or not output._HEADER_LINE.match(line):
        return
    m = output._HEADER_LINE.match(line)
    p = config.meeting_file(base, ".txt")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    first, sep, rest = text.partition("\n")
    if output._HEADER_LINE.match(first) and first != line:
        output._atomic_write(p, m.group(1) + m.group(2) + sep + rest)


def undo(dry: bool = True, log=print) -> dict:
    """Reverse every move in the journal: the old folder name, the fields
    the move changed, and the old header line. A dry run prints the plan."""
    if not dry:
        why = busy_reason()
        if why:
            return {"ok": False, "error": why}
    c = Counter()
    for k, st in progress().items():
        b = st["begin"]
        mid, old = b.get("meeting_id"), b["old_base"]
        cur = _current_base(mid, st["renamed"] or old)
        if cur is None:
            c["gone"] += 1
            continue
        # a meeting the move only gave a new header keeps its name: say
        # so, rather than 'X  ->  X'
        log(f"  {cur}  ->  {old}" if cur != old else f"  {cur}  (header only)")
        if dry:
            c["would undo"] += 1
            continue
        if batch_running():
            log("the batch started: stopping here.")
            break
        if cur != old:
            res = summarize.apply_meeting_edits(cur, to_base=old)
            if not res.get("ok"):
                log(f"  FAILED {cur}: {res.get('error')}")
                c["failed"] += 1
                continue
            cur = res["base"]
        from stt import review
        j = config.meeting_file(cur, ".json")
        with review.lock_meeting(cur):
            d = json.loads(j.read_text())
            for k2, v in (b.get("before") or {}).items():
                if isinstance(v, dict) and v.get("$missing"):
                    d.pop(k2, None)
                else:
                    d[k2] = v
            summarize._write_json(j, d)
            _restore_header(cur, b.get("header"))
        _journal_append({"event": "undo", "meeting_id": mid, "old_base": old,
                         "new_base": st["renamed"], "restored_base": cur})
        c["undone"] += 1
    return {"ok": True, "counts": dict(c)}


# ------------------------------------------------------------------- main ---
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    a = ap.parse_args(argv)
    if a.undo or a.apply:
        # --undo alone is the undo's dry run. It fell through to the forward
        # plan, which printed 'name already ISO' counts and rewrote the plan
        # files, and the undo plan itself was never shown.
        r = undo(dry=not a.apply) if a.undo else apply()
        if not r.get("ok"):
            print(f"refused: {r['error']}", file=sys.stderr)
            return 2
        for k, n in sorted(r["counts"].items()):
            print(f"  {n:4d}  {k}")
        if a.apply:
            # every rename leaves the old name's lock file behind: sweep the
            # ones nobody holds, so the registry hygiene check stays clean
            print(f"pruned {config.prune_lock_files()} stale lock file(s)")
        else:
            print("\nDRY RUN: nothing in the library changed. "
                  "--undo --apply runs this undo.")
        return 0
    doc = plan()
    rows = doc["rows"]
    for row in rows:
        if row.get("skip"):
            print(f"  SKIP  {row['old_base']}  ({row['skip']})")
        elif row["rename"]:
            print(f"  {row['old_base']}  ->  {row['new_base']}"
                  + (f"   [{'; '.join(row['flags'])}]" if row["flags"] else ""))
    print(f"\nplan:      {plan_path()}")
    print(f"readable:  {plan_path().with_suffix('.tsv')}")
    for k, n in sorted(counts(rows).items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:4d}  {k}")
    print("\nDRY RUN: nothing in the library changed. --apply renames from this plan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

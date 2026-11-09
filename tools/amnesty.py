#!/usr/bin/env python
"""One-time relabel of the waiting backlog (unified plan, Phase 1 step 6).

A meeting from before the title rule carries no `title_state`, and one with
`reviewed` False still waits in the panel for a name. This script settles
those titles without claiming that a person checked them:

- a public download keeps its published name: provisional, public_download
- a typed label is kept as typed: provisional, label
- a default, marker, generic, or address-style name: needs_title, model

It also fills in `route` where it is missing, and on a transcript with 0
segments it sets `no_speech` and removes the summary, title draft, and next
steps the model invented from silence.

What it never does: change `reviewed`, move a folder, or touch a meeting the
batch is writing (it skips and reports that one). Each meeting is changed
under review.lock_meeting and replaced atomically, with `generated_at` left
as it was. Every changed field is written to a journal first, so undo works
one field at a time and restores a field only where it still holds the
amnesty's value: an edit made after the amnesty survives the undo.

  ./run.sh py tools/amnesty.py                          # dry run: counts only
  ./run.sh py tools/amnesty.py --apply                  # write, print the run id
  ./run.sh py tools/amnesty.py --undo RUN               # dry run of the undo
  ./run.sh py tools/amnesty.py --undo RUN --apply       # undo every field
  ./run.sh py tools/amnesty.py --undo RUN --field title_state --apply

The dry run reads the store and writes nothing, not even a lock file.
Journals live in config.migration_dir() (mode 700, files mode 600).
"""
import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, review, status, titles  # noqa: E402

MISSING = {"$missing": True}
# the model's output on a transcript with 0 segments: invented, so removed
SILENT_FIELDS = ("ai_summary", "ai_title", "ai_next_steps")


def _is_missing(v) -> bool:
    return isinstance(v, dict) and v.get("$missing") is True


def _get(d: dict, k: str):
    return d[k] if k in d else MISSING


def _set(d: dict, k: str, v) -> None:
    if _is_missing(v):
        d.pop(k, None)
    else:
        d[k] = v


def changes_for(base: str, d: dict) -> dict:
    """{field: new value} the amnesty writes to one meeting json. MISSING as
    a value means the field is removed. Pure: reads nothing but `d`."""
    out = {}
    route = d.get("route")
    if not route:
        route = titles.route_for(d.get("source_file") or f"{base}.m4a")
        out["route"] = route
    if not d.get("title_state") and d.get("reviewed") is False:
        if route == "public_download":
            out["title_state"], out["title_source"] = "provisional", "public_download"
        elif titles.is_label(base):
            out["title_state"], out["title_source"] = "provisional", "label"
        else:
            out["title_state"], out["title_source"] = "needs_title", "model"
    if not d.get("segments"):
        if not d.get("no_speech"):
            out["no_speech"] = True
        for k in SILENT_FIELDS:
            if k in d:
                out[k] = MISSING
    return out


def _read(base: str, store=None):
    try:
        return json.loads(config.meeting_file(base, ".json", store).read_text())
    except (OSError, ValueError):
        return None


def plan(store=None) -> list:
    """[{base, meeting_id, changes, skip}] for every live meeting that the
    amnesty would change. `skip` names why a meeting is left alone."""
    rows = []
    for base in config.meeting_bases(store):
        d = _read(base, store)
        if d is None:
            rows.append({"base": base, "meeting_id": None, "changes": {},
                         "skip": "unreadable json"})
            continue
        ch = changes_for(base, d)
        if not ch:
            continue
        skip = "being processed" if status.meeting_active(base) else None
        rows.append({"base": base, "meeting_id": d.get("meeting_id"),
                     "changes": ch, "skip": skip})
    return rows


def summarize_plan(rows: list, total: int) -> dict:
    """The counts a dry run prints. Titles first, as the plan states them."""
    c = Counter()
    for r in rows:
        if r["skip"]:
            c["skipped"] += 1
            continue
        ch = r["changes"]
        if "title_state" in ch:
            c[f"title:{ch['title_state']}:{ch['title_source']}"] += 1
        if "route" in ch:
            c[f"route:{ch['route']}"] += 1
        if ch.get("no_speech") is True:
            c["no_speech"] += 1
        for k in SILENT_FIELDS:
            if k in ch:
                c[f"removed:{k}"] += 1
    prov = sum(v for k, v in c.items() if k.startswith("title:provisional:"))
    return {"meetings": total, "changed": sum(1 for r in rows if not r["skip"]),
            "provisional": prov,
            "label": c["title:provisional:label"],
            "public_download": c["title:provisional:public_download"],
            "needs_title": c["title:needs_title:model"],
            "route": {k.split(":", 1)[1]: v for k, v in sorted(c.items())
                      if k.startswith("route:")},
            "no_speech": c["no_speech"],
            "removed": {k.split(":", 1)[1]: v for k, v in sorted(c.items())
                        if k.startswith("removed:")},
            "skipped": [{"base": r["base"], "why": r["skip"]} for r in rows if r["skip"]]}


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _atomic_json(path: Path, d: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Journal:
    """Append-only JSON lines, mode 600, fsynced per entry. Each entry is
    written BEFORE the json it describes, so a crash mid-run leaves a journal
    that undo can still read. The worst case is an entry whose write never
    landed, and undo skips that one because the field never took the value."""

    def __init__(self, path: Path):
        self.path = path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(path, 0o600)
        self.fh = os.fdopen(fd, "a")

    def write(self, entry: dict) -> None:
        self.fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()


def apply(store=None, journal_dir=None, run_id=None) -> dict:
    """Write the amnesty. Returns {run, journal, applied, skipped}."""
    jdir = _private_dir(Path(journal_dir or config.migration_dir()))
    run_id = run_id or datetime.now().strftime("amnesty-%Y%m%dT%H%M%S")
    jpath = jdir / f"{run_id}.jsonl"
    journal = Journal(jpath)
    applied, skipped = [], []
    try:
        for row in plan(store):
            base = row["base"]
            if row["skip"]:
                skipped.append({"base": base, "why": row["skip"]})
                continue
            with review.lock_meeting(base):
                # re-check under the lock: the batch may have started on it,
                # or a person may have edited it since the plan was read
                if status.meeting_active(base):
                    skipped.append({"base": base, "why": "being processed"})
                    continue
                j = config.meeting_file(base, ".json", store)
                try:
                    d = json.loads(j.read_text())
                except (OSError, ValueError):
                    skipped.append({"base": base, "why": "unreadable json"})
                    continue
                ch = changes_for(base, d)
                if not ch:
                    continue
                at = datetime.now().isoformat(timespec="seconds")
                for field, new in ch.items():
                    journal.write({"run": run_id, "meeting_id": d.get("meeting_id"),
                                   "base": base, "field": field,
                                   "old": _get(d, field), "new": new, "at": at})
                for field, new in ch.items():
                    _set(d, field, new)
                _atomic_json(j, d)        # generated_at stays as it was
                applied.append(base)
    finally:
        journal.close()
    return {"run": run_id, "journal": str(jpath), "applied": applied,
            "skipped": skipped}


def _journal_entries(run_id: str, journal_dir=None) -> list:
    from stt import fieldjournal
    return fieldjournal.entries(run_id, journal_dir)


def _bases_by_id(store=None) -> dict:
    """{meeting_id: base} read straight from the jsons. Never rebuilds the
    id index, so an undo dry run writes nothing."""
    out = {}
    for b in config.meeting_bases(store):
        mid = config.id_of(b, store)
        if mid:
            out[mid] = b
    return out


def undo(run_id: str, fields=None, store=None, journal_dir=None, dry_run=True) -> dict:
    """Put back each journaled field whose current value still equals the
    amnesty's value. A field a person changed since keeps the change and is
    reported as kept. `fields` limits the undo to those field names.

    `title_state` and `title_source` are one pair: when a person changed the
    state since (a Keep confirms it and leaves the source alone), the source
    is kept too, so undo never leaves a confirmed title with no source."""
    every = _journal_entries(run_id, journal_dir)
    state_new = {(e.get("meeting_id") or f"base:{e['base']}"): e["new"]
                 for e in every if e["field"] == "title_state"}
    entries = [e for e in every if not fields or e["field"] in fields]
    by_id = _bases_by_id(store)
    live = set(config.meeting_bases(store))
    groups = {}
    for e in entries:
        key = e.get("meeting_id") or f"base:{e['base']}"
        groups.setdefault(key, []).append(e)
    restored, kept, missing, skipped = [], [], [], []
    for key, es in groups.items():
        base = by_id.get(es[0].get("meeting_id")) or (
            es[0]["base"] if es[0]["base"] in live else None)
        if base is None:
            missing.extend({"base": e["base"], "field": e["field"]} for e in es)
            continue
        if status.meeting_active(base):
            skipped.append({"base": base, "why": "being processed"})
            continue

        def _fold(d):
            done, held = [], []
            state_kept = key in state_new and d.get("title_state") != state_new[key]
            for e in reversed(es):
                cur = _get(d, e["field"])
                if e["field"] == "title_source" and state_kept:
                    held.append({"base": base, "field": e["field"]})
                elif cur == e["new"] or (_is_missing(cur) and _is_missing(e["new"])):
                    _set(d, e["field"], e["old"])
                    done.append({"base": base, "field": e["field"]})
                else:
                    held.append({"base": base, "field": e["field"]})
            return done, held

        if dry_run:
            d = _read(base, store)
            if d is None:
                skipped.append({"base": base, "why": "unreadable json"})
                continue
            done, held = _fold(d)
        else:
            with review.lock_meeting(base):
                j = config.meeting_file(base, ".json", store)
                try:
                    d = json.loads(j.read_text())
                except (OSError, ValueError):
                    skipped.append({"base": base, "why": "unreadable json"})
                    continue
                done, held = _fold(d)
                if done:
                    _atomic_json(j, d)
        restored += done
        kept += held
    return {"run": run_id, "restored": restored, "kept": kept,
            "missing": missing, "skipped": skipped, "dry_run": dry_run}


def _print_plan(s: dict) -> None:
    print(f"amnesty dry run (nothing written): {s['meetings']} meetings, "
          f"{s['changed']} would change")
    print(f"  titles provisional: {s['provisional']} "
          f"(label {s['label']}, public_download {s['public_download']})")
    print(f"  titles needs_title: {s['needs_title']} (model)")
    print("  route filled in: " + (", ".join(f"{k} {v}" for k, v in s["route"].items())
                                   or "none"))
    print(f"  no_speech set: {s['no_speech']}")
    print("  removed from silent transcripts: "
          + (", ".join(f"{k} {v}" for k, v in s["removed"].items()) or "none"))
    print(f"  skipped: {len(s['skipped'])}")
    for x in s["skipped"]:
        print(f"    {x['why']}: {x['base']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--undo", metavar="RUN", help="undo the journaled run RUN")
    ap.add_argument("--field", action="append", help="limit --undo to this field")
    ap.add_argument("--journal-dir", help="journal folder (default: config.migration_dir())")
    a = ap.parse_args(argv)
    if a.undo is not None:     # "" is a mistyped run, not "no undo"
        from stt import fieldjournal
        try:
            r = undo(a.undo, fields=a.field, journal_dir=a.journal_dir,
                     dry_run=not a.apply)
        except fieldjournal.NoSuchRun as e:
            print(e, file=sys.stderr)
            return 1
        word = "would restore" if r["dry_run"] else "restored"
        print(f"undo {a.undo}: {word} {len(r['restored'])} fields, "
              f"kept {len(r['kept'])} edited since, "
              f"{len(r['missing'])} on meetings no longer live, "
              f"skipped {len(r['skipped'])} meetings")
        return 0
    if a.field:
        ap.error("--field needs --undo")
    total = len(config.meeting_bases())
    if not a.apply:
        _print_plan(summarize_plan(plan(), total))
        return 0
    r = apply(journal_dir=a.journal_dir)
    print(f"amnesty applied: run {r['run']}, {len(r['applied'])} meetings changed, "
          f"{len(r['skipped'])} skipped")
    print(f"journal: {r['journal']}")
    for x in r["skipped"]:
        print(f"  skipped ({x['why']}): {x['base']}")
    print(f"undo with: ./run.sh py tools/amnesty.py --undo {r['run']} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

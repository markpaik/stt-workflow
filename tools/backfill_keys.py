#!/usr/bin/env python
"""Backfill the store keys every meeting json should carry (unified plan,
Phase 3 step 3): `title`, `source_name`, `audio_fp`, `date_source`, and
`recorded_at`.

It only ADDS keys a meeting lacks. A key already present is never
recomputed: `audio_fp` and `source_name` are frozen at first write. It
changes no date and moves no folder: the stored date is explained, not
corrected (tools/fix_dates.py corrects). See stt/provenance.py for how
each value is derived.

The write follows config.backfill_meeting_ids: the meeting's own lock, a
re-read under it, one atomic replace, and `generated_at` left as it was, so
an open panel page does not reload. A meeting the batch is writing is
skipped and reported. Every added key goes to a journal first, and undo
removes a key only where it still holds the value this run wrote. A write
run first stamps a meeting_id on any meeting that lacks one, so undo can
find a meeting renamed after the run.

  ./run.sh py tools/backfill_keys.py                       # dry run: counts only
  ./run.sh py tools/backfill_keys.py --apply               # write, print the run id
  ./run.sh py tools/backfill_keys.py --undo RUN            # dry run of the undo
  ./run.sh py tools/backfill_keys.py --undo RUN --apply
  ./run.sh py tools/backfill_keys.py --undo RUN --field audio_fp --apply

The dry run reads the store, the manifest, and the audio headers, and
writes nothing, not even a lock file.
"""
import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, provenance, review, status  # noqa: E402
from stt import fieldjournal as fj  # noqa: E402


def _names():
    """The manifest's raw names, read without its lock (a dry run must not
    create a lock file)."""
    m = fj.read_json(config.MANIFEST_PATH) or {}
    return provenance.manifest_names(m)


def plan(store=None, probe=True, today=None) -> list:
    """[{base, meeting_id, changes, skip}] for every live meeting that lacks
    a key."""
    store = Path(store) if store else config.meetings_dir()
    names = _names()
    rows = []
    for base in config.meeting_bases(store):
        d = fj.read_json(config.meeting_file(base, ".json", store))
        if d is None:
            rows.append({"base": base, "meeting_id": None, "changes": {},
                         "skip": "unreadable json"})
            continue
        ch = provenance.derive(base, d, store, names, probe=probe, today=today)
        if not ch:
            continue
        skip = "being processed" if status.meeting_active(base) else None
        rows.append({"base": base, "meeting_id": d.get("meeting_id"),
                     "changes": ch, "skip": skip})
    return rows


def summarize_plan(rows: list, total: int) -> dict:
    c = Counter()
    for r in rows:
        if r["skip"]:
            continue
        ch = r["changes"]
        for k in provenance.KEYS:
            if k in ch:
                c[f"add:{k}"] += 1
        if "source_name" in ch and ch["source_name"] is None:
            c["source_name_unknown"] += 1
        if "date_source" in ch:
            c[f"date_source:{ch['date_source']}"] += 1
        if ch.get("recorded_at"):
            c["recorded_at_set"] += 1
    return {"meetings": total,
            "changed": sum(1 for r in rows if not r["skip"]),
            "added": {k: c[f"add:{k}"] for k in provenance.KEYS},
            "source_name_unknown": c["source_name_unknown"],
            "date_source": {s: c[f"date_source:{s}"] for s in provenance.DATE_SOURCES
                            if c[f"date_source:{s}"]},
            "recorded_at_set": c["recorded_at_set"],
            "uncertain": sum(c[f"date_source:{s}"] for s in provenance.UNCERTAIN),
            "skipped": [{"base": r["base"], "why": r["skip"]} for r in rows if r["skip"]]}


def apply(store=None, journal_dir=None, run_id=None, probe=True, today=None) -> dict:
    """Add the missing keys, journaling each one first. The journal opens
    on the first key a run adds: a run with nothing to add returns run
    None and leaves no file (it left an empty .jsonl and an undo hint for
    it). A run id is the second it started, so opening the file early
    could also have touched an earlier run's journal of the same second."""
    store = Path(store) if store else config.meetings_dir()
    run_id = run_id or datetime.now().strftime("backfill-%Y%m%dT%H%M%S")
    jpath = fj.journal_path(run_id, journal_dir)
    # undo finds each meeting by its id: a meeting journaled with none could
    # not be found again once its folder moved (tools/fix_dates.py does the same)
    config.backfill_meeting_ids(store)
    names = _names()
    applied, skipped = [], []
    journal = None
    try:
        for row in plan(store, probe=probe, today=today):
            base = row["base"]
            if row["skip"]:
                skipped.append({"base": base, "why": row["skip"]})
                continue
            with review.lock_meeting(base):
                if status.meeting_active(base):
                    skipped.append({"base": base, "why": "being processed"})
                    continue
                j = config.meeting_file(base, ".json", store)
                d = fj.read_json(j)
                if d is None:
                    skipped.append({"base": base, "why": "unreadable json"})
                    continue
                ch = provenance.derive(base, d, store, names, probe=probe, today=today)
                if not ch:
                    continue
                if journal is None:
                    journal = fj.Journal(jpath)
                at = datetime.now().isoformat(timespec="seconds")
                for field, new in ch.items():
                    journal.write({"run": run_id, "meeting_id": d.get("meeting_id"),
                                   "base": base, "field": field,
                                   "old": fj.get(d, field), "new": new, "at": at})
                for field, new in ch.items():
                    fj.put(d, field, new)
                fj.atomic_json(j, d)          # generated_at stays as it was
                applied.append(base)
    finally:
        if journal is not None:
            journal.close()
    if not applied:
        return {"run": None, "journal": None, "applied": [], "skipped": skipped}
    return {"run": run_id, "journal": str(jpath), "applied": applied,
            "skipped": skipped}


def undo(run_id: str, fields=None, store=None, journal_dir=None, dry_run=True) -> dict:
    """Remove each journaled key whose current value still equals the value
    this run wrote. A key changed since keeps the change."""
    store = Path(store) if store else config.meetings_dir()
    entries = [e for e in fj.entries(run_id, journal_dir, valid=fj.field_entry,
                                     tool="backfill_keys")
               if not fields or e["field"] in fields]
    by_id = fj.bases_by_id(store)
    live = set(config.meeting_bases(store))
    groups = {}
    for e in entries:
        groups.setdefault(e.get("meeting_id") or f"base:{e['base']}", []).append(e)
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
            for e in reversed(es):
                if fj.same(fj.get(d, e["field"]), e["new"]):
                    fj.put(d, e["field"], e["old"])
                    done.append({"base": base, "field": e["field"]})
                else:
                    held.append({"base": base, "field": e["field"]})
            return done, held

        j = config.meeting_file(base, ".json", store)
        if dry_run:
            d = fj.read_json(j)
            if d is None:
                skipped.append({"base": base, "why": "unreadable json"})
                continue
            done, held = _fold(d)
        else:
            with review.lock_meeting(base):
                d = fj.read_json(j)
                if d is None:
                    skipped.append({"base": base, "why": "unreadable json"})
                    continue
                done, held = _fold(d)
                if done:
                    fj.atomic_json(j, d)
        restored += done
        kept += held
    return {"run": run_id, "restored": restored, "kept": kept,
            "missing": missing, "skipped": skipped, "dry_run": dry_run}


def _print_plan(s: dict) -> None:
    print(f"backfill dry run (nothing written): {s['meetings']} meetings, "
          f"{s['changed']} would change")
    print("  keys added: " + ", ".join(f"{k} {v}" for k, v in s["added"].items()))
    print(f"  source_name unknown (null): {s['source_name_unknown']}")
    print("  date_source: " + (", ".join(f"{k} {v}" for k, v in s["date_source"].items())
                               or "none"))
    print(f"  date uncertain (mtime or short_typed): {s['uncertain']}")
    print(f"  recorded_at set: {s['recorded_at_set']}")
    print(f"  skipped: {len(s['skipped'])}")
    for x in s["skipped"]:
        print(f"    {x['why']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--undo", metavar="RUN", help="undo the journaled run RUN")
    ap.add_argument("--field", action="append", help="limit --undo to this key")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the audio tag read (date_source then ignores tags)")
    ap.add_argument("--journal-dir", help="journal folder (default: config.migration_dir())")
    a = ap.parse_args(argv)
    if a.undo is not None:     # "" is a mistyped run, not "no undo"
        try:
            r = undo(a.undo, fields=a.field, journal_dir=a.journal_dir,
                     dry_run=not a.apply)
        except fj.NoSuchRun as e:
            print(e, file=sys.stderr)
            return 1
        word = "would remove" if r["dry_run"] else "removed"
        print(f"undo {a.undo}: {word} {len(r['restored'])} keys, "
              f"kept {len(r['kept'])} changed since, "
              f"{len(r['missing'])} on meetings no longer live, "
              f"skipped {len(r['skipped'])} meetings")
        return 0
    if a.field:
        ap.error("--field needs --undo")
    probe = not a.no_probe
    if not a.apply:
        _print_plan(summarize_plan(plan(probe=probe), len(config.meeting_bases())))
        return 0
    r = apply(journal_dir=a.journal_dir, probe=probe)
    if r["run"] is None:
        print(f"backfill: nothing to add, 0 meetings changed, "
              f"{len(r['skipped'])} skipped")
        return 0
    print(f"backfill applied: run {r['run']}, {len(r['applied'])} meetings changed, "
          f"{len(r['skipped'])} skipped")
    print(f"journal: {r['journal']}")
    print(f"undo with: ./run.sh py tools/backfill_keys.py --undo {r['run']} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

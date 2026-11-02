#!/usr/bin/env python
"""Correct the meeting dates that a better source contradicts (unified plan,
Phase 3 step 5).

Two sources may correct a stored date that came from the file time:

- ``recorder``: a Voice Memos `creation_time` tag on a file carrying
  `voice-memo-uuid` that passes the per-file check in stt/provenance.py.
- ``short_typed``: a short date such as '9.14' typed in the label.

A typed 8-digit date and a date a person set are never changed. Each
correction goes through summarize.apply_meeting_edits with the real
`date_source`, so the folder is re-stamped and the rename follows into the
registries, the manifest, and the id index, exactly as a panel date edit
does. `recorded_at` carries the tag's start time when the recorder is the
source.

Each correction is journaled before it is applied. Undo puts the old date,
source, and start time back through the same function, and only where the
meeting still holds the corrected date: a date changed since is kept.

  ./run.sh py tools/fix_dates.py                    # dry run: counts only
  ./run.sh py tools/fix_dates.py --apply            # write, print the run id
  ./run.sh py tools/fix_dates.py --undo RUN         # dry run of the undo
  ./run.sh py tools/fix_dates.py --undo RUN --apply

The dry run reads the store, the manifest, and the audio headers, and
writes nothing.
"""
import argparse
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, provenance, status, summarize  # noqa: E402
from stt import fieldjournal as fj  # noqa: E402


def plan(store=None, probe=True, today=None) -> list:
    """[{base, meeting_id, old, new, skip}] for every meeting whose date a
    better source contradicts."""
    store = Path(store) if store else config.meetings_dir()
    names = provenance.manifest_names(fj.read_json(config.MANIFEST_PATH) or {})
    rows = []
    for base in config.meeting_bases(store):
        j = config.meeting_file(base, ".json", store)
        d = fj.read_json(j)
        if d is None:
            continue
        audio = config.meeting_audio(base, store)
        raw = provenance.source_name(base, d, j, names)
        mt = provenance.source_mtime(d, audio)
        pr = provenance.probe_tags(audio) if (probe and audio is not None) else None
        cands = provenance.candidates(base, d, raw, mt, pr, today=today)
        view = dict(d)
        if not view.get("date_source"):
            view["date_source"] = provenance.explain_meeting(base, d, raw, mt, cands)[0]
        fix = provenance.correction(view, cands)
        if fix is None:
            continue
        src, iso, rec = fix
        rows.append({
            "base": base, "meeting_id": d.get("meeting_id"),
            "old": {"date": d.get("date"),
                    "date_source": fj.get(d, "date_source"),
                    "recorded_at": fj.get(d, "recorded_at")},
            "new": {"date": iso, "date_source": src, "recorded_at": rec},
            "days": _days(d.get("date"), iso),
            "skip": "being processed" if status.meeting_active(base) else None})
    return rows


def _days(a, b):
    try:
        return (date.fromisoformat(a) - date.fromisoformat(b)).days
    except (TypeError, ValueError):
        return None


def summarize_plan(rows: list, total: int) -> dict:
    c = Counter(r["new"]["date_source"] for r in rows if not r["skip"])
    return {"meetings": total,
            "corrections": sum(1 for r in rows if not r["skip"]),
            "by_source": dict(sorted(c.items())),
            "days_moved": sorted(abs(r["days"]) for r in rows
                                 if not r["skip"] and r["days"] is not None),
            "with_start_time": sum(1 for r in rows if not r["skip"]
                                   and r["new"]["recorded_at"]),
            "skipped": [{"why": r["skip"]} for r in rows if r["skip"]]}


def _ra_arg(v):
    """apply_meeting_edits' recorded_at for a journaled value."""
    return summarize.DROP if fj.is_missing(v) else v


def apply(store=None, journal_dir=None, run_id=None, probe=True, today=None) -> dict:
    run_id = run_id or datetime.now().strftime("fixdates-%Y%m%dT%H%M%S")
    jpath = fj.journal_path(run_id, journal_dir)
    applied, skipped = [], []
    with fj.Journal(jpath) as journal:
        for r in plan(store, probe=probe, today=today):
            if r["skip"]:
                skipped.append({"base": r["base"], "why": r["skip"]})
                continue
            journal.write({"run": run_id, "meeting_id": r["meeting_id"],
                           "base": r["base"], "old": r["old"], "new": r["new"],
                           "at": datetime.now().isoformat(timespec="seconds")})
            res = summarize.apply_meeting_edits(
                r["base"], date=r["new"]["date"], date_source=r["new"]["date_source"],
                recorded_at=r["new"]["recorded_at"])
            if res.get("ok"):
                applied.append({"base": r["base"], "now": res["base"]})
            else:
                skipped.append({"base": r["base"], "why": res.get("error")})
    return {"run": run_id, "journal": str(jpath), "applied": applied,
            "skipped": skipped}


def undo(run_id: str, store=None, journal_dir=None, dry_run=True) -> dict:
    by_id = fj.bases_by_id(store)
    live = set(config.meeting_bases(store))
    restored, kept, missing, skipped = [], [], [], []
    for e in reversed(fj.entries(run_id, journal_dir)):
        base = by_id.get(e.get("meeting_id")) or (e["base"] if e["base"] in live else None)
        if base is None:
            missing.append({"base": e["base"]})
            continue
        d = fj.read_json(config.meeting_file(base, ".json", store)) or {}
        if (d.get("date") != e["new"]["date"]
                or d.get("date_source") != e["new"]["date_source"]):
            kept.append({"base": base})
            continue
        if dry_run:
            restored.append({"base": base})
            continue
        old = e["old"]
        src = None if fj.is_missing(old["date_source"]) else old["date_source"]
        res = summarize.apply_meeting_edits(
            base, date=old["date"], date_source=src or "manual",
            recorded_at=_ra_arg(old["recorded_at"]))
        if not res.get("ok"):
            skipped.append({"base": base, "why": res.get("error")})
            continue
        if src is None:
            # the meeting had no date_source before the run: take it off again
            from stt import review
            nb = res["base"]
            with review.lock_meeting(nb):
                j = config.meeting_file(nb, ".json", store)
                dd = fj.read_json(j)
                if dd is not None and dd.get("date_source") == "manual":
                    dd.pop("date_source", None)
                    fj.atomic_json(j, dd)
        restored.append({"base": res["base"]})
    return {"run": run_id, "restored": restored, "kept": kept,
            "missing": missing, "skipped": skipped, "dry_run": dry_run}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--undo", metavar="RUN", help="undo the journaled run RUN")
    ap.add_argument("--no-probe", action="store_true", help="skip the audio tag read")
    ap.add_argument("--journal-dir", help="journal folder (default: config.migration_dir())")
    a = ap.parse_args(argv)
    if a.undo:
        r = undo(a.undo, journal_dir=a.journal_dir, dry_run=not a.apply)
        word = "would restore" if r["dry_run"] else "restored"
        print(f"undo {a.undo}: {word} {len(r['restored'])} dates, "
              f"kept {len(r['kept'])} changed since, {len(r['missing'])} gone, "
              f"skipped {len(r['skipped'])}")
        return 0
    probe = not a.no_probe
    if not a.apply:
        s = summarize_plan(plan(probe=probe), len(config.meeting_bases()))
        print(f"date fix dry run (nothing written): {s['meetings']} meetings, "
              f"{s['corrections']} corrections")
        print("  by source: " + (", ".join(f"{k} {v}" for k, v in s["by_source"].items())
                                 or "none"))
        print(f"  days moved: {s['days_moved']}")
        print(f"  with a recorder start time: {s['with_start_time']}")
        print(f"  skipped: {len(s['skipped'])}")
        return 0
    r = apply(journal_dir=a.journal_dir, probe=probe)
    print(f"date fix applied: run {r['run']}, {len(r['applied'])} corrected, "
          f"{len(r['skipped'])} skipped")
    print(f"journal: {r['journal']}")
    print(f"undo with: ./run.sh py tools/fix_dates.py --undo {r['run']} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

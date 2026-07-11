#!/usr/bin/env python
"""Backfill the folder-name convention onto meetings that predate it.

Every meeting folder should be named "<title> MMDDYYYY". The date lives in the
NAME so a recurring meeting ('LT Weekly Meeting') stays unique on disk and two of
them can never resolve to the same folder and overwrite each other. Meetings
processed before the pipeline stamped the date carry a plain name — and a plain
'LT Weekly Meeting' shadows the whole dated series.

  ./run.sh py tools/restamp_folders.py            # dry run: print the plan
  ./run.sh py tools/restamp_folders.py --apply    # do it

Each rename goes through summarize.apply_meeting_edits, so the move also follows
the speaker registries (or the ▶ voice clips die), the stored source_file, and
the manifest (or a kept original re-transcribes into a duplicate). A meeting the
batch is writing right now is skipped rather than forced.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, dates, summarize  # noqa: E402


def plan():
    """[(base, want|None, note)] for every meeting whose folder name does not
    already equal '<title> <its date>'."""
    rows = []
    for base in config.meeting_bases():
        try:
            d = json.loads(config.meeting_file(base, ".json").read_text())
        except (OSError, ValueError):
            rows.append((base, None, "unreadable json — skipped"))
            continue
        iso = d.get("date")
        if not iso:
            rows.append((base, None, "no stored date — skipped"))
            continue
        want = dates.restamp(base, iso)
        if want != base:
            rows.append((base, want, iso))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually re-stamp (default: dry run, changes nothing)")
    args = ap.parse_args()

    rows = plan()
    todo = [r for r in rows if r[1]]
    if not rows:
        print("Every meeting folder already carries its date. Nothing to do.")
        return 0

    print(f"{'FOLDER NOW':<44}    {'FOLDER AFTER':<44}")
    print("-" * 92)
    for base, want, note in rows:
        print(f"{base[:43]:<44} -> {(want or note)[:43]:<44}")
    print("-" * 92)
    print(f"{len(todo)} to re-stamp, {len(rows) - len(todo)} skipped.")

    if not args.apply:
        print("\nDRY RUN — nothing was changed. Re-run with --apply to do it.")
        return 0

    ok = fail = 0
    for base, _, iso in todo:
        # passing the date it ALREADY has is what triggers the re-stamp: the
        # folder is rebuilt as "<title> <date>" and every reference follows
        r = summarize.apply_meeting_edits(base, date=iso)
        if r.get("ok"):
            print(f"  ok  {base}  ->  {r['base']}")
            ok += 1
        else:
            print(f"  --  {base}: {r.get('error')}", file=sys.stderr)
            fail += 1
    print(f"\n{ok} re-stamped, {fail} skipped.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

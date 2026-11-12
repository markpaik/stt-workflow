#!/usr/bin/env python
"""Bring the transcript mirror folder (STT_MIRROR_DIR) in line with the
whole library.

  ./run.sh py tools/mirror.py            # dry run: print what would change
  ./run.sh py tools/mirror.py --apply    # do it

The first --apply copies every meeting there: the backfill. After that the
batch keeps the folder current at the end of each run, and each panel edit
updates its own meeting's copy, so this tool is for the first copy and for
a check. The rules are stt/mirror.py's: one "<base>.txt" byte copy per live
meeting, and never one of a strict meeting. A file the mirror did not
write, or one that changed after it wrote it, is left as it is.

It exits 1 when the mirror is off, when the setting cannot be used, or when
the folder cannot be read or written.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, mirror  # noqa: E402

NOTES = {"blocked": "a file the mirror did not write holds this name",
         "changed": "changed after the mirror wrote it, so it is left as it is"}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write, update and remove files (default: a dry run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change and write nothing (the default)")
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run cannot be used together")

    why = mirror.setting_problem()
    if why:
        print(f"{mirror.SETTING}={why}.", file=sys.stderr)
        return 1
    ctx = mirror.context()
    if ctx is None:
        print(f"{mirror.SETTING} is not set in stt.env, so the mirror is off.",
              file=sys.stderr)
        return 1
    print(f"Mirror folder: {ctx.mirror}")
    print(f"Library:       {ctx.store}")
    r = mirror.reconcile(dry_run=not args.apply, ctx=ctx)
    if r.get("problem"):
        print(f"\n{r['problem']}. Nothing more was done.", file=sys.stderr)
        return 1
    c = mirror.counts(r)
    for o in r["ops"]:
        if o["op"] in ("write", "update", "remove", "blocked", "changed"):
            note = f"  ({NOTES[o['op']]})" if o["op"] in NOTES else ""
            print(f"  {o['op']:<8} {o['name']}{note}")
    verb = (("Wrote", "updated", "removed") if args.apply
            else ("Would write", "update", "remove"))
    print(f"\n{verb[0]} {r['write']}, {verb[1]} {r['update']}, {verb[2]} "
          f"{r['remove']}. {c.get('keep', 0)} unchanged. "
          f"{c.get('blocked', 0)} blocked, {c.get('changed', 0)} changed. "
          f"{c.get('strict', 0)} strict meeting(s) stay on this Mac.")
    if not args.apply:
        print("Dry run: nothing was written. Run with --apply to do it.")
    return 0


if __name__ == "__main__":
    config.private_umask()
    sys.exit(main())

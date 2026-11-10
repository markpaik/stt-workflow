#!/usr/bin/env python
"""Check the voice registries and the meeting store, and repair what is
safe to repair (stt/doctor.py).

  ./run.sh py tools/doctor.py check                 # report, writes nothing
  ./run.sh py tools/doctor.py check --deep          # also probe audio lengths
  ./run.sh py tools/doctor.py check --counts        # counts only, no names
  ./run.sh py tools/doctor.py fix                   # dry run of the repairs
  ./run.sh py tools/doctor.py fix --apply           # copy, journal, repair
  ./run.sh py tools/doctor.py undo RUN              # dry run of the undo
  ./run.sh py tools/doctor.py undo RUN --apply

Exit status is 1 when findings remain after `check`, so a script can gate
on it. tools/registry_doctor.py still checks the registries alone.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import doctor  # noqa: E402


def _print(findings, counts_only):
    if not findings:
        print("  clean: nothing to report")
        return
    if not counts_only:
        for f in findings:
            mark = "fixable" if f.fixable else "report "
            print(f"  [{mark}] {f.kind:<20} {f.subject}: {f.detail}")
    c = Counter(f.kind for f in findings)
    print("  " + ", ".join(f"{k}: {n}" for k, n in sorted(c.items())))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--deep", action="store_true")
    c.add_argument("--counts", action="store_true", help="counts only, no names")
    f = sub.add_parser("fix")
    f.add_argument("--apply", action="store_true")
    f.add_argument("--journal-dir")
    u = sub.add_parser("undo")
    u.add_argument("run")
    u.add_argument("--apply", action="store_true")
    u.add_argument("--journal-dir")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        fs = doctor.check(deep=a.deep)
        print("Doctor check (nothing written):")
        _print(fs, a.counts)
        return 1 if fs else 0
    if a.cmd == "fix":
        r = doctor.fix(dry_run=not a.apply, journal_dir=a.journal_dir)
        p = r["plan"]
        word = "would repair" if r["dry_run"] else "repaired"
        print(f"Doctor fix ({'dry run, nothing written' if r['dry_run'] else 'applied'}):")
        print(f"  registry findings {word}: {p['registry']}")
        print("  manifest actions: " + (", ".join(f"{k} {v}" for k, v in
                                                 sorted(p["manifest"].items())) or "none"))
        print(f"  stale holds: {p['holds']}, stale dismissals: {p['dismissals']}, "
              f"stale stopping.flag: {p['stop_flag']}")
        print(f"  logs over the cap: {p['logs']}, calibration over the cap: {p['calibration']}")
        if r.get("error"):
            print(f"  refused: {r['error']}")
            return 2
        if not r["dry_run"]:
            for line in r["done"]:
                print(f"  {line}")
            print(f"  journal: {r['journal']}")
            print(f"  undo with: ./run.sh py tools/doctor.py undo {r['run']} --apply")
        return 0
    from stt import fieldjournal
    try:
        r = doctor.undo(a.run, journal_dir=a.journal_dir, dry_run=not a.apply)
    except fieldjournal.NoSuchRun as e:
        print(e, file=sys.stderr)
        return 1
    word = "would restore" if r["dry_run"] else "restored"
    print(f"undo {a.run}: {word} {len(r['restored'])} file(s), "
          f"kept {len(r['kept'])} changed since")
    return 0


if __name__ == "__main__":
    sys.exit(main())

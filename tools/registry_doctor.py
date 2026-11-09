#!/usr/bin/env python
"""Report — and, with --fix, repair — defects in the voice registries.

    python tools/registry_doctor.py            # report only, writes nothing
    python tools/registry_doctor.py --fix      # apply the safe repairs
    python tools/registry_doctor.py --fix --voiceprints DIR
                                               # check AND repair DIR only

What it looks for: one voice holding two global numbers or two enrolled
profiles (duplicate), a global number whose stored samples no longer agree
with each other and so span two people (split), a sample source or meeting
reference that names nothing on disk (dangling), a lock file for a meeting
that can never lock under that key again (orphan_lock), and an unknowns
counter sitting below its own high-water mark (stale_next). `invalid` covers
the structural checks — a missing .npy, a sample count that disagrees with the
rows on disk, a row that is not a finite unit vector, an entry of the wrong
shape. `corrupt` is a registry.json or unknowns.json that cannot be read, and
`unreferenced_file` is a .npy that no registry entry points at. Neither is
fixed: an unreferenced file may be someone's only voiceprint.

--fix merges duplicate global numbers, moves a split stack's minority samples
onto fresh numbers, drops dead references, prunes orphan locks, and heals the
counter. It deliberately does NOT merge two enrolled people who share a voice:
that is either a mis-enrolled sample or one person under two spellings, and
only you can say which. Exit status is 1 whenever findings remain, so a cron
or a CI step can gate on it.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import registry_doctor  # noqa: E402


def report(findings, header):
    print(header)
    if not findings:
        print("  clean — nothing to report")
        return
    for f in findings:
        mark = "fixable" if f.fixable else "manual "
        print(f"  [{mark}] {f.kind:<11} {registry_doctor.format_finding(f)}")
    counts = Counter(f.kind for f in findings)
    print("  " + ", ".join(f"{k}: {n}" for k, n in sorted(counts.items())))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true",
                    help="apply the safe repairs (default: report only)")
    ap.add_argument("--voiceprints", default=None,
                    help="voiceprints directory to check and, with --fix, to "
                         "repair; the configured one is never written when "
                         "this names another (default: the configured one)")
    ap.add_argument("--meetings", default=None,
                    help="meetings directory (default: the configured one)")
    args = ap.parse_args()

    findings = registry_doctor.check(args.voiceprints, args.meetings)
    report(findings, "Registry check:")
    if not args.fix:
        return 1 if findings else 0

    done = registry_doctor.fix(findings, args.voiceprints, args.meetings)
    print("\nRepairs:")
    for line in done or ["nothing to do"]:
        print(f"  {line}")
    left = registry_doctor.check(args.voiceprints, args.meetings)
    report(left, "\nAfter the repairs:")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())

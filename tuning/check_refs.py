#!/usr/bin/env python
"""Validate Scribe reference exports and match them to processed meetings.

  ./run.sh py tuning/check_refs.py
"""
import sys
from pathlib import Path

from stt import config
from tuning import eval as E

REF_DIR = config.PROJECT_DIR / "qa" / "scribe_refs"


def _norm(s):
    return "".join(c.lower() for c in s if c.isalnum())


def main():
    REF_DIR.mkdir(parents=True, exist_ok=True)
    refs = sorted(REF_DIR.glob("*.json"))
    meetings = {p.stem: p for p in config.MEETINGS_DIR.glob("*.json")}
    if not refs:
        print(f"No reference JSONs in {REF_DIR} yet.")
        print("Export Scribe JSON (words + speaker_id + timestamps) named like the meeting.")
        return 1

    print(f"{'REF FILE':<40} {'parse':>6} {'words':>6} {'spk':>4}  matched meeting")
    print("-" * 92)
    ok = 0
    for r in refs:
        try:
            ref = E.parse_scribe(str(r))
            nw, ns = len(ref["words"]), len(ref["by_speaker"])
            status = "OK" if nw > 0 and ns > 0 else "EMPTY"
        except Exception as e:
            print(f"{r.name:<40} {'FAIL':>6}  ({type(e).__name__}: {e})")
            continue
        # match to a processed meeting by normalized stem
        match = next((m for m in meetings if _norm(r.stem) == _norm(m)), None)
        if not match:
            match = next((m for m in meetings if _norm(r.stem) in _norm(m)
                          or _norm(m) in _norm(r.stem)), None)
        print(f"{r.name:<40} {status:>6} {nw:>6} {ns:>4}  "
              f"{match or '!! NO MATCH — rename to the meeting basename'}")
        if status == "OK" and match:
            ok += 1
    print(f"\n{ok} reference(s) ready to tune against "
          f"(need >=3 with speaker labels for a tune/validate split).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

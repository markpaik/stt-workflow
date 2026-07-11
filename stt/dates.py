"""Parse the meeting date out of a recording's filename.

The naming convention embeds dates as digit runs — "LT Meeting 05212026" is
May 21 2026 (MMDDYYYY). YYYYMMDD is also accepted. Returns an ISO date string
or None; callers fall back to file mtime.
"""
import re
from datetime import date

_DIGITS8 = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def _valid(y, m, d):
    if not (2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _run_date(run: str):
    """ISO date for ONE 8-digit run, else None. MMDDYYYY is the house
    convention; YYYYMMDD accepted when unambiguous."""
    mmddyyyy = _valid(int(run[4:8]), int(run[0:2]), int(run[2:4]))
    if mmddyyyy:
        return mmddyyyy
    return _valid(int(run[0:4]), int(run[4:6]), int(run[6:8]))


def meeting_date(name: str):
    """ISO date parsed from an 8-digit run in `name`, else None."""
    for run in _DIGITS8.findall(name):
        iso = _run_date(run)
        if iso:
            return iso
    return None


# --- The folder-name convention: "<title> MMDDYYYY" ---
# The date lives IN the folder name so recurring meetings ("LT Weekly Meeting")
# stay unique on disk and can never overwrite each other; the panel strips it
# back off for display. These three are the single definition of that rule —
# the pipeline, rename, the date editor, the recorder, and the panel all go
# through them, so the name and the stored date can't drift apart.

_TRAILING_STAMP = re.compile(r"\s+(\d{8})$")


def strip_stamp(name: str) -> str:
    """'Weekly Check-in 07102026' -> 'Weekly Check-in'. Only strips a TRAILING
    8-digit run that really parses as a date (so 'Case 99999999' is left alone),
    and never reduces a name to nothing (a bare '07102026' stays as it is)."""
    m = _TRAILING_STAMP.search(name)
    if m and meeting_date(m.group(1)) is not None:
        return name[:m.start()].rstrip() or name
    return name


def stamp(name: str, iso: str) -> str:
    """Append a date as MMDDYYYY, so meeting_date() parses it straight back."""
    try:
        return f"{name} {date.fromisoformat(iso).strftime('%m%d%Y')}"
    except (ValueError, TypeError):
        return name


_UNIQ_SUFFIX = re.compile(r"\s+\(\d+\)$")


def restamp(base: str, iso: str) -> str:
    """The folder name this meeting SHOULD have: its title carrying `iso` as its
    one and only date.

    A name that ALREADY has a date gets it replaced IN PLACE, wherever it sits.
    Stripping only a TRAILING stamp missed the recorder's own default name —
    'Recording 07112026 1814' puts the date in the MIDDLE, so re-stamping
    appended a second one and produced 'Recording 07112026 1814 07112026'.
    A ' (N)' twin suffix is peeled first (the caller's uniquify re-adds it if the
    new name still collides). Idempotent: restamping with the same date is a
    no-op."""
    stem = _UNIQ_SUFFIX.sub("", base)
    try:
        new = date.fromisoformat(iso).strftime("%m%d%Y")
    except (ValueError, TypeError):
        return stem
    for m in _DIGITS8.finditer(stem):
        if _run_date(m.group(1)) is not None:   # the first REAL date in the name
            return stem[:m.start()] + new + stem[m.end():]
    return stamp(stem, iso)                      # no date yet: append one

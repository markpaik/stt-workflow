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


def meeting_date(name: str):
    """ISO date parsed from an 8-digit run in `name`, else None.
    MMDDYYYY is the house convention; YYYYMMDD accepted when unambiguous."""
    for run in _DIGITS8.findall(name):
        mmddyyyy = _valid(int(run[4:8]), int(run[0:2]), int(run[2:4]))
        if mmddyyyy:
            return mmddyyyy
        yyyymmdd = _valid(int(run[0:4]), int(run[4:6]), int(run[6:8]))
        if yyyymmdd:
            return yyyymmdd
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


def restamp(base: str, iso: str) -> str:
    """The folder name this meeting SHOULD have: its title plus its date. Both
    idempotent and self-correcting — re-stamping an already-stamped name with a
    corrected date replaces the old stamp rather than appending a second one."""
    return stamp(strip_stamp(base), iso)

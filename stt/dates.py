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

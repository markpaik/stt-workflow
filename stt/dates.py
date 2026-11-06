"""Parse the meeting date out of a recording's filename.

The naming convention puts the date FIRST, in ISO form: "2026-05-21 LT
Meeting" is May 21 2026. Names from before that rule carry the date as an
8-digit run, "LT Meeting 05212026" (MMDDYYYY, YYYYMMDD also accepted), and
both forms are read everywhere a name is read. Returns an ISO date string
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
    """ISO date for ONE 8-digit run, else None. MMDDYYYY was the house
    convention; YYYYMMDD accepted when unambiguous."""
    mmddyyyy = _valid(int(run[4:8]), int(run[0:2]), int(run[2:4]))
    if mmddyyyy:
        return mmddyyyy
    return _valid(int(run[0:4]), int(run[4:6]), int(run[6:8]))


# the canonical stamp: an ISO date that opens the name, then a space
_LEADING_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+|$)")


def _leading_iso(name: str):
    """(iso, rest) when `name` opens with a real ISO date, else None."""
    m = _LEADING_ISO.match(str(name or ""))
    if not m:
        return None
    iso = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (iso, str(name)[m.end():]) if iso else None


def meeting_date(name: str):
    """ISO date parsed from `name`: a leading ISO stamp first, else an
    8-digit run anywhere in it (the older form). None when neither parses."""
    lead = _leading_iso(name)
    if lead:
        return lead[0]
    for run in _DIGITS8.findall(name):
        iso = _run_date(run)
        if iso:
            return iso
    return None


# --- The folder-name convention: "YYYY-MM-DD <title>" ---
# The date lives IN the folder name so recurring meetings ("LT Weekly Meeting")
# stay unique on disk and can never overwrite each other; the panel strips it
# back off for display. A same-name same-date twin is "YYYY-MM-DD <title> (2)".
# Folders named before this rule read "<title> MMDDYYYY", and every function
# here reads that form too, so an old folder, a typed name or a dropped file
# still parses. Only the ISO form is ever WRITTEN. These functions are the
# single definition of that rule — the pipeline, rename, the date editor, the
# recorder, and the panel all go through them, so the name and the stored date
# can't drift apart.

_TRAILING_STAMP = re.compile(r"\s+(\d{8})$")


def _trailing_stamp(name: str):
    """(iso, head) when `name` ends with an 8-digit run that parses as a
    date (the older form), else None."""
    m = _TRAILING_STAMP.search(str(name or ""))
    if m:
        iso = meeting_date(m.group(1))
        if iso is not None:
            return iso, str(name)[:m.start()].rstrip()
    return None


def stamp_date(name: str):
    """The date the name's STAMP carries: a leading ISO date, else a
    trailing 8-digit stamp. None when the name has no stamp. A date
    elsewhere in the name is not a stamp ('Recording 07112026 1814' has
    none). Peel a ' (N)' twin suffix first (title_of does)."""
    hit = _leading_iso(name) or _trailing_stamp(name)
    return hit[0] if hit else None


def strip_stamp(name: str) -> str:
    """'2026-07-10 Weekly Check-in' -> 'Weekly Check-in', and the older
    'Weekly Check-in 07102026' -> 'Weekly Check-in'. A leading ISO stamp
    is the stamp when there is one, and then the end of the name is left
    alone ('2026-11-01 Retreat 12312026' -> 'Retreat 12312026'). Only a run
    that really parses as a date is a stamp (so 'Case 99999999' is left
    alone), and a name is never reduced to nothing (a bare '07102026' or
    '2026-07-10' stays as it is)."""
    hit = _leading_iso(name) or _trailing_stamp(name)
    if hit:
        return hit[1].strip() or name
    return name


def stamp(name: str, iso: str) -> str:
    """Put a date in front of a name, ISO first ('2026-07-10 Weekly'), so
    meeting_date() parses it straight back. An invalid date leaves the name
    as it is."""
    try:
        return f"{date.fromisoformat(iso).isoformat()} {name}"
    except (ValueError, TypeError):
        return name


_UNIQ_SUFFIX = re.compile(r"\s+\(\d+\)$")


def title_of(base: str) -> str:
    """The meeting's `title` key: its folder name with the twin suffix and
    the date stamp removed. stamp(title_of(b), date) + twin_suffix(b) gives
    an ISO-form folder name back."""
    return strip_stamp(_UNIQ_SUFFIX.sub("", str(base or "").strip()))


def twin_suffix(base: str) -> str:
    """' (2)' for a same-name twin folder, else ''."""
    m = _UNIQ_SUFFIX.search(str(base or ""))
    return m.group(0) if m else ""


def is_iso_form(base: str) -> bool:
    """True when the folder name already follows the ISO-first rule."""
    return _leading_iso(_UNIQ_SUFFIX.sub("", str(base or "").strip())) is not None


# --- Short typed dates: '9.14', '10-5', '9/14', '9.14.26' ---
# A person typing a label on the phone writes the day short. The audit found
# 7 such labels, and each disagreed with the stored transfer date by 9 to 25
# days. A short date has no year, so it resolves to its most recent day on or
# before a reference day (the transfer day), and only within SHORT_MAX_DAYS:
# further back than that, the digits are more likely a time or a count than
# the day of the meeting.
#
# Only a short date at the END of the title counts: every real label puts it
# there, and a date-like run mid-title is usually something else ('Grades
# 9-12 Planning', '1:1 with Lead', 'v1.2 demo'). A colon is not a date
# separator ('Standup 9:30' is a time), and a letter right before the digits
# ('v1.2') rules the run out.
_SHORT = re.compile(
    r"(?<![\w.:/\-])(\d{1,2})([.\-/])(\d{1,2})(?:\2(\d{2}|\d{4}))?\s*$")
SHORT_MAX_DAYS = 60

# A trailing run right after one of these words is a range or a count, not a
# day: 'Math PLC Grades 6-8' is grades six to eight, never June 8. The word
# is matched whole, case-blind, with an optional trailing period ('Gr.').
_RANGE_WORDS = frozenset("""
    grade grades gr grs k pk tk ages age yrs years
    ch chapter chapters unit units lesson lessons module modules
    week weeks wk wks page pages pg pgs pp level levels tier tiers
    period periods session sessions part parts section sections
    step steps days room rooms v ver version rev no
    """.split())
_WORD_BEFORE = re.compile(r"(\w+)\.?\s*$")
# another range earlier in the title ('Grades K-2 and 3-5', '6-8, 9-12')
# means the trailing one is a range too
_RANGE_TOKEN = re.compile(r"(?<![\w.:/\-])(?:[A-Za-z]|\d{1,2})\s*[\-\u2013/]\s*\d{1,2}(?![\w.:/\-])")


def _is_range(head: str) -> bool:
    """True when the text before a trailing short run marks it as a range or
    a count: a range word right before it, or another range earlier on."""
    w = _WORD_BEFORE.search(head)
    if w and w.group(1).lower() in _RANGE_WORDS:
        return True
    return bool(_RANGE_TOKEN.search(head))


def short_date(name: str, ref_iso: str):
    """ISO date for the short typed date that ends `name`, resolved against
    the reference day `ref_iso`, else None.

    '9.14' read on 2026-09-29 is 2026-09-14. '12.30' read on 2027-01-08 is
    2026-12-30: a year boundary resolves to the past. An explicit year
    ('9.14.26') is taken as written. A result after the reference day, or
    more than SHORT_MAX_DAYS before it, is rejected. 8-digit stamps never
    match, because the pattern needs a separator. A run that reads as a
    range or a count ('Grades 6-8', 'Units 3/4', 'K-2 and 3-5') is
    rejected (_is_range)."""
    try:
        ref = date.fromisoformat(str(ref_iso)[:10])
    except (TypeError, ValueError):
        return None
    text = str(name or "").rstrip()
    for m in _SHORT.finditer(text):
        mo, dy, yr = int(m.group(1)), int(m.group(3)), m.group(4)
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            continue
        if _is_range(text[:m.start()]):
            continue
        cands = []
        if yr:
            y = int(yr) + (2000 if len(yr) == 2 else 0)
            cands.append(y)
        else:
            cands += [ref.year, ref.year - 1]
        for y in cands:
            try:
                d = date(y, mo, dy)
            except ValueError:
                continue
            if d <= ref and (ref - d).days <= SHORT_MAX_DAYS:
                return d.isoformat()
            if yr:
                break
    return None


def restamp(base: str, iso: str) -> str:
    """The folder name this meeting SHOULD have: its title with `iso` in
    front as its one and only date, "YYYY-MM-DD <title>".

    The old date is removed wherever the older form put it: a leading ISO
    stamp, a trailing 8-digit stamp, or, failing both, the first real
    8-digit date that stands as its own word inside the name. That last
    case is the recorder's old default, 'Recording 07112026 1814', which put
    the date in the MIDDLE: it becomes '2026-07-11 Recording 1814'. A run
    glued to other text ('Scan 20260715-160418') stays in the title. A name with a leading ISO stamp
    has only that stamp replaced. A ' (N)' twin suffix is peeled first (the
    caller's uniquify re-adds it if the new name still collides). An
    invalid `iso` returns the peeled name unchanged. Idempotent: restamping
    with the same date is a no-op."""
    stem = _UNIQ_SUFFIX.sub("", base)
    try:
        new = date.fromisoformat(iso).isoformat()
    except (ValueError, TypeError):
        return stem
    hit = _leading_iso(stem) or _trailing_stamp(stem)
    if hit:
        return stamp(hit[1].strip() or stem, new)
    for m in _DIGITS8.finditer(stem):
        if _run_date(m.group(1)) is not None:   # the first REAL date in the name
            # only a date that stands as its own word is removed: in
            # 'Scan 20260715-160418' the run is part of a timestamp, so it
            # stays in the title and the date just goes in front
            if (m.start() == 0 or stem[m.start() - 1].isspace()) and \
                    (m.end() == len(stem) or stem[m.end()].isspace()):
                rest = re.sub(r"\s+", " ", stem[:m.start()] + " " + stem[m.end():]).strip()
                return stamp(rest or stem, new)
            break
    return stamp(stem, new)                      # no date yet: put one in front


def canonical(name: str, iso: str, today: str = None) -> str:
    """The ISO-form folder name for a TYPED or arriving name and the
    meeting's date `iso`.

    A stamp the name carries (leading ISO or trailing 8-digit) is the
    meeting date, so it is replaced by `iso` (the caller decides whether
    the typed date or an explicit one wins). The one exception is a stamp
    in the future that differs from `iso`: 'Planning Retreat 12312026'
    describes an event, not when it was recorded, so the digits stay in
    the title and the date goes in front. A name with no stamp gets
    restamp(). Without a valid `iso`, a stamped name is converted using
    its own stamp, and a plain name is returned as it is."""
    stem = _UNIQ_SUFFIX.sub("", str(name or "").strip())
    sd = stamp_date(stem)
    try:
        iso = date.fromisoformat(str(iso)).isoformat()
    except (ValueError, TypeError):
        iso = None
    if iso is None:
        return restamp(stem, sd) if sd else stem
    if sd and sd != iso and sd > (today or date.today().isoformat()):
        return stamp(stem, iso)
    return restamp(stem, iso)

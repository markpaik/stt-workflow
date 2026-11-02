"""Where a meeting's name and date came from (unified plan section 2 and
Phase 3).

Every function here READS. The backfill tool, the date-correction tool, and
the doctor all build on these, so one definition decides what a meeting's
`title`, `source_name`, `audio_fp`, `date_source`, and `recorded_at` are.

Date sources, best first (design-files.md section 5):

- ``typed``: an 8-digit date in the raw arrival name that is not in the
  future.
- ``recorder``: a start time the recorder wrote. The native recorder's own
  name ('Recording 07112026 1814'), or a Voice Memos `creation_time` tag on
  a file that carries `voice-memo-uuid` AND passes the per-file check: the
  tag plus the audio length must end no later than TAG_TOLERANCE_SEC after
  the source file's modification time. A tag near the export time fails,
  because the recording would then end after the file was written.
- ``short_typed``: a short date such as '9.14' in the label or title.
- ``mtime``: the day the source file was last modified, which for a voice
  memo is usually the transfer day.
- ``manual``: a person set the date in the panel.
"""
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, dates

DATE_SOURCES = ("typed", "recorder", "short_typed", "mtime", "manual")
UNCERTAIN = ("mtime", "short_typed")
TAG_TOLERANCE_SEC = 60.0
KEYS = ("title", "source_name", "audio_fp", "date_source", "recorded_at")

_NATIVE = re.compile(r"^recording\s+(\d{8})\s+(\d{2})(\d{2})(?:\s+\(\d+\))?$", re.I)


# ---------- probing ----------

def probe_tags(audio) -> dict:
    """{'tags': {lower-case key: value}, 'duration': seconds or None} from
    ffprobe. Reads the container header only. Empty on any failure."""
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                            "-show_format", str(audio)],
                           capture_output=True, text=True, timeout=60)
        fmt = json.loads(r.stdout or "{}").get("format") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"tags": {}, "duration": None}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    try:
        dur = float(fmt.get("duration"))
    except (TypeError, ValueError):
        dur = None
    return {"tags": tags, "duration": dur}


def _parse_tag_time(s):
    try:
        t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def tag_start(probe: dict, duration, source_mtime):
    """The recorder start time a Voice Memos tag proves, as an aware local
    datetime, or None.

    Only a file carrying `voice-memo-uuid` counts, and only when the tag
    plus the audio length ends no later than TAG_TOLERANCE_SEC after the
    source modification time. On older exports the tag is the export time,
    which fails this check because the recording would end after the file
    was written."""
    tags = (probe or {}).get("tags") or {}
    if "voice-memo-uuid" not in tags or source_mtime is None:
        return None
    t = _parse_tag_time(tags.get("creation_time"))
    if t is None:
        return None
    dur = duration if duration is not None else (probe or {}).get("duration")
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        return None
    if t.timestamp() + dur > float(source_mtime) + TAG_TOLERANCE_SEC:
        return None
    return t.astimezone()


# ---------- the keys ----------

def manifest_names(manifest: dict) -> dict:
    """{meeting json path: raw arrival name} from the manifest's records.
    The manifest key IS the raw name, and its outputs follow every rename."""
    out = {}
    for key, rec in ((manifest or {}).get("processed") or {}).items():
        for o in rec.get("outputs") or []:
            if str(o).endswith(".json"):
                out[str(o)] = key
    return out


def source_name(base: str, d: dict, json_path, names: dict):
    """The raw arrival name, or None when no record keeps it.

    The stored key wins. Then the manifest's record of this meeting. Then
    `source_file`, but only on a meeting that was never renamed (a rename
    overwrites `source_file` with the new folder name) and never redone (a
    Redo writes the stored audio's name, which carries the folder name).

    A Redo shows as `arrived_at` different from `processed_at`. A meeting
    from before `arrived_at` existed gives no such proof, so its
    `source_file` counts only when it is not the folder name itself."""
    if d.get("source_name"):
        return d["source_name"]
    hit = names.get(str(json_path))
    if hit:
        return hit
    sf = d.get("source_file")
    if not sf or d.get("renamed_from"):
        return None
    arrived, processed = d.get("arrived_at"), d.get("processed_at")
    if arrived:
        return sf if arrived == processed else None
    stem = os.path.splitext(str(sf))[0]
    if stem == dates._UNIQ_SUFFIX.sub("", str(base or "")) or stem == base:
        return None
    return sf


def _local_day(ts):
    try:
        return date.fromtimestamp(float(ts)).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def source_mtime(d: dict, audio):
    """The source file's modification time: the stored `source_mtime`, else
    the stored audio's mtime (the batch copies with copy2, which keeps it)."""
    if d.get("source_mtime") is not None:
        try:
            return float(d["source_mtime"])
        except (TypeError, ValueError):
            pass
    if audio is not None:
        try:
            return Path(audio).stat().st_mtime
        except OSError:
            return None
    return None


def candidates(base: str, d: dict, raw_name, mtime, probe=None, today=None) -> dict:
    """{source: (iso_date, recorded_at or None)} for every date source this
    meeting has evidence for. `probe` is probe_tags() of its audio, or None
    to skip the tag."""
    today = today or date.today().isoformat()
    out = {}
    stem = os.path.splitext(raw_name)[0] if raw_name else None
    if stem:
        typed = dates.meeting_date(stem)
        m = _NATIVE.match(stem)
        if m and typed:
            iso = typed
            try:
                t = datetime.fromisoformat(f"{iso}T{m.group(2)}:{m.group(3)}:00").astimezone()
                out["recorder"] = (iso, t.isoformat(timespec="seconds"))
            except ValueError:
                pass
        elif typed and typed <= today:
            out["typed"] = (typed, None)
    if "recorder" not in out and probe is not None:
        t = tag_start(probe, d.get("duration_sec"), mtime)
        if t is not None:
            out["recorder"] = (t.date().isoformat(), t.isoformat(timespec="seconds"))
    mday = _local_day(mtime) if mtime is not None else None
    ref = mday or d.get("date")
    if ref:
        # the title only, never a raw name the title no longer shows: a
        # rename that dropped a typed '7.15' replaced the label, and the
        # date it kept was the one a person saw last
        s = dates.short_date(dates.title_of(base), ref)
        if s:
            out["short_typed"] = (s, None)
    if mday:
        out["mtime"] = (mday, None)
    return out


def best(cands: dict):
    """(source, iso, recorded_at) of the best candidate, or None."""
    for src in ("typed", "recorder", "short_typed", "mtime"):
        if src in cands:
            return (src,) + tuple(cands[src])
    return None


def explain(stored_iso, cands: dict, raw_known=True, mtime_known=True,
            folder_iso=None):
    """(date_source, recorded_at) that explains the date ALREADY stored.

    The pipeline only ever chose a typed date, else the file time. So when
    the raw name and the source file time are both on record, a stored date
    that neither gives came from a person (`manual`), and so did a stored
    date that disagrees with a typed one.

    The evidence is thinner on older meetings, and the label then stays on
    the cautious side:
    - raw name unknown (a rename overwrote it), no automatic source
      matches, and the folder's trailing stamp (`folder_iso`) carries the
      stored date: the date was typed, in the original name or in a rename
      (`typed`).
    - source file time unknown and no other source matches: the date came
      from a file time this record no longer holds, or from a person. It is
      labeled `mtime`, so the panel shows it as uncertain."""
    if not stored_iso:
        return None, None
    if "typed" in cands:
        if cands["typed"][0] == stored_iso:
            return "typed", None
        return "manual", None
    for src in ("recorder", "short_typed", "mtime"):
        if src in cands and cands[src][0] == stored_iso:
            return src, cands[src][1]
    if not raw_known and folder_iso == stored_iso:
        return "typed", None
    if not raw_known or not mtime_known:
        return "mtime", None
    return "manual", None


def explain_meeting(base: str, d: dict, raw, mtime, cands: dict):
    """explain() with this meeting's own evidence flags filled in."""
    folder = dates._UNIQ_SUFFIX.sub("", base)
    folder_iso = dates.meeting_date(folder[-8:]) if dates.strip_stamp(folder) != folder else None
    return explain(d.get("date"), cands, raw_known=raw is not None,
                   mtime_known=d.get("source_mtime") is not None,
                   folder_iso=folder_iso)


def correction(d: dict, cands: dict):
    """(source, iso, recorded_at) when the stored date should change, else
    None. A typed or manual date is never overridden. Only a better source
    than the file time (the recorder tag or a short typed date) corrects."""
    cur = d.get("date_source")
    if cur in ("typed", "manual"):
        return None
    if "typed" in cands:
        return None
    for src in ("recorder", "short_typed"):
        if src in cands:
            iso, rec = cands[src]
            if iso != d.get("date"):
                return (src, iso, rec)
            return None
    return None


def derive(base: str, d: dict, store=None, names=None, probe=True, today=None) -> dict:
    """{key: value} for every key in KEYS that this meeting lacks. Keys it
    already carries are never recomputed: `audio_fp` and `source_name` are
    frozen at first write. The one exception is a `source_name` stored as
    null: once a manifest record links the meeting (a doctor relink after
    the backfill), the raw name it gives fills the null."""
    store = Path(store) if store else config.meetings_dir()
    j = config.meeting_file(base, ".json", store)
    audio = config.meeting_audio(base, store)
    out = {}
    if "title" not in d:
        out["title"] = dates.title_of(base)
    raw = source_name(base, d, j, names or {})
    if "source_name" not in d:
        out["source_name"] = raw
    elif d.get("source_name") is None and raw is not None:
        out["source_name"] = raw
    if "audio_fp" not in d and audio is not None:
        from . import dupes
        fp = dupes.fingerprint(audio)
        if fp:
            out["audio_fp"] = fp
    if "date_source" not in d:
        mt = source_mtime(d, audio)
        pr = probe_tags(audio) if (probe and audio is not None) else None
        cands = candidates(base, d, raw, mt, pr, today=today)
        src, rec = explain_meeting(base, d, raw, mt, cands)
        if src:
            out["date_source"] = src
            if "recorded_at" not in d:
                out["recorded_at"] = rec
    elif "recorded_at" not in d:
        out["recorded_at"] = None
    return out

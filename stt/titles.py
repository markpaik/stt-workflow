"""Title rules: where a meeting's title came from, how settled it is, and
which route brought the recording in.

One set of names, used by the pipeline, the panel, the amnesty and the
metrics tool (docs: qa/rethink-2026-11-01/rethink/unified-plan.md section 2):

- ``title_state``: ``needs_title`` (only the model's draft exists),
  ``provisional`` (a typed label, a public download, or the amnesty), or
  ``confirmed`` (a person kept or edited it). A missing key means a meeting
  from before this rule, and the panel falls back to ``reviewed``.
- ``title_source``: ``label``, ``shortcut``, ``public_download``,
  ``calendar``, ``model``, ``manual``.
- ``route``: ``voice_memo``, ``native``, ``conference``,
  ``public_download``, ``other``.

``reviewed`` keeps its own meaning (a person looked at the meeting). Nothing
here ever sets it: a provisional title is not a human check.
"""
import json
import os
import re
from pathlib import Path

from . import config, dates

TITLE_STATES = ("needs_title", "provisional", "confirmed")
TITLE_SOURCES = ("label", "shortcut", "public_download", "calendar", "model", "manual")
ROUTES = ("voice_memo", "native", "conference", "public_download", "other")

# every key the pipeline carries across a Redo. A Redo rebuilds the meeting's
# metadata from scratch, so a key missing here is silently erased by it.
# Later phases write the last five; carrying them now costs nothing.
CARRY_KEYS = ("title_state", "title_source", "route",
              "recorded_at", "date_source", "source_name", "audio_fp", "title",
              "location_note", "vm_uuid")

# --- what counts as "no label" -------------------------------------------
# A recording name that says nothing about the meeting: the recorder's and
# Voice Memos' defaults, a capture-Shortcut marker, a conference export, or a
# generic word. Each is matched AFTER the date stamp and any " (N)" twin
# suffix are peeled off.
_GENERIC = [
    re.compile(r"^recording(\s+\d{8})?(\s+\d{3,4})?$", re.I),     # the native recorder
    re.compile(r"^new recording(\s+\d+)?$", re.I),                 # Voice Memos
    re.compile(r"^(untitled|audio|voice memo|memo|meeting|recording|"
               r"new memo|record)(\s+\d+)?$", re.I),
    re.compile(r"^mtg([\s\-_]+[\d\-\.:/ ]*)?$", re.I),             # the capture marker
    re.compile(r"^gmt\d{8}-\d{6}([_\s].*)?$", re.I),               # conference export
    re.compile(r"^zoom[_\s]?\d+.*$", re.I),
    re.compile(r"^audio[_\s]?\d+.*$", re.I),
    re.compile(r"^[\d\s\-\.:_/]*$"),                               # digits and punctuation only
]

# An address-style name: Voice Memos titles a memo by where it was recorded
# ("1200 Example Ave"), which says nothing about the meeting. A house number,
# one to five words, then a street word, with an optional count Voice Memos
# appends to repeats. The owner's decision: this counts as no label.
_STREET = (r"st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|"
           r"court|way|pl|place|pkwy|parkway|hwy|highway|ter|terrace|cir|circle|"
           r"sq|square|fwy|freeway|trl|trail|aly|alley")
_ADDRESS = re.compile(
    r"^\d{1,6}[a-z]?\s+(?:[nsew]\.?\s+)?(?:[\w'.\-]+\s+){0,4}?(?:" + _STREET +
    r")\.?(?:\s+(?:#?\s*\d+|[nsew]\.?))?(?:\s+\d+)?$", re.I)

_TWIN = re.compile(r"\s+\(\d+\)$")


def clean_name(name: str) -> str:
    """The name as a person typed it: no extension, no date stamp, no twin
    suffix, whitespace collapsed."""
    s = str(name or "")
    stem, ext = os.path.splitext(s)
    if ext.lower() in config.AUDIO_EXTS or ext.lower() in config.VIDEO_EXTS:
        s = stem
    s = _TWIN.sub("", s.strip())
    s = dates.strip_stamp(s)
    return re.sub(r"\s+", " ", s).strip()


def is_address(name: str) -> bool:
    """True for an address-style name such as '1200 Example Ave'."""
    return bool(_ADDRESS.match(clean_name(name)))


def is_label(name: str) -> bool:
    """Does this recording name carry a typed label worth keeping as a title?

    False for an empty name, a default or marker name, a conference export,
    a generic word, and an address-style name. A typed label that also holds
    a date ('Budget Review 9.14') is still a label."""
    s = clean_name(name)
    if not s:
        return False
    if any(p.match(s) for p in _GENERIC):
        return False
    if _ADDRESS.match(s):
        return False
    return True


# --- public downloads ------------------------------------------------------
# The board-meeting downloader records each file it delivered in its own state
# file. The match is read-only and by exact file name.
def public_state_path() -> Path:
    """Where the downloader's state lives. STT_PUBLIC_STATE wins; otherwise
    the sibling project beside this one. Resolved per call, so a sandboxed
    PROJECT_DIR finds no real state."""
    env = os.environ.get("STT_PUBLIC_STATE")
    if env:
        return Path(env).expanduser()
    return Path(config.PROJECT_DIR).parent / "board-meetings" / "state.json"


_pub_cache = {"key": None, "files": frozenset()}


def public_files() -> frozenset:
    """Every file name the public-meeting downloader delivered, cached by
    the state file's mtime. Empty when the state is missing or unreadable."""
    p = public_state_path()
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return frozenset()
    if _pub_cache["key"] == key:
        return _pub_cache["files"]
    try:
        d = json.loads(p.read_text())
        items = d.get("items") if isinstance(d, dict) else None
        vals = items.values() if isinstance(items, dict) else (items or [])
        files = frozenset(str(v.get("file")) for v in vals
                          if isinstance(v, dict) and v.get("file"))
    except (OSError, ValueError, AttributeError):
        files = frozenset()
    _pub_cache["key"], _pub_cache["files"] = key, files
    return files


def is_public_download(source_file: str) -> bool:
    return bool(source_file) and str(source_file) in public_files()


# --- routes ------------------------------------------------------------------
_NATIVE = re.compile(r"^recording\s+\d{8}\s+\d{3,4}(\s+\(\d+\))?$", re.I)
_CONFERENCE = re.compile(r"^(gmt\d{8}-\d{6}|zoom[_\s]?\d+)", re.I)


def route_for(source_file: str, src_path=None) -> str:
    """Which route brought this recording in, from its file name (and, for a
    new arrival, the folder it arrived in)."""
    name = str(source_file or "")
    stem, ext = os.path.splitext(name)
    if is_public_download(name):
        return "public_download"
    if src_path is not None:
        try:
            if Path(src_path).resolve().parent == config.recordings_dir().resolve():
                return "native"
        except OSError:
            pass
    if _NATIVE.match(stem):
        return "native"
    if ext.lower() in config.VIDEO_EXTS or _CONFERENCE.match(stem):
        return "conference"
    if ext.lower() in (".m4a", ".caf", ".qta"):
        return "voice_memo"
    return "other"


def initial_title(source_file: str, route: str) -> tuple:
    """(title_state, title_source) for a meeting arriving for the first time.

    The chain: a public download keeps its published name; a typed label is
    kept as typed; otherwise only the model's draft exists. Every result
    except the last is provisional, and none is a human check."""
    if route == "public_download":
        return "provisional", "public_download"
    if is_label(source_file):
        return "provisional", "label"
    return "needs_title", "model"


# --- the finish rule (plan row 8) -------------------------------------------
def needs_title(d: dict) -> bool:
    """Does this meeting still ask for a title? `d` is the meeting json, or a
    panel meta carrying `title_state` and `needs_review`.

    A stated `title_state` decides alone. A missing one (a meeting from before
    the rule) falls back to the old test: reviewed is explicitly False."""
    ts = d.get("title_state")
    if ts:
        return ts == "needs_title"
    if "needs_review" in d:
        return bool(d["needs_review"])
    return d.get("reviewed") is False


def is_done(d: dict) -> bool:
    """Plan row 8: done unless the title is still needed. Voices and flags
    leave the rule."""
    return not needs_title(d)


# --- the automatic title (the owner's title chain) ---------------------------
# A typed title wins, and it arrives as the recording's name (a label). A
# default name is not a typed title. For a meeting with no typed title, the
# folder takes an automatic title once, after the summary: the named
# speakers plus the topic, else the local model's draft alone. The owner's
# decision: every automatic title stays provisional. It is never confirmed
# and never counts as a human check.
AUTO_TITLE_STATE = "provisional"
AUTO_TITLE_SOURCE = "model"
_NO_TOPIC = {"", "untitled meeting", "untitled"}


def _talk_by_name(d: dict, exclude=()) -> list:
    """Named speakers, most talk first. A speaker's talk is the sum of its
    segment lengths."""
    names = {}
    for sp in d.get("speakers") or []:
        if isinstance(sp, dict) and sp.get("name") and sp["name"] not in exclude:
            names[sp.get("id")] = sp["name"]
    talk = {}
    for seg in d.get("segments") or []:
        n = names.get(seg.get("speaker"))
        if n:
            try:
                talk[n] = talk.get(n, 0.0) + max(0.0, float(seg["end"]) - float(seg["start"]))
            except (KeyError, TypeError, ValueError):
                pass
    for n in names.values():
        talk.setdefault(n, 0.0)
    return [n for n, _ in sorted(talk.items(), key=lambda kv: (-kv[1], kv[0]))]


def auto_title(d: dict, me=None):
    """The automatic title for meeting json `d`, or None when there is no
    topic to build it from. The owner's own name (`me`, the mic speaker) is
    left out: it is in nearly every meeting and says nothing about this one."""
    topic = re.sub(r"\s+", " ", str(d.get("ai_title") or "")).strip()
    if topic.lower() in _NO_TOPIC:
        return None
    who = _talk_by_name(d, exclude={me} if me else ())
    if not who:
        return topic
    if len(who) == 1:
        return f"{topic} with {who[0]}"
    if len(who) == 2:
        return f"{topic} with {who[0]} and {who[1]}"
    return f"{topic} with {who[0]}, {who[1]} and others"


def wants_auto_title(d: dict) -> bool:
    """True only for a new meeting that arrived with no typed title and still
    holds the model's placeholder state. A meeting from before the rule (no
    title_state), a typed label, a public download, and anything a person
    kept or edited are never renamed."""
    return d.get("title_state") == "needs_title" and d.get("title_source") == "model"

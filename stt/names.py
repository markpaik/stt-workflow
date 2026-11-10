"""The character rules for a name a person types.

A typed name lands in places that trust it to be one line of plain text. A
meeting title becomes a folder and its file names. A speaker name becomes a
registry key, a voiceprint file name, the transcript header and every line
label. Three kinds of character break that trust:

- control characters (newline, tab, NUL, the C1 range) and the Unicode line
  and paragraph separators split a header or a label line in two;
- bidirectional controls (U+202E and the rest) change the order a name
  displays in, so a folder named '<U+202E>gnp.txt' shows in Finder as
  'txt.png';
- zero-width characters (U+200B, U+2060, U+FEFF) make two names that look
  the same into two different people or two different folders.

The joiners U+200C and U+200D stay: several scripts and emoji need them.

A person's name REFUSES these characters (person_name): a name is an
identity, and quietly changing it would enroll someone under a spelling
nobody typed. A meeting title STRIPS them (strip_unsafe), as it always
stripped control characters: a title is only a label.
"""
import re
import unicodedata

BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                          "\u2066\u2067\u2068\u2069")
ZERO_WIDTH = frozenset("\u200b\u2060\ufeff")

EMPTY_NAME = "Type a name first."
UNSAFE_NAME = ("A name cannot hold a line break, a tab, or an invisible "
               "control character.")
# the panel's own limit for a typed name (gui/server.py, max_len=200)
NAME_MAX = 200
LONG_NAME = f"A name can be {NAME_MAX} characters at most."
NOT_TEXT = "A name must be text."


class BadName(ValueError):
    """A typed person name that cannot be stored. The message is written for
    the person who typed it."""


def unsafe(ch: str) -> bool:
    return (unicodedata.category(ch) in ("Cc", "Zl", "Zp")
            or ch in BIDI_CONTROLS or ch in ZERO_WIDTH)


def has_unsafe(s) -> bool:
    return any(unsafe(ch) for ch in str(s or ""))


def strip_unsafe(s) -> str:
    """`s` without its unsafe characters, for a meeting title. One that is
    whitespace (a newline, a tab, a line separator) becomes a space, so two
    lines never run together into one word."""
    return "".join((" " if ch.isspace() else "") if unsafe(ch) else ch
                   for ch in str(s or ""))


def person_name(raw) -> str:
    """The name to store for what someone typed: the edges stripped.

    Raises BadName for an empty or whitespace-only name, for one that holds
    an unsafe character anywhere inside it, for one over NAME_MAX
    characters, and for a value that is not text (5 enrolled as '5.npy')."""
    if raw is not None and not isinstance(raw, str):
        raise BadName(NOT_TEXT)
    name = str(raw or "").strip()
    if not name:
        raise BadName(EMPTY_NAME)
    if has_unsafe(name):
        raise BadName(UNSAFE_NAME)
    if len(name) > NAME_MAX:
        raise BadName(LONG_NAME)
    return name


# a voiceprint file name: macOS allows 255 bytes, and _unique_filename may
# add "_NN" before ".npy"
_STEM_MAX_BYTES = 200


def file_stem(name: str) -> str:
    """The voiceprint file stem for a person's name: '/' as '_' (a path
    separator), no leading dot ('.' named the hidden file '..npy', which
    a '*.npy' listing never sees), and at most _STEM_MAX_BYTES bytes, cut
    on a character. A name of dots only gives 'person'."""
    stem = str(name).replace("/", "_").lstrip(".")
    raw = stem.encode("utf-8")
    if len(raw) > _STEM_MAX_BYTES:
        stem = raw[:_STEM_MAX_BYTES].decode("utf-8", "ignore").rstrip()
    return stem or "person"


# --------------------------------------------------------- meeting titles ---
# A folder or file name cannot hold < > " \ | ? * or a control character.
# ':' and '/' carry meaning in a title ('1:1', '9:30', 'Q3/Q4'), so on disk
# they become a hyphen (file_title) and the title a person sees keeps them
# (clean_title, stored as the meeting's `title`).
_TITLE_DROP = re.compile(r'[<>"\\|?*\x00-\x1f]')
TITLE_MAX = 120


def cut_words(s: str, n: int = TITLE_MAX) -> str:
    """`s` cut to at most `n` characters. A cut that lands inside a word
    goes back to the last space, when one falls in the second half, so a
    long title never ends 'Extraordinaril'. A title with no such space
    (CJK, one long word) is cut at `n`. A separator left at the end goes."""
    if len(s) <= n:
        return s
    cut = s[:n]
    if not s[n].isspace():
        sp = cut.rfind(" ")
        if sp >= n // 2:
            cut = cut[:sp]
    return cut.rstrip(" -\u2013\u2014,;:/")


def _uncut_title(s) -> str:
    s = _TITLE_DROP.sub("", strip_unsafe(s)).strip().lstrip(".")
    return re.sub(r"\s+", " ", s)


def clean_title(s) -> str:
    """A typed meeting title as it is shown: the unsafe characters stripped
    (strip_unsafe), the characters no file name can hold dropped apart from
    ':' and '/', leading dots removed (a bare '.' or '..' would name the
    parent folder), whitespace runs collapsed, at most TITLE_MAX
    characters, cut between words (cut_words)."""
    return cut_words(_uncut_title(s)).strip()


def title_was_cut(s) -> bool:
    """True when clean_title(s) is shorter than TITLE_MAX allows for: the
    typed title ran past it and lost its end."""
    return len(_uncut_title(s).strip()) > TITLE_MAX


def file_title(s) -> str:
    """clean_title(s) in the form a folder name holds. A ':' or '/' between
    two characters becomes '-' ('1:1' -> '1-1', 'Q3/Q4' -> 'Q3-Q4'). One
    with a space beside it becomes ' - ' ('Demo: Platform' -> 'Demo -
    Platform'). One at either end is dropped ('Agenda:' -> 'Agenda'), and
    so is a dot it leaves at the front (':.x' never names a hidden
    folder)."""
    s = re.sub(r"^[\s:/]+|[\s:/]+$", "", clean_title(s)).lstrip(".")
    s = re.sub(r"(?<=\S)[:/]+(?=\S)", "-", s)
    s = re.sub(r"\s*[:/]+\s*", " - ", s)
    return cut_words(re.sub(r"\s+", " ", s)).strip()

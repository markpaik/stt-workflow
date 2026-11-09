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
nobody typed.
"""
import unicodedata

BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                          "\u2066\u2067\u2068\u2069")
ZERO_WIDTH = frozenset("\u200b\u2060\ufeff")

EMPTY_NAME = "Type a name first."
UNSAFE_NAME = ("A name cannot hold a line break, a tab, or an invisible "
               "control character.")


class BadName(ValueError):
    """A typed person name that cannot be stored. The message is written for
    the person who typed it."""


def unsafe(ch: str) -> bool:
    return (unicodedata.category(ch) in ("Cc", "Zl", "Zp")
            or ch in BIDI_CONTROLS or ch in ZERO_WIDTH)


def has_unsafe(s) -> bool:
    return any(unsafe(ch) for ch in str(s or ""))


def person_name(raw) -> str:
    """The name to store for what someone typed: the edges stripped.

    Raises BadName for an empty or whitespace-only name, and for one that
    holds an unsafe character anywhere inside it."""
    name = str(raw or "").strip()
    if not name:
        raise BadName(EMPTY_NAME)
    if has_unsafe(name):
        raise BadName(UNSAFE_NAME)
    return name

#!/usr/bin/env python3
"""Contrast audit for the panel's colour tokens (gui/static/app.css).

The Indigo Blueprint round, like Signal and Boardroom Wide before it, promises
that every text pair reaches WCAG AA 4.5:1 in BOTH themes and that a token
that misses is darkened hue-true rather than swapped for another hue. This
module is the arithmetic behind that promise: it reads the four token blocks
straight out of the stylesheet, resolves the var() aliases, and measures each
pair the design actually paints.

Run it:      python3 tools/token_contrast.py
Import it:   from tools.token_contrast import audit  ->  list of Result rows

tests/test_new_shell.py runs it over the shipped sheet, so a future token edit
cannot quietly drop a pair below 4.5:1.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

CSS_PATH = Path(__file__).resolve().parent.parent / "gui" / "static" / "app.css"

AA = 4.5   # WCAG AA for normal-size text; the house floor for every pair

# The four token blocks at the top of the sheet, in source order. The light
# theme is :root plus its data-theme=light restatement; the dark theme is the
# prefers-color-scheme block plus its data-theme=dark restatement. Each block
# must stand alone, so each is audited on its own.
BLOCKS = (
    ("light :root", r"(?m)^:root\{"),
    ("dark  prefers-color-scheme", r"(?m)^@media\(prefers-color-scheme:dark\)\{:root\{"),
    ("dark  data-theme=dark", r"(?m)^:root\[data-theme=dark\]\{"),
    ("light data-theme=light", r"(?m)^:root\[data-theme=light\]\{"),
)

# The BASE pairs: the ones the design promises by name, measured in every
# block whether or not a rule still paints them. Foreground first, background
# second; both are token names without the leading dashes. Everything else the
# sheet paints is found by painted_pairs() below, so a new rule cannot add an
# unmeasured pair (the hand list had already drifted: accent-text on
# accent-soft and ink on rec-soft were painted and never measured).
PAIRS = (
    ("ink", "paper"), ("ink", "card"), ("ink", "inset"),
    ("sub", "paper"), ("sub", "card"), ("sub", "inset"),
    ("accent-text", "paper"), ("accent-text", "card"), ("accent-text", "inset"),
    ("acc-ink", "accent"),
    ("ok", "ok-soft"), ("ok", "card"),
    ("busy", "busy-soft"),
    ("attn", "attn-soft"), ("attn", "card"), ("attn", "paper"), ("attn", "inset"),
    ("amber", "amber-soft"),          # the alias pair, measured in its own right
    ("rec-text", "rec-soft"), ("rec-text", "card"), ("rec-text", "paper"),
    ("work", "work-soft"),
    ("personal", "personal-soft"), ("personal", "card"),
)


class Result(NamedTuple):
    block: str
    fg: str
    bg: str
    fg_hex: str
    bg_hex: str
    ratio: float

    @property
    def ok(self) -> bool:
        return self.ratio >= AA

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return (f"{mark} {self.block:28s} {self.fg:12s} on {self.bg:14s} "
                f"{self.fg_hex} on {self.bg_hex} = {self.ratio:.2f}:1")


def _slice_block(css: str, pattern: str) -> str:
    """The declarations inside one token block, brace-matched from its head.

    Counting starts AFTER the match, at depth 1, because every pattern above
    already consumes the brace that opens :root. Counting from the start of
    the match instead closed the @media block rather than the :root inside it,
    so the dark block was read with a later rule's tokens folded into it."""
    m = re.search(pattern, css)
    if not m:
        raise AssertionError(f"token block not found: {pattern}")
    depth, i = 1, m.end()
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[m.end():i]
        i += 1
    raise AssertionError(f"unclosed token block: {pattern}")


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def read_tokens(css: str, pattern: str) -> dict[str, str]:
    """Token name -> value for one block, with var() aliases resolved."""
    body = _strip_comments(_slice_block(css, pattern))
    raw: dict[str, str] = {}
    for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", body):
        raw[name] = value.strip()
    out: dict[str, str] = {}
    for name in raw:
        seen, value = set(), raw[name]
        while True:
            alias = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
            if not alias:
                break
            target = alias.group(1)
            if target in seen or target not in raw:
                break
            seen.add(target)
            value = raw[target]
        out[name] = value
    return out


_RULE = re.compile(r"([^{}]*)\{([^{}]*)\}")
_FG = re.compile(r"(?:^|;)\s*color\s*:\s*var\(--([a-z0-9-]+)\)")
_BG = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:\s*var\(--([a-z0-9-]+)\)")


def painted_pairs(css: str) -> set[tuple[str, str]]:
    """Every (text token, ground token) the sheet paints in ONE rule block.

    A rule that sets both color:var(--X) and background:var(--Y) is painting X
    on Y, and that pair has to clear AA whether or not anyone remembered to
    add it to PAIRS."""
    out: set[tuple[str, str]] = set()
    for _sel, decls in _RULE.findall(_strip_comments(css)):
        fg, bg = _FG.search(decls), _BG.search(decls)
        if fg and bg:
            out.add((fg.group(1), bg.group(1)))
    return out


def _is_colour(value: str) -> bool:
    """A resolved token value that is a hex colour. A scanned pair whose token
    holds something else (a gradient, a length, an rgba() alpha wash) is not a
    contrast pair at all."""
    return bool(re.fullmatch(r"#[0-9a-fA-F]{3,8}", value.strip()))


def _lin(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def audit(css_path: Path = CSS_PATH) -> list[Result]:
    """Measure the union of PAIRS and the pairs the sheet actually paints, in
    every token block. A PAIRS token that a block no longer defines is an
    error: a block that has stopped defining a token the sheet paints is a
    bug, not a pass. A SCANNED pair is skipped when either token is not a
    colour in that block -- the rule is painting something the contrast
    arithmetic has no opinion about."""
    css = Path(css_path).read_text(encoding="utf-8")
    scanned = sorted(painted_pairs(css))          # stable order, base pairs first
    rows: list[Result] = []
    for label, pattern in BLOCKS:
        tokens = read_tokens(css, pattern)
        seen: set[tuple[str, str]] = set()
        for fg, bg in PAIRS:
            missing = [n for n in (fg, bg) if n not in tokens]
            if missing:
                raise AssertionError(f"{label}: token(s) not defined: {missing}")
            seen.add((fg, bg))
            rows.append(Result(label, fg, bg,
                               tokens[fg], tokens[bg],
                               contrast(tokens[fg], tokens[bg])))
        for fg, bg in scanned:
            if (fg, bg) in seen:
                continue
            if fg not in tokens or bg not in tokens:
                continue
            if not _is_colour(tokens[fg]) or not _is_colour(tokens[bg]):
                continue
            seen.add((fg, bg))
            rows.append(Result(label, fg, bg,
                               tokens[fg], tokens[bg],
                               contrast(tokens[fg], tokens[bg])))
    return rows


def main() -> int:
    rows = audit()
    for r in rows:
        print(r.line())
    bad = [r for r in rows if not r.ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} pairs at or above {AA}:1")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

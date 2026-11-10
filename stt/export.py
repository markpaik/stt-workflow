"""Export a meeting transcript for sharing: Word (.docx), PDF, or plain text.

Files land in ~/Downloads (the natural hand-off spot) and are revealed in
Finder. STT_EXPORT_DIR moves them. Under STT_HOME (a demo or test home)
they land in $STT_HOME/exports, so a sandbox never writes a transcript
copy to the real Downloads folder. PDF renders a styled HTML through the locally-installed Chrome in
headless mode — no extra Python dependencies. Everything stays on-device.
"""
import html
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, dates, output
from .errors import UserError

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# A line's speaker label is output.line_label over the roster, the label the
# panel and the .txt show. The roster names a voice below the minting floor
# ("Voice 9") while its lines carry only the raw diarizer id, which must
# never reach a shared file.


# what XML 1.0 forbids: C0 controls but tab, newline and return, lone
# surrogates, and the two noncharacters. python-docx refuses a string that
# holds one (ValueError), so one stray NUL from a paste or an ASR artifact
# failed the whole Word export while the PDF of the same meeting printed.
# Both formats drop them, so the two files agree.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _clean(s: str) -> str:
    return _XML_ILLEGAL.sub("", s)


class MeetingUnreadable(UserError):
    """The meeting's json is not a readable JSON object (bad JSON, a list,
    a null): a hand edit or a damaged file. Its text is the panel's."""


def _load(base: str) -> dict:
    try:
        d = json.loads(config.meeting_file(base, ".json").read_text())
    except ValueError:
        d = None
    if not isinstance(d, dict):
        raise MeetingUnreadable("This meeting's transcript file cannot be read, "
                                "so it cannot be exported.")
    return d


# A hand-edited json can hold null, the wrong type, or nothing where the
# pipeline always writes a value: a null start, a null text, a speaker with
# no display, a date that is not ISO. Each one failed the whole export with
# a raw TypeError, KeyError or ValueError. Each reader below takes such a
# value as absent: the line, the timestamp or the meta part is left out.

def _text(v) -> str:
    return _clean(v if isinstance(v, str) else "" if v is None else str(v))


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _day(v):
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _list(v) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _lines(d: dict):
    """(speaker label, timestamp or None, flagged, text) for each line with
    text, in order. The label is output.line_label over the roster."""
    roster = output.roster_labels(_list(d.get("speakers")))
    for seg in _list(d.get("segments")):
        text = _text(seg.get("text")).strip()
        if not text:
            continue
        start = _num(seg.get("start"))
        yield (_text(output.line_label(seg, roster)),
               None if start is None else _fmt_ts(start),
               bool(seg.get("flags")), text)


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _meta_line(base: str, d: dict) -> str:
    parts = []
    day = _day(d.get("date")) or _day(dates.meeting_date(base))
    if day:
        parts.append(day.strftime("%B %-d, %Y"))
    dur = _num(d.get("duration_sec", 0))
    if dur is not None:
        parts.append(f"{round(dur / 60)} min")
    who = [w for w in (_text(s.get("display")) for s in _list(d.get("speakers"))) if w]
    if who:
        parts.append(", ".join(who))
    return _clean("  ·  ".join(parts))


def export_dir() -> Path:
    """Where exported copies go: STT_EXPORT_DIR, else $STT_HOME/exports
    under a sandbox home, else ~/Downloads. Read at call time. Like the
    other settings, STT_EXPORT_DIR is read from stt.env first, then from
    the environment. A relative value is under the home folder, never under
    whatever folder the panel happened to start in."""
    v = (config._env_file().get("STT_EXPORT_DIR")
         or os.environ.get("STT_EXPORT_DIR") or "").strip()
    if v:
        p = Path(v).expanduser()
        return p if p.is_absolute() else Path.home() / p
    if config.STT_HOME:
        return Path(config._HOME) / "exports"
    return Path.home() / "Downloads"


NAME_MAX = 255   # bytes in one file name on macOS


def _out_path(base: str, ext: str) -> Path:
    """A new file in export_dir(): "<base>.<ext>", else "<base> (n).<ext>".
    The name is cut to fit NAME_MAX bytes with its suffix: a folder name
    near the limit fits its own "<base>.json", and the " (1)" of a second
    export pushed it past, which raised a raw OSError."""
    d = export_dir()
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        tail = f" ({n}).{ext}" if n else f".{ext}"
        out = d / (config.cap_name(base, NAME_MAX - len(tail.encode())) + tail)
        if not out.exists():
            return out
        n += 1


def to_docx(base: str) -> Path:
    from docx import Document
    from docx.shared import Pt, RGBColor

    d = _load(base)
    doc = Document()
    doc.add_heading(_clean(base), level=1)
    meta = doc.add_paragraph(_meta_line(base, d))
    for run in meta.runs:   # none when the json gives no meta part at all
        run.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)
        run.font.size = Pt(10)

    for who, ts, flagged, text in _lines(d):
        p = doc.add_paragraph()
        r = p.add_run(who + (f"  [{ts}]" if ts else "") + ("  ⚠" if flagged else ""))
        r.bold = True
        r.font.size = Pt(10)
        doc.add_paragraph(text)
    out = _out_path(base, "docx")
    doc.save(str(out))
    return out


def to_html(base: str) -> str:
    d = _load(base)
    rows = []
    for who, ts, flagged, text in _lines(d):
        warn = ' <span class="warn">uncertain</span>' if flagged else ""
        stamp = f' <span class="ts">{ts}</span>' if ts else ""
        rows.append(
            f'<div class="seg"><div class="who">{html.escape(who)}'
            f'{stamp}{warn}</div>'
            f'<div class="txt">{html.escape(text)}</div></div>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font:11pt -apple-system,'Helvetica Neue',sans-serif;color:#1d1d1f;
margin:48px;max-width:680px}}
h1{{font-size:17pt;margin:0 0 4px}}
.meta{{color:#6e6e73;font-size:9pt;margin-bottom:22px}}
.seg{{margin-bottom:13px;page-break-inside:avoid}}
.who{{font-weight:600;font-size:9pt}}
.ts{{color:#86868b;font-weight:400}}
.warn{{color:#c93400;font-weight:400;font-size:8pt}}
.txt{{margin-top:1px}}
</style></head><body><h1>{html.escape(_clean(base))}</h1>
<div class="meta">{html.escape(_meta_line(base, d))}</div>
{''.join(rows)}</body></html>"""


def _is_pdf(p: Path) -> bool:
    """A whole PDF: the "%PDF-" header first and the "%%EOF" marker in the
    last kilobyte."""
    try:
        with open(p, "rb") as f:
            head = f.read(5)
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 1024))
            tail = f.read()
    except OSError:
        return False
    return head == b"%PDF-" and b"%%EOF" in tail


def to_pdf(base: str) -> Path:
    """Print the meeting's HTML to a PDF with headless Chrome.

    The file decides, not Chrome's exit code: headless Chrome can write the
    whole PDF and then crash on exit (crashpad, exit 2). A complete PDF is
    the export. A missing or partial one is removed and the export fails,
    so a retry never leaves a "name (n).pdf" behind. Chrome runs on a
    throwaway profile, so it never contends with a Chrome already open,
    and on a mock keychain, so the profile never opens a keychain dialog."""
    if not Path(CHROME).exists():
        raise UserError("Google Chrome not found — export Word instead, "
                           "or install Chrome for PDF export.")
    out = _out_path(base, "pdf")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        page = Path(td) / "t.html"
        page.write_text(to_html(base), encoding="utf-8")
        try:
            rc = subprocess.run(
                [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                 f"--user-data-dir={Path(td) / 'profile'}",
                 # a new profile asks the login keychain for "Chrome Safe
                 # Storage", and macOS shows a dialog for each export
                 "--use-mock-keychain", "--password-store=basic",
                 f"--print-to-pdf={out}", str(page)],
                capture_output=True, timeout=60).returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    if _is_pdf(out):
        return out
    out.unlink(missing_ok=True)
    raise UserError(f"Chrome did not write the PDF (exit {rc}). "
                       "Try again, or export Word instead.")


def export(base: str, fmt: str) -> Path:
    if fmt == "docx":
        return to_docx(base)
    if fmt == "pdf":
        return to_pdf(base)
    raise ValueError(f"unknown export format {fmt}")

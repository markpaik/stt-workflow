"""Write the two deliverables per meeting: a readable .txt and a structured .json.

Writes are atomic (tmp + os.replace) so downstream tooling can never read a
half-written transcript as complete. Confidence in the outputs is REAL evidence
(observed match scores), never fabricated; fragile attributions are marked in the
.txt with [*] and enumerated in the .json flags.
"""
import json
import os
from datetime import datetime


def _fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _atomic_write(path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def speaker_display(label, name=None) -> str:
    """Human-readable speaker name for the .txt (real name, else 'Speaker N')."""
    if name:
        return name
    if label is None:
        return "Speaker ?"
    try:
        return f"Speaker {int(str(label).split('_')[-1]) + 1}"
    except ValueError:
        return str(label)


def txt_header(source_file, duration_sec, speakers, strict=False) -> str:
    """The one canonical .txt header line (pipeline, relabel, and review all
    regenerate the .txt and must agree)."""
    named = [s["name"] for s in speakers if s.get("name")]
    h = (f"{source_file}  |  {round((duration_sec or 0) / 60, 1)} min  |  "
         f"{len(speakers)} speaker(s)")
    if named:
        h += "  |  identified: " + ", ".join(named)
    if strict:
        h += "  |  STRICT mode (no smoothing/open-set reassignment)"
    return h


def build_speakers(labels, names) -> list:
    speakers = []
    for label in labels:
        info = names.get(label, {})
        speakers.append({
            "id": label,
            "name": info.get("name"),
            "global_id": info.get("global_id"),  # stable unknown id, e.g. "U007"
            "display": info.get("display") or speaker_display(label, info.get("name")),
            "match_score": info.get("score"),  # observed evidence or None — never faked
        })
    return speakers


def write_txt(path, segments, header=None):
    lines, any_flag = [], False
    if header:
        lines.append(header)
        lines.append("")
    for seg in segments:
        who = seg.get("display") or speaker_display(seg.get("speaker"), seg.get("name"))
        text = seg["text"].strip()
        if not text:
            continue
        mark = ""
        if seg.get("flags") or seg.get("attribution") == "smoothed":
            mark = " [*]"
            any_flag = True
        lines.append(f"[{_fmt_ts(seg['start'])}] {who}{mark}: {text}")
    if any_flag:
        lines.append("")
        lines.append("[*] = uncertain attribution (overlapping/very short speech; "
                     "see flags in the .json). Verify against audio if it matters.")
    _atomic_write(path, "\n\n".join(lines) + "\n")


def write_json(path, meta, speakers, segments, words):
    data = {
        **meta,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "speakers": speakers,
        "segments": segments,
        "words": words,
    }
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))

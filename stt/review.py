"""Human review of flagged segments + audio lookup for a speaker's voice.

The pipeline marks segments it is not sure about (overlap, smoothed fragments,
strict-mode holds). This module powers the GUI's review flow: list what needs
review, then Accept (attribution was right), Edit (fix the text), or Reassign
(fix the speaker) — persisting straight back to the meeting's .json and
regenerating its .txt, so the underlying files are always the truth.

Word-preserving contract: an edited segment keeps its word-level timing entries
(speaker labels update on reassign), but the segment text becomes the human's
version and is marked "text_edited" — we never pretend edited text came from ASR.
"""
import json

from . import config, identify, output


def _load(base: str):
    jpath = config.MEETINGS_DIR / f"{base}.json"
    if not jpath.exists():
        raise FileNotFoundError(f"no transcript json for {base}")
    return jpath, json.loads(jpath.read_text())


def is_minor(seg) -> bool:
    """A flagged crumb not worth a human's time by default: a sub-second
    interjection of a few words ("like", "so", "and") caught in crosstalk."""
    return ((seg["end"] - seg["start"]) < 1.0
            and len(seg.get("text", "").split()) <= 3)


def list_flagged(base: str) -> dict:
    """Segments needing review — substantial ones first (longest, most words),
    sub-second crumbs last with a bulk-accept path."""
    _, data = _load(base)
    segs = data.get("segments", [])
    items = []
    for i, s in enumerate(segs):
        if not s.get("flags") or s.get("reviewed"):
            continue
        items.append({
            "index": i, "start": s["start"], "end": s["end"],
            "speaker": s.get("speaker"),
            "display": s.get("display") or output.speaker_display(s.get("speaker"), s.get("name")),
            "text": s.get("text", ""), "flags": s.get("flags", []),
            "minor": is_minor(s),
            "prev": segs[i - 1]["text"][-90:] if i > 0 else "",
            "next": segs[i + 1]["text"][:90] if i + 1 < len(segs) else "",
        })
    items.sort(key=lambda x: (x["minor"], -(x["end"] - x["start"])))
    return {"base": base, "items": items,
            "n_minor": sum(1 for x in items if x["minor"]),
            "speakers": [{"id": s["id"], "display": s["display"]}
                         for s in data.get("speakers", [])]}


def accept_minor(base: str) -> dict:
    """Bulk-accept every minor flagged crumb (recorded as decisions, so they
    stay accepted across relabels). Substantial items remain for review."""
    jpath, data = _load(base)
    n = 0
    for seg in data.get("segments", []):
        if seg.get("flags") and not seg.get("reviewed") and is_minor(seg):
            seg["reviewed"] = "accepted"
            _record_decision(base, {"start": seg["start"], "end": seg["end"],
                                    "action": "accept", "text": None, "speaker_id": None})
            seg["flags"] = []
            seg["overlap"] = False
            n += 1
    if n:
        _rewrite(jpath, data)
    remaining = sum(1 for s in data.get("segments", [])
                    if s.get("flags") and not s.get("reviewed"))
    return {"ok": True, "accepted": n, "remaining": remaining}


def _rewrite(jpath, data):
    """Persist mutated data: atomic json + regenerated txt with the shared header."""
    segments, words, speakers = data["segments"], data["words"], data["speakers"]
    meta = {k: v for k, v in data.items()
            if k not in ("speakers", "segments", "words", "generated_at")}
    output.write_json(jpath, meta, speakers, segments, words)
    header = output.txt_header(data.get("source_file", jpath.stem),
                               data.get("duration_sec", 0), speakers,
                               data.get("strict", False))
    output.write_txt(jpath.with_suffix(".txt"), segments, header=header)


def apply(base: str, index: int, action: str, start: float = None,
          text: str = None, speaker_id: str = None) -> dict:
    """Apply one review decision. action: 'accept' | 'edit'.
    Returns {"ok": bool, "remaining": int}. `start` sanity-checks the index
    against the file in case it changed since the list was fetched."""
    jpath, data = _load(base)
    segs = data.get("segments", [])
    if not (0 <= index < len(segs)):
        return {"ok": False, "error": "segment index out of range"}
    seg = segs[index]
    if start is not None and abs(seg["start"] - float(start)) > 0.25:
        return {"ok": False, "error": "transcript changed since review opened — reopen it"}

    if action == "accept":
        seg["reviewed"] = "accepted"
    elif action == "edit":
        if text is not None and text.strip() and text.strip() != seg.get("text", "").strip():
            seg["text"] = text.strip()
            seg["text_edited"] = True
        if speaker_id:
            by_id = {s["id"]: s for s in data.get("speakers", [])}
            sp = by_id.get(speaker_id)
            if sp is None:
                return {"ok": False, "error": f"unknown speaker {speaker_id}"}
            seg["speaker"] = sp["id"]
            seg["name"] = sp.get("name")
            seg["display"] = sp["display"]
            # keep word-level labels consistent with the human's call
            for w in data.get("words", []):
                if seg["start"] <= w["start"] < seg["end"]:
                    w["speaker"] = sp["id"]
        seg["reviewed"] = "edited"
    else:
        return {"ok": False, "error": f"unknown action {action}"}

    seg["flags"] = []
    seg["overlap"] = False
    _record_decision(base, {"start": seg["start"], "end": seg["end"], "action": action,
                            "text": text if action == "edit" else None,
                            "speaker_id": speaker_id if action == "edit" else None})
    _rewrite(jpath, data)
    remaining = sum(1 for s in segs if s.get("flags") and not s.get("reviewed"))
    return {"ok": True, "remaining": remaining}


def _decisions_path(base: str):
    return config.MEETINGS_DIR / f"{base}.reviews.json"


def _record_decision(base: str, decision: dict):
    """Review decisions persist in a sidecar so a later relabel (which rebuilds
    segments from the diarization cache) can NEVER silently erase human work."""
    from datetime import datetime
    p = _decisions_path(base)
    decisions = []
    if p.exists():
        try:
            decisions = json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    decision["at"] = datetime.now().isoformat(timespec="seconds")
    # newest decision for the same segment wins
    decisions = [d for d in decisions
                 if abs(d["start"] - decision["start"]) > 0.25] + [decision]
    p.write_text(json.dumps(decisions, indent=2))


def reapply_decisions(base: str, data: dict) -> int:
    """Re-apply recorded review decisions onto freshly-rebuilt segments (called
    by relabel after it regenerates a meeting). Returns how many were applied."""
    p = _decisions_path(base)
    if not p.exists():
        return 0
    try:
        decisions = json.loads(p.read_text())
    except json.JSONDecodeError:
        return 0
    by_id = {s["id"]: s for s in data.get("speakers", [])}
    applied = 0
    for dec in decisions:
        seg = next((s for s in data.get("segments", [])
                    if abs(s["start"] - dec["start"]) <= 0.3), None)
        if seg is None:
            continue
        if dec["action"] == "edit":
            if dec.get("text"):
                seg["text"] = dec["text"]
                seg["text_edited"] = True
            sp = by_id.get(dec.get("speaker_id") or "")
            if sp:
                seg["speaker"], seg["name"] = sp["id"], sp.get("name")
                seg["display"] = sp["display"]
                for w in data.get("words", []):
                    if seg["start"] <= w["start"] < seg["end"]:
                        w["speaker"] = sp["id"]
        seg["reviewed"] = "edited" if dec["action"] == "edit" else "accepted"
        seg["flags"] = []
        seg["overlap"] = False
        applied += 1
    return applied


def find_voice_clip(key: str, meeting: str = None):
    """Locate a playable stretch of `key`'s voice: (meeting_base, start, dur).

    key may be a speaker id (SPEAKER_xx), a global unknown id (U007), a display
    name, or an enrolled person's name. For enrolled people whose transcripts
    haven't been relabeled yet (e.g. named mid-batch), fall back to VOICEPRINT
    matching against each source meeting's cached centroids — the person's name
    doesn't need to appear in any transcript for playback to work."""
    dst = config.MEETINGS_DIR

    def _by_transcript(base):
        j = dst / f"{base}.json"
        if not j.exists():
            return None
        try:
            d = json.loads(j.read_text())
        except json.JSONDecodeError:
            return None
        ids = {s["id"] for s in d.get("speakers", [])
               if key in (s["id"], s.get("global_id"), s.get("display"), s.get("name"))}
        segs = [s for s in d.get("segments", []) if s.get("speaker") in ids]
        if not segs:
            return None
        seg = max(segs, key=lambda s: s["end"] - s["start"])
        return base, max(0.0, seg["start"]), min(12.0, max(2.0, seg["end"] - seg["start"]))

    candidates = ([meeting] if meeting else
                  [j.stem for j in sorted(dst.glob("*.json"),
                                          key=lambda p: p.stat().st_mtime, reverse=True)])
    for base in candidates:
        hit = _by_transcript(base)
        if hit:
            return hit

    # voiceprint fallback for enrolled names (works before relabel has run)
    reg = identify.load_registry()
    if key not in reg:
        return None
    samples = identify.load_voiceprints().get(key)
    if samples is None:
        return None
    sources = [s for s in reg[key].get("sources", []) if s and s != "?"]
    for base in (sources[::-1] or [j.stem for j in dst.glob("*.json")]):
        dcache = dst / f"{base}.diar.npz"
        jf = dst / f"{base}.json"
        if not dcache.exists() or not jf.exists():
            continue
        from . import diarcache
        try:
            _, _, cent_emb = diarcache.load(dcache)[:3]
        except Exception:
            continue
        scored = [(identify.score_against(v, samples), label)
                  for label, v in cent_emb.items()]
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.5:
            label = scored[0][1]
            try:
                d = json.loads(jf.read_text())
            except json.JSONDecodeError:
                continue
            segs = [s for s in d.get("segments", []) if s.get("speaker") == label]
            if segs:
                seg = max(segs, key=lambda s: s["end"] - s["start"])
                return base, max(0.0, seg["start"]), min(12.0, max(2.0, seg["end"] - seg["start"]))
    return None

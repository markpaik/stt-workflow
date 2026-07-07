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
import contextlib
import fcntl
import json
import threading
import uuid

from . import config, identify, output

_meeting_lock_depth = threading.local()


@contextlib.contextmanager
def lock_meeting(base: str):
    """Serialize a full read-modify-write of one meeting's json between the
    GUI's review edits (this module) and relabel's rebuild (relabel.py) —
    both mutate the SAME file with no other exclusion between them, so a save
    landing mid-relabel (or vice versa) used to silently lose whichever write
    finished last. Per-meeting, not a single global lock, so editing one
    meeting never blocks browsing or editing another.

    Re-entrant WITHIN one thread, per meeting (mirrors identify.lock_registry):
    flock isn't re-entrant, so a nested lock_meeting(base) on the same thread
    would deadlock against itself. A per-base depth counter makes only the
    outermost call take the real OS lock; another process/thread still
    genuinely waits."""
    held = getattr(_meeting_lock_depth, "held", None)
    if held is None:
        held = _meeting_lock_depth.held = {}
    if held.get(base, 0) > 0:
        held[base] += 1
        try:
            yield
        finally:
            held[base] -= 1
        return
    lock_dir = config.MEETINGS_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{base}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held[base] = 1
        try:
            yield
        finally:
            held[base] = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


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
            "alt": s.get("alt"),  # second engine's candidate(s), verify mode
            "minor": is_minor(s),
            "prev": segs[i - 1]["text"][-90:] if i > 0 else "",
            "next": segs[i + 1]["text"][:90] if i + 1 < len(segs) else "",
        })
    items.sort(key=lambda x: (x["minor"], -(x["end"] - x["start"])))
    return {"base": base, "items": items,
            "n_minor": sum(1 for x in items if x["minor"]),
            "speakers": [{"id": s["id"], "display": s["display"]}
                         for s in data.get("speakers", [])],
            # enrolled people, for reassigning to someone the diarizer missed
            "people": sorted(identify.load_registry().keys())}


def accept_minor(base: str) -> dict:
    """Bulk-accept every minor flagged crumb (recorded as decisions, so they
    stay accepted across relabels). Substantial items remain for review."""
    with lock_meeting(base):
        jpath, data = _load(base)
        n = 0
        for seg in data.get("segments", []):
            if seg.get("flags") and not seg.get("reviewed") and is_minor(seg):
                seg["reviewed"] = "accepted"
                _record_decision(base, {"start": seg["start"], "end": seg["end"],
                                        "action": "accept", "text": None, "speaker_id": None})
                seg["flags"] = []
                seg["overlap"] = False
                seg.pop("alt", None)
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


def _resolve_speaker(data, spec):
    """Resolve a speaker spec to this meeting's speaker entry, creating one when
    the person isn't among the diarized speakers. spec is either an existing
    speaker id ("SPEAKER_03") or "name:<who>" — an enrolled person or a brand-new
    name typed by the human (a voice diarization missed entirely, e.g. crosstalk).
    Manual entries get ids MANUAL_1, MANUAL_2… and their display is the name."""
    speakers = data.setdefault("speakers", [])
    by_id = {s["id"]: s for s in speakers}
    if spec in by_id:
        return by_id[spec]
    if not (spec or "").startswith("name:"):
        return None
    nm = spec[5:].strip()
    if not nm:
        return None
    for s in speakers:
        if nm in (s.get("name"), s.get("display")):
            return s
    n = 1 + sum(1 for s in speakers if str(s["id"]).startswith("MANUAL_"))
    entry = {"id": f"MANUAL_{n}", "name": nm, "global_id": None,
             "display": nm, "match_score": None, "manual": True}
    speakers.append(entry)
    return entry


def _set_segment_speaker(data, seg, sp):
    seg["speaker"] = sp["id"]
    seg["name"] = sp.get("name")
    seg["display"] = sp["display"]
    # keep word-level labels consistent with the human's call
    for w in data.get("words", []):
        if seg["start"] <= w["start"] < seg["end"]:
            w["speaker"] = sp["id"]


def apply(base: str, index: int, action: str, start: float = None,
          text: str = None, speaker_id: str = None) -> dict:
    """Apply one review decision. action: 'accept' | 'edit'.
    Returns {"ok": bool, "remaining": int}. `start` sanity-checks the index
    against the file in case it changed since the list was fetched."""
    with lock_meeting(base):
        return _apply_locked(base, index, action, start, text, speaker_id)


def _locate_segment(segs, index, start):
    """Resolve the segment a review decision targets. `index` is a snapshot
    from when the client fetched its list; a relabel that ran since can shift
    every later index by inserting/merging/nudging segments, so trusting the
    index blindly risks silently editing the wrong line. `start` is the
    client's cross-check.

    Exact index + matching start (within STRICT) is the common case. If start
    drifted past STRICT — e.g. a relabel nudged boundaries by re-running
    diarization, not a structural change — fall back to locating this
    segment by start-time proximity across the whole list (within WIDE)
    instead of hard-rejecting a still-valid edit. No match within WIDE means
    the transcript genuinely changed underneath the review; reject. So does
    an AMBIGUOUS match: recovery is only safe when the nearest candidate is
    decisively nearest — a second segment nearly as close (within TIE) means
    the stale start can no longer say which line the human meant, and
    guessing risks editing (or deleting) the wrong one.

    Returns (index, seg) or (None, None)."""
    STRICT, WIDE, TIE = 0.25, 2.0, 0.5
    in_range = 0 <= index < len(segs)
    if start is None:
        return (index, segs[index]) if in_range else (None, None)
    start = float(start)
    if in_range and abs(segs[index]["start"] - start) <= STRICT:
        return index, segs[index]
    dists = sorted((abs(s["start"] - start), i) for i, s in enumerate(segs))
    if not dists or dists[0][0] >= WIDE:
        return None, None
    best_d, best_i = dists[0]
    if len(dists) > 1 and (dists[1][0] - best_d) < TIE:
        return None, None
    return best_i, segs[best_i]


def _apply_locked(base, index, action, start, text, speaker_id) -> dict:
    jpath, data = _load(base)
    segs = data.get("segments", [])
    if not (0 <= index < len(segs)) and start is None:
        return {"ok": False, "error": "segment index out of range"}
    index, seg = _locate_segment(segs, index, start)
    if seg is None:
        return {"ok": False, "error": "transcript changed since review opened — reopen it"}

    if action == "accept":
        seg["reviewed"] = "accepted"
    elif action == "edit":
        if text is not None and text.strip() and text.strip() != seg.get("text", "").strip():
            seg["text"] = text.strip()
            seg["text_edited"] = True
        if speaker_id:
            sp = _resolve_speaker(data, speaker_id)
            if sp is None:
                return {"ok": False, "error": f"unknown speaker {speaker_id}"}
            _set_segment_speaker(data, seg, sp)
        seg["reviewed"] = "edited"
    else:
        return {"ok": False, "error": f"unknown action {action}"}

    seg["flags"] = []
    seg["overlap"] = False
    seg.pop("alt", None)
    if seg.get("inserted") and seg.get("manual_id"):
        # this line only exists because a human inserted it — there is no
        # rebuilt segment for a plain accept/edit decision to attach to on the
        # next relabel, so update the ORIGINAL insert decision in place
        # (matched by manual_id) rather than recording a competing one keyed
        # by timestamp, which would just make the line vanish on replay.
        _record_decision(base, {"start": seg["start"], "end": seg["end"], "action": "insert",
                                "text": seg["text"], "speaker_id": seg["speaker"],
                                "manual_id": seg["manual_id"]})
    else:
        _record_decision(base, {"start": seg["start"], "end": seg["end"], "action": action,
                                "text": text if action == "edit" else None,
                                "speaker_id": speaker_id if action == "edit" else None})
    _rewrite(jpath, data)
    remaining = sum(1 for s in segs if s.get("flags") and not s.get("reviewed"))
    return {"ok": True, "remaining": remaining}


def insert_segment(base: str, start: float, end: float, speaker_id: str,
                   text: str) -> dict:
    """Add a line the pipeline missed entirely (a voice buried in crosstalk).
    The segment is human-authored: no word-level timing entries, marked
    inserted + text_edited, and it survives every relabel via the decisions
    sidecar."""
    if not (text or "").strip():
        return {"ok": False, "error": "text is required"}
    with lock_meeting(base):
        jpath, data = _load(base)
        sp = _resolve_speaker(data, speaker_id)
        if sp is None:
            return {"ok": False, "error": f"unknown speaker {speaker_id}"}
        start, end = max(0.0, float(start)), float(end)
        if end <= start:
            end = start + 1.0
        manual_id = uuid.uuid4().hex[:12]
        seg = {"start": round(start, 3), "end": round(end, 3), "speaker": sp["id"],
               "text": text.strip(), "attribution": "manual", "flags": [],
               "overlap": False, "name": sp.get("name"), "display": sp["display"],
               "text_edited": True, "inserted": True, "reviewed": "edited",
               "manual_id": manual_id}
        segs = data.get("segments", [])
        pos = next((i for i, s in enumerate(segs) if s["start"] > seg["start"]), len(segs))
        segs.insert(pos, seg)
        _record_decision(base, {"start": seg["start"], "end": seg["end"],
                                "action": "insert", "text": seg["text"],
                                "speaker_id": speaker_id, "manual_id": manual_id})
        _rewrite(jpath, data)
        return {"ok": True, "index": pos}


def delete_segment(base: str, index: int, start: float = None) -> dict:
    """Remove a line (echo, hallucination, misheard non-speech). Its words are
    detached from any speaker rather than deleted — the ASR record stays honest."""
    with lock_meeting(base):
        jpath, data = _load(base)
        segs = data.get("segments", [])
        if not (0 <= index < len(segs)) and start is None:
            return {"ok": False, "error": "segment index out of range"}
        index, seg = _locate_segment(segs, index, start)
        if seg is None:
            return {"ok": False, "error": "transcript changed since it was opened — reopen it"}
        segs.pop(index)
        for w in data.get("words", []):
            if seg["start"] <= w["start"] < seg["end"] and w.get("speaker") == seg.get("speaker"):
                w["speaker"] = None
        if seg.get("inserted"):
            # a human-INSERTED line exists only because of its insert decision
            # — erase that decision instead of recording a delete. Recording a
            # timestamp-keyed delete is actively dangerous: _record_decision's
            # proximity dedup discards the insert (same start), leaving an
            # orphaned delete free to match — and destroy — a REAL segment
            # within 0.3s on the next relabel replay.
            _drop_insert_decision(base, seg)
        else:
            _record_decision(base, {"start": seg["start"], "end": seg["end"],
                                    "action": "delete", "text": None, "speaker_id": None})
        _rewrite(jpath, data)
        return {"ok": True}


def _drop_insert_decision(base: str, seg):
    """Remove the insert decision that created `seg` — matched by manual_id
    when present, else by start proximity (inserts recorded before manual_id
    existed)."""
    p = _decisions_path(base)
    if not p.exists():
        return
    try:
        decisions = json.loads(p.read_text())
    except json.JSONDecodeError:
        return
    mid = seg.get("manual_id")
    keep = [d for d in decisions
            if not (d.get("action") == "insert"
                    and ((mid and d.get("manual_id") == mid)
                         or (not mid and abs(d.get("start", -1e9) - seg["start"]) <= 0.25)))]
    if len(keep) != len(decisions):
        _write_decisions(p, keep)


def _decisions_path(base: str):
    return config.MEETINGS_DIR / f"{base}.reviews.json"


def count_decisions(base: str) -> int:
    """How many saved human edits this meeting has (for the Redo warning)."""
    p = _decisions_path(base)
    try:
        return len(json.loads(p.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def archive_decisions(base: str, dest_dir=None) -> bool:
    """A REDO rebuilds transcription AND diarization from scratch — the new
    speaker cluster ids have no relation to the old ones, so replaying old
    decisions could put words in the wrong mouths. Archive them instead
    (nothing is deleted; the file is renamed .superseded)."""
    from pathlib import Path
    d = Path(dest_dir) if dest_dir else config.MEETINGS_DIR
    p = d / f"{base}.reviews.json"
    if not p.exists():
        return False
    p.replace(d / f"{base}.reviews.superseded.json")
    return True


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
    # newest decision for the same segment wins. A manual_id (inserted lines)
    # is matched exactly — start-time proximity would conflate an edit of an
    # inserted line with the insert decision that created it and delete the
    # insert, since both share the same timestamp; matching by manual_id keeps
    # them as one decision that always replays as an insert.
    mid = decision.get("manual_id")
    if mid:
        decisions = [d for d in decisions if d.get("manual_id") != mid] + [decision]
    else:
        decisions = [d for d in decisions
                     if abs(d["start"] - decision["start"]) > 0.25] + [decision]
    _write_decisions(p, decisions)


def _write_decisions(p, decisions):
    """Atomic: this file is the ONLY durable record of every human review
    edit — a kill landing mid-write must never truncate it, because both
    readers silently degrade a torn file to 'no decisions', which would lose
    all accumulated human work on the next save and replay nothing on the
    next relabel."""
    import os
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(decisions, indent=2))
    os.replace(tmp, p)


def reapply_decisions(base: str, data: dict) -> int:
    """Re-apply recorded review decisions onto freshly-rebuilt segments (called
    by relabel after it regenerates a meeting). Handles accepts, edits and
    speaker reassignments (including manual people the diarizer never saw),
    plus human-inserted and human-deleted lines. Returns how many applied."""
    p = _decisions_path(base)
    if not p.exists():
        return 0
    try:
        decisions = json.loads(p.read_text())
    except json.JSONDecodeError:
        return 0
    segs = data.get("segments", [])
    applied = 0
    for dec in decisions:
        if dec["action"] == "insert":
            seg = {"start": dec["start"], "end": dec["end"], "speaker": None,
                   "text": dec.get("text") or "", "attribution": "manual",
                   "flags": [], "overlap": False, "text_edited": True,
                   "inserted": True, "reviewed": "edited"}
            if dec.get("manual_id"):
                seg["manual_id"] = dec["manual_id"]
            sp = _resolve_speaker(data, dec.get("speaker_id") or "")
            if sp:
                seg["speaker"], seg["name"] = sp["id"], sp.get("name")
                seg["display"] = sp["display"]
            pos = next((i for i, s in enumerate(segs)
                        if s["start"] > seg["start"]), len(segs))
            segs.insert(pos, seg)
            applied += 1
            continue
        seg = next((s for s in segs
                    if abs(s["start"] - dec["start"]) <= 0.3
                    and not s.get("inserted")), None)
        if seg is None:
            continue
        if dec["action"] == "delete":
            segs.remove(seg)
            for w in data.get("words", []):
                if seg["start"] <= w["start"] < seg["end"] and w.get("speaker") == seg.get("speaker"):
                    w["speaker"] = None
            applied += 1
            continue
        if dec["action"] == "edit":
            if dec.get("text"):
                seg["text"] = dec["text"]
                seg["text_edited"] = True
            sp = _resolve_speaker(data, dec.get("speaker_id") or "")
            if sp:
                _set_segment_speaker(data, seg, sp)
        seg["reviewed"] = "edited" if dec["action"] == "edit" else "accepted"
        seg["flags"] = []
        seg["overlap"] = False
        seg.pop("alt", None)
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
        if not scored:
            continue
        best_score, best_label = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else -1.0
        # same open-set bar as every other identity decision in this codebase
        # (config.NAMING_THRESHOLD/MARGIN) — a bare score with no runner-up
        # check was the most permissive match anywhere in the system, and
        # this one plays audio: a confidently-wrong match means the human
        # hears the WRONG person's voice while trying to verify a name.
        if not (best_score >= config.NAMING_THRESHOLD
                and (best_score - second_score) >= config.NAMING_MARGIN):
            continue
        label = best_label
        try:
            d = json.loads(jf.read_text())
        except json.JSONDecodeError:
            continue
        segs = [s for s in d.get("segments", []) if s.get("speaker") == label]
        if segs:
            seg = max(segs, key=lambda s: s["end"] - s["start"])
            return base, max(0.0, seg["start"]), min(12.0, max(2.0, seg["end"] - seg["start"]))
    return None

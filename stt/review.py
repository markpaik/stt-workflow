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
    lock_dir = config.meetings_dir() / ".locks"
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
    jpath = config.meeting_file(base, ".json")
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


def _merge_same_speaker_run(data, idx):
    """Combine the run of consecutive same-speaker segments around segs[idx]
    into one. The pipeline never emits same-speaker neighbors (assign_and_group
    breaks segments only on speaker change), so adjacency like this exists
    only because a human edit just created it — a reassignment healing a
    misattribution, a split whose tail matches the next line, a deleted echo
    between two halves of one turn — and in every case it reads as ONE turn.
    Inserted lines never merge: they replay from their own decisions.

    Pending review flags on absorbed neighbors survive (the human approved
    the edited part, not the neighbors' flagged words). Returns
    (index_of_merged_segment, parts_combined)."""
    segs = data.get("segments", [])
    if not (0 <= idx < len(segs)) or segs[idx].get("inserted"):
        return idx, 1
    spk = segs[idx].get("speaker")

    def joins(s):
        return s.get("speaker") == spk and not s.get("inserted")

    lo, hi = idx, idx
    while lo > 0 and joins(segs[lo - 1]):
        lo -= 1
    while hi + 1 < len(segs) and joins(segs[hi + 1]):
        hi += 1
    if lo == hi:
        return idx, 1
    parts = segs[lo:hi + 1]
    merged = dict(parts[0])
    merged["end"] = parts[-1]["end"]
    merged["text"] = " ".join(p.get("text", "").strip() for p in parts).strip()
    merged["text_edited"] = any(p.get("text_edited") for p in parts)
    for k in ("speaker", "name", "display"):
        merged[k] = segs[idx].get(k)
    flags = list(dict.fromkeys(f for p in parts for f in (p.get("flags") or [])))
    merged["flags"] = flags
    merged["overlap"] = any(p.get("overlap") for p in parts)
    alts = [a for p in parts for a in (p.get("alt") or [])]
    if alts:
        merged["alt"] = alts
    else:
        merged.pop("alt", None)
    if flags:
        merged.pop("reviewed", None)  # absorbed prompts still need a look
    else:
        merged["reviewed"] = "edited"
    segs[lo:hi + 1] = [merged]
    return lo, len(parts)


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
    merged_at, n_merged = index, 1
    if action == "edit" and speaker_id:
        # the decision above is keyed to the segment's PRE-merge start, which
        # is what the relabel replay will see on rebuilt (unmerged) segments
        merged_at, n_merged = _merge_same_speaker_run(data, index)
    _rewrite(jpath, data)
    remaining = sum(1 for s in segs if s.get("flags") and not s.get("reviewed"))
    return {"ok": True, "remaining": remaining, "index": merged_at,
            "merged": n_merged > 1}


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
        # removing an interjection between two halves of one person's turn
        # leaves same-speaker neighbors touching — that reads as one turn
        if 0 < index < len(segs) and segs[index - 1].get("speaker") == segs[index].get("speaker"):
            _merge_same_speaker_run(data, index - 1)
        _rewrite(jpath, data)
        return {"ok": True}


def split_segment(base: str, index: int, start: float, text_a: str, text_b: str,
                  speaker_a: str = None, speaker_b: str = None) -> dict:
    """Split one line in two — the tail (text_b) usually belongs to someone
    the diarizer glued onto this speaker. The cut time comes from the
    segment's own word timings (proportional fallback for edited text) and is
    recorded in the decision, so the split survives every relabel by
    time-of-cut. When a half's new speaker matches its neighbor, the
    same-speaker auto-merge folds them together — the full repair for a
    misattributed tail in one gesture."""
    if not (text_a or "").strip() or not (text_b or "").strip():
        return {"ok": False, "error": "both halves need some text"}
    with lock_meeting(base):
        jpath, data = _load(base)
        segs = data.get("segments", [])
        if not (0 <= index < len(segs)) and start is None:
            return {"ok": False, "error": "segment index out of range"}
        index, seg = _locate_segment(segs, index, start)
        if seg is None:
            return {"ok": False, "error": "transcript changed since it was opened — reopen it"}
        if seg.get("inserted"):
            return {"ok": False, "error": "an added line has no word timings to split — edit it instead"}
        sp_b = _resolve_speaker(data, speaker_b or seg["speaker"])
        if sp_b is None:
            return {"ok": False, "error": f"unknown speaker {speaker_b}"}
        sp_a = _resolve_speaker(data, speaker_a) if speaker_a else None
        if speaker_a and sp_a is None:
            return {"ok": False, "error": f"unknown speaker {speaker_a}"}
        orig_start, orig_end, orig_speaker = seg["start"], seg["end"], seg["speaker"]
        at = _cut_time(data, seg, text_a, text_b)
        b = _do_split(data, segs, index, at, text_a, text_b, sp_a, sp_b)
        # replay note: speaker specs are stored as GIVEN (id or "name:<who>"),
        # like edit decisions — a MANUAL_n id resolved now wouldn't exist on
        # rebuilt data, but the spec re-resolves
        _record_decision(base, {"start": orig_start, "end": orig_end,
                                "action": "split", "cut": at,
                                "text": text_a.strip(), "text_b": text_b.strip(),
                                "speaker_id": speaker_a,
                                "speaker_b": speaker_b or orig_speaker})
        a_idx = _merge_split_halves(data, seg, b)
        _rewrite(jpath, data)
        remaining = sum(1 for s in segs if s.get("flags") and not s.get("reviewed"))
        return {"ok": True, "index": a_idx, "remaining": remaining}


def _cut_time(data, seg, text_a, text_b):
    """Where in the audio the split lands. When the text still matches the
    segment's ASR words one-to-one, cut exactly between word k-1 and word k;
    for human-edited text, fall back to a character-proportional estimate."""
    k = len(text_a.split())
    n_tokens = k + len(text_b.split())
    words = sorted((w for w in data.get("words", [])
                    if seg["start"] <= w["start"] < seg["end"]
                    and w.get("speaker") == seg.get("speaker")),
                   key=lambda w: w["start"])
    if len(words) == n_tokens and 0 < k < len(words):
        return round((words[k - 1]["end"] + words[k]["start"]) / 2, 3)
    frac = k / max(1, n_tokens)
    return round(seg["start"] + frac * (seg["end"] - seg["start"]), 3)


def _do_split(data, segs, i, at, text_a, text_b, sp_a, sp_b):
    """Mutate segs[i] into the first half and insert the second after it."""
    seg = segs[i]
    at = min(max(float(at), seg["start"] + 0.05), seg["end"] - 0.05)
    b = {"start": round(at, 3), "end": seg["end"], "speaker": seg["speaker"],
         "name": seg.get("name"), "display": seg.get("display"),
         "text": text_b.strip(), "attribution": seg.get("attribution", "diarized"),
         "flags": [], "overlap": False, "text_edited": True, "reviewed": "edited"}
    seg["end"] = round(at, 3)
    seg["text"] = text_a.strip()
    seg["text_edited"] = True
    seg["reviewed"] = "edited"
    seg["flags"] = []
    seg["overlap"] = False
    seg.pop("alt", None)
    segs.insert(i + 1, b)
    if sp_a:
        _set_segment_speaker(data, seg, sp_a)
    _set_segment_speaker(data, b, sp_b)
    return b


def _merge_split_halves(data, a_seg, b_seg):
    """Run the same-speaker merge for both halves of a fresh split — but only
    when the halves ended up with DIFFERENT speakers. Same-speaker halves are
    a deliberate two-line split; auto-merging would silently undo it.
    Returns the post-merge index of the first half's segment."""
    segs = data.get("segments", [])
    if b_seg is not None and a_seg.get("speaker") != b_seg.get("speaker"):
        _merge_same_speaker_run(data, next(i for i, s in enumerate(segs) if s is a_seg))
        _merge_same_speaker_run(data, next(i for i, s in enumerate(segs) if s is b_seg))
    return next((i for i, s in enumerate(segs)
                 if s is a_seg or (s["start"] <= a_seg["start"] < s["end"])), 0)


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
    return config.meeting_file(base, ".reviews.json")


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
    p = config.meeting_file(base, ".reviews.json", dest_dir)
    if not p.exists():
        return False
    p.replace(config.meeting_file(base, ".reviews.superseded.json", dest_dir))
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
            i = next(j for j, s in enumerate(segs) if s is seg)
            segs.remove(seg)
            for w in data.get("words", []):
                if seg["start"] <= w["start"] < seg["end"] and w.get("speaker") == seg.get("speaker"):
                    w["speaker"] = None
            if 0 < i < len(segs) and segs[i - 1].get("speaker") == segs[i].get("speaker"):
                _merge_same_speaker_run(data, i - 1)
            applied += 1
            continue
        if dec["action"] == "split":
            sp_b = _resolve_speaker(data, dec.get("speaker_b") or "")
            if sp_b is None:
                continue
            sp_a = (_resolve_speaker(data, dec["speaker_id"])
                    if dec.get("speaker_id") else None)
            i = next(j for j, s in enumerate(segs) if s is seg)
            b = _do_split(data, segs, i, dec["cut"],
                          dec.get("text") or "", dec.get("text_b") or "", sp_a, sp_b)
            _merge_split_halves(data, seg, b)
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
        if dec["action"] == "edit" and dec.get("speaker_id"):
            _merge_same_speaker_run(
                data, next(j for j, s in enumerate(segs) if s is seg))
    return applied


def find_voice_clip(key: str, meeting: str = None):
    """Locate a playable stretch of `key`'s voice: (meeting_base, start, dur).

    key may be a speaker id (SPEAKER_xx), a global unknown id (U007), a display
    name, or an enrolled person's name. For enrolled people whose transcripts
    haven't been relabeled yet (e.g. named mid-batch), fall back to VOICEPRINT
    matching against each source meeting's cached centroids — the person's name
    doesn't need to appear in any transcript for playback to work."""
    def _by_transcript(base):
        j = config.meeting_file(base, ".json")
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

    all_bases = config.meeting_bases()
    candidates = ([meeting] if meeting else
                  sorted(all_bases, key=lambda b: config.meeting_file(b, ".json").stat().st_mtime,
                         reverse=True))
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
    for base in (sources[::-1] or all_bases):
        dcache = config.meeting_file(base, ".diar.npz")
        jf = config.meeting_file(base, ".json")
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

#!/usr/bin/env python
"""Re-apply speaker attribution to already-processed meetings using their CACHED
diarization — no re-transcription or re-diarization. Run after enrolling new
people (or after attribution-logic fixes) to update past meetings in seconds.

  ./run.sh relabel "LT Meeting 05212026"     # one meeting (basename, no extension)
  ./run.sh relabel --all                      # every meeting with a .diar.npz sidecar
  ./run.sh relabel --strict "Brenda ..."      # sensitive: flag, don't guess
"""
import argparse
import fcntl
import json
import sys
from pathlib import Path

from stt import (config, diarcache, diarize, identify, merge, output, punctuate,
                 refine, unknowns)


def relabel_one(base: str, strict=None, allowed_names=None) -> bool:
    from stt import status as _status
    active = {Path(k).stem for k in _status.read().get("active", {})}
    if base in active:
        # the batch is writing this file right now — and it reads voiceprints
        # fresh, so it will come out with the latest names anyway
        print(f"  skip {base}: being processed right now (will get names itself)")
        return False
    jpath = config.meeting_file(base, ".json")
    tpath = config.meeting_file(base, ".txt")
    dpath = config.meeting_file(base, ".diar.npz")
    if not jpath.exists():
        print(f"  skip {base}: missing .json")
        return False
    if not dpath.exists():
        print(f"  skip {base}: no .diar.npz cache (was it processed with diarization?)")
        return False
    strict = config.STRICT if strict is None else strict

    # held for the whole read -> recompute -> reapply -> write span, not just
    # the write: a GUI edit landing after we read but before we write would
    # otherwise be silently clobbered by our (by-then-stale) rewrite.
    from stt import review
    with review.lock_meeting(base):
        data = json.loads(jpath.read_text())
        words = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in data["words"]]
        # heal ASR hallucination loops in transcripts processed before the guard
        from stt import sanitize
        words, loop_spans = sanitize.collapse_repeats(words)

        vps = identify.load_voiceprints()
        if allowed_names is not None:
            vps = {n: s for n, s in vps.items() if n in allowed_names}
        raw_turns, turn_embeddings, cent_emb, overlaps = diarcache.load(dpath)
        cluster_names = ({k: v["name"] for k, v in
                          identify.name_speakers(cent_emb, allowed_names=allowed_names,
                                                 context=f"relabel:{base}").items()}
                         if vps else {k: None for k in cent_emb})
        cluster_names = refine.resolve_split_clusters(cluster_names, cent_emb, vps)
        turns, names, stats = diarize.build_attribution(
            raw_turns, turn_embeddings, cluster_names, vps if config.REFINE else {},
            cluster_centroids=cent_emb, words=words, strict=strict)
        uid_map = unknowns.assign(cent_emb, cluster_names, base)
        for label, uid in uid_map.items():
            if label in names and not names[label].get("name"):
                names[label]["global_id"] = uid
                names[label]["display"] = unknowns.display(uid)

        labels = sorted(names.keys())
        segments, labeled_words = merge.assign_and_group(words, turns, names,
                                                         overlaps=overlaps,
                                                         spans=stats.get("spans", []) + loop_spans)
        if config.PUNCTUATE:
            punctuate.restore_segments(segments)
        # engine-disagreement flags outlive the rebuild too (sidecar from verify mode)
        from stt import verify
        vc = verify.load_sidecar(base)
        if vc:
            verify.apply_flags(segments, vc.get("regions", []))
        data["segments"], data["words"] = segments, labeled_words
        # human review decisions outlive any relabel — reapply them onto the
        # freshly-rebuilt segments (accepts, text edits, speaker reassignments,
        # inserted/removed lines) and clear the flags they resolved
        review.reapply_decisions(base, data)
        segments, labeled_words = data["segments"], data["words"]
        # reapply_decisions may have added/reused MANUAL_n entries (people the
        # diarizer never heard, named by a human) on data["speakers"] — the roster
        # rebuild below only knows about diarized clusters, so fold those back in
        # or every manually-named speaker vanishes from the header/dropdown on
        # every relabel, and a later relabel would mint a fresh MANUAL_1 for
        # someone else since it no longer sees the one already in use.
        manual_speakers = [s for s in data.get("speakers", [])
                           if str(s.get("id", "")).startswith("MANUAL_")]
        data["speakers"] = output.build_speakers(labels, names) + manual_speakers
        data["refine_stats"] = {k: v for k, v in stats.items() if k != "spans"}
        data["strict"] = strict
        data["punctuated"] = bool(config.PUNCTUATE)
        data["overlap_spans"] = [[s, e] for s, e in overlaps]

        header = output.txt_header(data.get("source_file", base),
                                   data.get("duration_sec", 0), data["speakers"], strict)

        output.write_json(jpath, {k: v for k, v in data.items()
                                  if k not in ("speakers", "segments", "words")},
                          data["speakers"], segments, labeled_words)
        output.write_txt(tpath, segments, header=header)

    print(f"  {base}: " + ", ".join(s["display"] for s in data["speakers"])
          + (f"  [{stats['flagged']} flagged]" if stats.get("flagged") else ""))
    return True


PENDING_FLAG_NAME = "relabel_pending.flag"


def all_bases():
    return [b for b in config.meeting_bases()
            if config.meeting_file(b, ".diar.npz").exists()]


def relabel_all():
    """Relabel every cached meeting. Caller must already hold the batch lock
    (run_batch calls this at the end of a run to apply names given mid-run)."""
    for base in all_bases():
        try:
            relabel_one(base)
        except Exception as e:
            print(f"  FAILED {base}: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meetings", nargs="*", help="meeting basenames")
    ap.add_argument("--all", action="store_true", help="relabel every cached meeting")
    ap.add_argument("--strict", action="store_true",
                    help="no smoothing/open-set reassignment; flag instead")
    ap.add_argument("--speakers", help="comma-separated attendee names to allow")
    args = ap.parse_args()

    # Relabel runs CONCURRENTLY with a batch: every output write is atomic
    # (tmp + rename) and the batch only writes the file it is processing —
    # which relabel skips (see relabel_one). Names therefore apply to finished
    # transcripts immediately instead of waiting hours for a run to end. The
    # lock here only serializes relabel against ITSELF.
    lockfd = open(config.PROJECT_DIR / "relabel.lock", "w")
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # another relabel is mid-flight (possibly with older voiceprints) —
        # queue a follow-up; the GUI kicks it as soon as the lock frees
        (config.PROJECT_DIR / PENDING_FLAG_NAME).write_text("all")
        print("Another relabel is already running — a follow-up pass is queued.")
        return 0

    if args.all:
        bases = all_bases()
    else:
        bases = args.meetings
    if not bases:
        raise SystemExit("pass one or more meeting basenames, or --all")

    allowed = [s.strip() for s in args.speakers.split(",")] if args.speakers else None
    for base in bases:
        try:
            relabel_one(base, strict=args.strict or None, allowed_names=allowed)
        except Exception as e:
            print(f"  FAILED {base}: {e}", file=sys.stderr)
    if args.all:  # a single-meeting relabel must NOT clear a queued relabel-all
        (config.PROJECT_DIR / PENDING_FLAG_NAME).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Re-apply speaker attribution to already-processed meetings using their CACHED
diarization — no re-transcription or re-diarization. Run after enrolling new
people (or after attribution-logic fixes) to update past meetings in seconds.

  ./run.sh relabel "LT Meeting 05212026"     # one meeting (basename, no extension)
  ./run.sh relabel --all                      # every meeting with a .diar.npz sidecar
  ./run.sh relabel --strict "Hearing ..."     # sensitive: flag, don't guess
"""
import argparse
import fcntl
import json
import sys
from pathlib import Path

from stt import (channels, config, control, diarcache, diarize, identify, merge,
                 output, punctuate, refine, unknowns)


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
        # the existence checks above are a SNAPSHOT: a concurrent rename,
        # restamp or archive moves both files in the gap before these reads.
        # An unguarded read raised out of relabel_all's per-meeting handler,
        # which then reported a failure with a raw filesystem path in it.
        try:
            data = json.loads(jpath.read_text())
            words = [{"start": w["start"], "end": w["end"], "word": w["word"]}
                     for w in data["words"]]
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"  skip {base}: its transcript moved or is unreadable "
                  f"({type(e).__name__}) -- it will pick up the new name on the "
                  "next pass")
            return False
        # heal ASR hallucination loops in transcripts processed before the guard
        from stt import sanitize
        words, loop_spans = sanitize.collapse_repeats(words)

        vps = identify.load_voiceprints()
        if allowed_names is not None:
            vps = {n: s for n, s in vps.items() if n in allowed_names}
        try:
            raw_turns, turn_embeddings, cent_emb, overlaps = diarcache.load(dpath)
        except (OSError, ValueError, KeyError) as e:
            print(f"  skip {base}: its diarization cache moved or is unreadable "
                  f"({type(e).__name__}) -- it will pick up the new name on the "
                  "next pass")
            return False
        cluster_names = ({k: v["name"] for k, v in
                          identify.name_speakers(cent_emb, allowed_names=allowed_names,
                                                 context=f"relabel:{base}").items()}
                         if vps else {k: None for k in cent_emb})
        cluster_names = refine.resolve_split_clusters(cluster_names, cent_emb, vps)
        turns, names, stats = diarize.build_attribution(
            raw_turns, turn_embeddings, cluster_names, vps if config.REFINE else {},
            cluster_centroids=cent_emb, words=words, strict=strict)
        # meeting-local names: a human named a cluster the quality floor will
        # not let enroll (seconds of speech). The name is a transcript label
        # for THIS meeting only -- no voiceprint, no registry entry -- and it
        # outlives every relabel the way dismissed_voices does (a top-level
        # key, re-applied onto the rebuilt roster below). A Redo re-clusters
        # from scratch, so it resets there on purpose.
        local_names = {str(k): str(v) for k, v in
                       (data.get("local_names") or {}).items() if str(v).strip()}
        if not data.get("one_time_speakers"):
            # the registry must see a locally named cluster as NAMED, or a
            # floor-passing cluster the human labeled would still mint a
            # persistent voice sample -- the exact thing "a transcript label,
            # never a voiceprint" promises does not happen. The view is only
            # for assign(): build_attribution above must keep treating the
            # label as unmatched, because no voiceprint backs the name.
            assign_view = dict(cluster_names)
            for cid, nm in local_names.items():
                if cid in assign_view and not assign_view[cid]:
                    assign_view[cid] = nm
            uid_map = unknowns.assign(cent_emb, assign_view, base,
                                      stats=unknowns.talk_stats(raw_turns))
            for label, uid in uid_map.items():
                if label in names and not names[label].get("name"):
                    names[label]["global_id"] = uid
                    names[label]["display"] = unknowns.display(uid)
        # applied AFTER matching and unknown assignment: an unbacked name must
        # never look like a voiceprint match to refine, and the human's word
        # beats both.
        for cid, nm in local_names.items():
            if cid in names:
                names[cid]["name"] = nm
                names[cid]["display"] = nm
                names[cid]["global_id"] = None

        # channel-aware recordings: re-overlay the mic owner's turns from the
        # cache, RE-GATED against the CURRENT voiceprint — so un-enrolling the
        # mic speaker (or improving their prints) updates past meetings too. This
        # also RECOVERS a recording first processed before the mic speaker was
        # enrolled (mono_fallback_no_enroll cached its ungated spans): enrolling
        # + relabel now attributes them, no full re-transcription (C6). The
        # whole-file pass-fraction gate mirrors _plan_channels so a bleed / wrong
        # person still leaves the mono attribution alone.
        ch = diarcache.load_channel(dpath)
        if ch["mic_speaker"] and ch["spans"] is not None and len(ch["spans"]):
            mark_vp = identify.load_voiceprints().get(ch["mic_speaker"])
            kept, scores = [], []
            if mark_vp is not None:
                for sp, em in zip(ch["spans"], ch["embs"]):
                    if em is None:
                        continue
                    sc = identify.score_against(em, mark_vp)
                    if sc >= config.CHANNEL_FORCE_MIN:
                        kept.append(sp)
                        scores.append(sc)
            if kept and len(kept) / len(ch["spans"]) >= config.CHANNEL_PASS_FRACTION:
                turns, names, extra_ov = channels.combine_turns(
                    turns, names, kept, ch["mic_speaker"], sum(scores) / len(scores))
                overlaps = overlaps + extra_ov

        labels = sorted(names.keys())
        segments, labeled_words = merge.assign_and_group(
            words, turns, names, overlaps=overlaps,
            spans=stats.get("spans", []) + loop_spans,
            overlap_min_sec=0.0 if strict else config.OVERLAP_FLAG_MIN_SEC)
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
        # the roster entry says the name is meeting-local, so the panel can
        # keep its chip editable (re-saving is the typo fix) and never claim
        # the system knows this voice
        for s in data["speakers"]:
            if s.get("id") in local_names:
                s["local"] = True
        data["refine_stats"] = {k: v for k, v in stats.items() if k != "spans"}
        data["strict"] = strict
        data["punctuated"] = bool(config.PUNCTUATE)
        data["overlap_spans"] = [[s, e] for s, e in overlaps]

        header = output.txt_header(data.get("source_file", base),
                                   data.get("duration_sec", 0), data["speakers"],
                                   strict, data.get("processed_at"))

        output.write_json(jpath, {k: v for k, v in data.items()
                                  if k not in ("speakers", "segments", "words")},
                          data["speakers"], segments, labeled_words)
        output.write_txt(tpath, segments, header=header)

    print(f"  {base}: " + ", ".join(s["display"] for s in data["speakers"])
          + (f"  [{stats['flagged']} flagged]" if stats.get("flagged") else ""))
    return True


PENDING_FLAG_NAME = "relabel_pending.flag"
# The lock path and the "a pass is running" marker live together in stt.control:
# the panel has to answer "is a relabel running?" on every 2s poll and cannot
# import this module to ask (it pulls the whole pipeline in). control.relabel_*
# is the single source of both the path and the mechanism; see the comment there
# for why the probe reads a marker instead of testing the lock.


def all_bases():
    return [b for b in config.meeting_bases()
            if config.meeting_file(b, ".diar.npz").exists()]


def relabel_all():
    """Relabel every cached meeting. Caller must already hold the batch lock
    (run_batch calls this at the end of a run to apply names given mid-run).

    Takes the RELABEL lock too (blocking): without it, run_batch's end-of-run
    pass interleaved with a GUI-spawned `relabel --all` holding the lock —
    two passes re-running unknowns.assign over the same meetings at once,
    doubling the registry churn. Waiting is correct: by the time this
    returns, every naming made up to this instant has been applied."""
    with open(control.relabel_lock_path(), "w") as lockfd:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        try:
            # the marker rides INSIDE the lock: it says "a pass is running", and
            # the panel's "applying names" pill reads it (see stt.control)
            with control.relabel_marker():
                for base in all_bases():
                    try:
                        relabel_one(base)
                    except Exception as e:
                        print(f"  FAILED {base}: {e}", file=sys.stderr)
        finally:
            fcntl.flock(lockfd, fcntl.LOCK_UN)


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
    lockfd = open(control.relabel_lock_path(), "w")
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # another relabel is mid-flight (possibly with older voiceprints) —
        # queue a follow-up; the GUI kicks it as soon as the lock frees
        (config.PROJECT_DIR / PENDING_FLAG_NAME).write_text("all")
        print("Another relabel is already running — a follow-up pass is queued.")
        return 0

    if args.all:
        # consume the queued flag NOW, not at exit: this pass reads voiceprints
        # fresh per meeting, so it covers every naming made up to this instant.
        # A flag written DURING the pass (a naming landed mid-run, its own
        # relabel hit our lock and queued) must SURVIVE it — clearing the flag
        # at exit silently cancelled the promised follow-up, and names given
        # mid-pass never applied. (A single-meeting relabel must not consume
        # a queued relabel-all either way, hence inside `if args.all`.)
        (config.PROJECT_DIR / PENDING_FLAG_NAME).unlink(missing_ok=True)
        bases = all_bases()
    else:
        bases = args.meetings
    if not bases:
        raise SystemExit("pass one or more meeting basenames, or --all")

    allowed = [s.strip() for s in args.speakers.split(",")] if args.speakers else None
    # marker up for the whole pass (single meeting or --all), so the panel can
    # say "applying names" for exactly as long as this is really happening. Only
    # a pass that WON the lock publishes it: the queue-a-follow-up path above
    # returned before this and must not claim to be running.
    with control.relabel_marker():
        for base in bases:
            try:
                relabel_one(base, strict=args.strict or None, allowed_names=allowed)
            except Exception as e:
                print(f"  FAILED {base}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Process one audio file: transcribe -> diarize -> name -> merge -> punctuate -> write.

This never touches the iCloud original; the batch orchestrator owns the
copy/move/delete lifecycle. Output writes are atomic (see output._atomic_write).
"""
import datetime as _dt
import os
from pathlib import Path

import numpy as np

from . import (audio, channels, config, diarcache, diarize, identify, merge,
               output, sanitize, punctuate, unknowns, verify)


def _load_asr(strict=False):
    backend = config.ASR_BACKEND
    if backend.startswith("cloud:"):
        if strict:
            # sensitive recordings NEVER leave the machine, whatever the
            # global engine setting says — strict mode always runs local
            print("   strict mode: cloud transcription disabled for this file"
                  " — using the local engine", flush=True)
        else:
            from . import asr_cloud
            return asr_cloud
    if backend == "mlxwhisper":
        from . import asr_mlxwhisper as asr
    else:
        from . import asr_parakeet as asr
    return asr


def _transcribe_with_fallback(asr, wav, progress=None) -> dict:
    """A cloud engine failing (network down, quota, bad key, size cap) must
    degrade to the local engine, not kill the nightly run. Local engine
    errors still propagate — there is nothing to fall back to."""
    try:
        return asr.transcribe(wav, progress=progress)
    except Exception as e:
        if not getattr(asr, "IS_CLOUD", False):
            raise
        print(f"   cloud transcription failed ({e}) — falling back to the "
              "local engine", flush=True)
        from . import asr_parakeet
        return asr_parakeet.transcribe(wav, progress=progress)


def _meeting_date(src: Path, existing_json: Path = None) -> str:
    """Reprocessing preserves an already-stored date: it was either stamped
    from this same filename originally or corrected by a human in the panel —
    a Redo must never silently undo that correction. Fresh files derive from
    the filename convention, else the source mtime."""
    import json as _json
    from datetime import date

    from . import dates
    if existing_json is not None and existing_json.exists():
        try:
            kept = _json.loads(existing_json.read_text()).get("date")
            if kept:
                return kept
        except (OSError, ValueError):
            pass
    try:
        fallback = date.fromtimestamp(src.stat().st_mtime).isoformat()
    except OSError:
        fallback = date.today().isoformat()
    # a recording can't be from the future: a filename like "...10242026"
    # parses to Oct 24 2026, which would sort above today's real meetings.
    # Reject a future parse and fall back to the processing/mtime date.
    parsed = dates.meeting_date(src.stem)
    if parsed and parsed <= date.today().isoformat():
        return parsed
    return fallback


def _src_mtime(src: Path):
    try:
        return round(src.stat().st_mtime, 3)
    except OSError:
        return None


def _owns_meeting(base: str, src: Path, dest_dir) -> bool:
    """Is meeting `base` THIS source file's own meeting (so a reprocess should
    land back in it), or a different recording that merely shares the name?
    Identity = the stored source_file name matches AND the recorded source
    mtime matches (copy2 preserves it, so a Redo from the stored audio agrees;
    a same-named re-export is a new file with a new mtime and does NOT match).
    Meetings from before source_mtime was recorded fall back to the name match
    — their Redo keeps working; they just lack same-name re-export protection."""
    import json as _json
    try:
        d = _json.loads(config.meeting_file(base, ".json", dest_dir).read_text())
    except (OSError, ValueError):
        return False
    if d.get("source_file") != src.name:
        return False
    sm = d.get("source_mtime")
    if sm is None:
        return True  # legacy meeting: name match is all the identity there is
    try:
        return abs(float(sm) - src.stat().st_mtime) < 1.0
    except (OSError, ValueError):
        return False


def resolve_base(src: Path, dest_dir=None) -> str:
    """The meeting folder name for this source file.

    A NEW recording whose FILENAME carries no date gets the meeting's date stamped
    into the folder: 'LT Weekly Meeting' -> 'LT Weekly Meeting 06042026'. That is
    what stops two recordings of the SAME recurring meeting — which naturally share
    a filename — from resolving to the same folder and silently overwriting each
    other's transcript.

    Identity rules, in order:
    - A Redo (the source IS a meeting's stored audio, living inside its folder)
      always resolves to that meeting — nothing is renamed behind the user's back.
    - A name already on disk is reused ONLY if _owns_meeting says this exact
      recording created it; a different recording that merely shares the name
      (same title, same date, a same-day second session) gets ' (2)' instead of
      silently overwriting a transcript.
    - Archived names count as taken (registries reference meetings by name;
      a live meeting reusing an archived name would corrupt a later restore)."""
    from . import dates
    dest_dir = Path(dest_dir) if dest_dir else config.MEETINGS_DIR
    base = src.stem
    try:  # a Redo hands us the stored audio inside the meeting's own folder
        if src.resolve().parent == config.meeting_dir(base, dest_dir).resolve():
            return base
    except OSError:
        pass
    # this file's own meeting already lives under the plain name (a legacy
    # pre-stamping meeting reprocessed from the watched folder): stay there —
    # stamping now would strand the transcript in a NEW folder as a duplicate
    if (config.meeting_file(base, ".json", dest_dir).exists()
            and _owns_meeting(base, src, dest_dir)):
        return base
    target = base
    if dates.meeting_date(base) is None:  # plain name: stamp the meeting's date
        target = dates.stamp(base, _meeting_date(src, None))
    archive = config.archive_dir(dest_dir)
    i = 1
    while True:
        cand = target if i == 1 else f"{target} ({i})"
        i += 1
        if config.meeting_file(cand, ".json", dest_dir).exists():
            if _owns_meeting(cand, src, dest_dir):
                return cand              # this recording's own meeting: reuse it
            continue                     # someone else's transcript: never clobber
        if (archive / cand).exists():
            continue                     # archived meetings keep their names too
        # free — OR an empty shell left by a run that died after process_file's
        # mkdir but before it wrote the json. Reuse it. Treating a shell as taken
        # made every failed retry claim the NEXT suffix, so one file that failed
        # four times left (2)(3)(4)(5) behind it.
        return cand


def _resolve_channel(src: Path, existing_json: Path, input_opts):
    """(channel_layout, mic_speaker) for a stereo me/them recording, or
    (None, None). Precedence mirrors _meeting_date: explicit input_opts, then
    the stored meeting json (so a Redo keeps the mode after the source sidecar
    is gone), then a <base>.opts.json sidecar next to a fresh source file."""
    import json as _json

    def _pick(d):
        return (d or {}).get("channel_layout"), (d or {}).get("mic_speaker")

    cl, ms = _pick(input_opts)
    if cl:
        return cl, ms
    for path in (existing_json, src.with_suffix(".opts.json")):
        if path and path.exists():
            try:
                cl, ms = _pick(_json.loads(path.read_text()))
                if cl:
                    return cl, ms
            except (OSError, ValueError):
                pass
    return None, None


def _plan_channels(base, src, mic_speaker):
    """Decide whether to run the channel-aware path and prepare its inputs.
    Returns (mode, wav_mic, wav_sys, plan, stats) where plan is
    {"spans","embs","score"} when mode == 'stereo_channel_aware' else None.
    The two split WAVs are always returned (for cleanup) when created."""
    wav_mic = config.WORK_DIR / f"{base}.mic.16k.wav"
    wav_sys = config.WORK_DIR / f"{base}.sys.16k.wav"
    audio.to_wav16k_channel(src, wav_mic, 0)
    audio.to_wav16k_channel(src, wav_sys, 1)
    stats = channels.sanity(wav_mic, wav_sys)
    if stats["dual_mono"]:
        return "mono_fallback_dual_mono", wav_mic, wav_sys, None, stats
    if stats["sys_dead"]:
        return "mono_fallback_sys_dead", wav_mic, wav_sys, None, stats
    mark_vp = identify.load_voiceprints().get(mic_speaker)
    spans = channels.mic_spans(wav_mic, wav_sys)
    embs = diarize.embed_spans(wav_mic, spans)
    stats["n_mic_spans"] = len(spans)
    if mark_vp is None:
        # the mic speaker is not enrolled yet: without a voiceprint to gate them
        # this pass falls back to mono. But cache the (ungated) spans + their
        # embeddings so enrolling them later and running relabel reconstructs the
        # mic overlay in seconds, no full re-transcription needed (C6).
        plan = {"spans": spans, "embs": embs, "score": None} if spans else None
        return "mono_fallback_no_enroll", wav_mic, wav_sys, plan, stats
    kept, kept_embs, scores = [], [], []
    for sp, em in zip(spans, embs):
        if em is None:
            continue
        sc = identify.score_against(em, mark_vp)
        if sc >= config.CHANNEL_FORCE_MIN:
            kept.append(sp)
            kept_embs.append(em)
            scores.append(sc)
    frac = (len(kept) / len(spans)) if spans else 0.0
    stats.update({"n_kept": len(kept), "pass_fraction": round(frac, 3)})
    if not spans:
        # nobody dominated the mic at all (an all-listening meeting, or the mic
        # speaker never spoke) -> nothing to overlay; process as mono
        return "mono_fallback_no_me", wav_mic, wav_sys, None, stats
    if frac < config.CHANNEL_PASS_FRACTION:
        # candidates existed but too few match the enrolled voice -> heavy bleed
        # or a wrong mapping; the split can't be trusted, use the mono mix
        return "mono_fallback_bleed", wav_mic, wav_sys, None, stats
    plan = {"spans": kept, "embs": kept_embs, "score": float(sum(scores) / len(scores))}
    return "stereo_channel_aware", wav_mic, wav_sys, plan, stats


def process_file(src, dest_dir=None, do_diarize=True, save_embeddings=True,
                 strict=None, allowed_names=None, report=None, do_verify=None,
                 num_speakers=None, min_speakers=None, max_speakers=None,
                 track_unknowns=True, input_opts=None) -> dict:
    src = Path(src)
    report = report or (lambda *a, **k: None)
    strict = config.STRICT if strict is None else strict
    do_verify = config.VERIFY if do_verify is None else do_verify
    dest_dir = Path(dest_dir) if dest_dir else config.MEETINGS_DIR
    base = resolve_base(src, dest_dir)
    config.meeting_dir(base, dest_dir).mkdir(parents=True, exist_ok=True)
    txt_path = config.meeting_file(base, ".txt", dest_dir)
    json_path = config.meeting_file(base, ".json", dest_dir)
    emb_path = config.meeting_file(base, ".emb.npz", dest_dir)
    diar_path = config.meeting_file(base, ".diar.npz", dest_dir)

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav = config.WORK_DIR / f"{base}.16k.wav"
    # channel-aware mode is opt-in per recording (the meeting recorder's stereo
    # me/them layout + an enrolled mic speaker); everything else is byte-for-byte
    # today's mono path.
    channel_layout, mic_speaker = _resolve_channel(src, json_path, input_opts)
    channel_mode, channel_stats = "mono", None
    wav_mic = wav_sys = None
    mic_plan = None
    try:
        dur = audio.duration_sec(src)  # probe the source so ETA is known up front
    except Exception:
        dur = None
    report("converting", 0.0, dur)
    try:
        # inside the try: a failed/partial conversion (disk full, corrupt
        # source, killed mid-write) must still hit the finally below and
        # clean up the scratch WAV, not leak it until the next batch's
        # clean_scratch() — worse, it would leak on every bad file in a run.
        audio.to_wav16k(src, wav)  # the mono MIX — ASR input, and diar input in mono mode
        if dur is None:
            dur = audio.duration_sec(wav)
            report("converting", 1.0, dur)

        if (channel_layout == "mic_left_system_right" and mic_speaker and do_diarize
                and audio.probe_channels(src) >= 2):
            channel_mode, wav_mic, wav_sys, mic_plan, channel_stats = _plan_channels(
                base, src, mic_speaker)

        report("transcribing", 0.0)
        asr = _load_asr(strict)
        asr_out = _transcribe_with_fallback(
            asr, wav, progress=lambda f: report("transcribing", f))
        # collapse ASR hallucination loops before anything downstream sees them
        words, loop_spans = sanitize.collapse_repeats(asr_out["words"])

        turns, labels, names, overlaps, diar = [], [], {}, [], None
        if do_diarize:
            report("diarizing", 0.0)
            # channel-aware: diarize the SYSTEM channel only (the remote
            # participants), so the mic owner's speech never joins their
            # clusters and me-vs-them overlap disappears.
            diar_wav = wav_sys if channel_mode == "stereo_channel_aware" else wav
            diar = diarize.diarize(diar_wav, strict=strict, words=words,
                                   allowed_names=allowed_names, context=base,
                                   num_speakers=num_speakers,
                                   min_speakers=min_speakers, max_speakers=max_speakers,
                                   progress=lambda f: report("diarizing", f))
            turns, labels, names = diar["turns"], diar["labels"], diar["names"]
            overlaps = diar["overlaps"]
            if channel_mode == "stereo_channel_aware":
                # overlay the mic owner's turns onto the system diarization
                turns, names, extra_ov = channels.combine_turns(
                    turns, names, mic_plan["spans"], mic_speaker, mic_plan["score"])
                overlaps = overlaps + extra_ov
                labels = labels + [channels.MIC_ID]  # so build_speakers emits the mic speaker
                channel_stats["n_mic_turns"] = sum(
                    1 for t in turns if t["speaker"] == channels.MIC_ID)
            if track_unknowns:
                # stable global numbering for unknown voices across meetings
                uid_map = unknowns.assign(diar["embeddings"], diar["cluster_names"], base)
                for label, uid in uid_map.items():
                    if label in names and not names[label].get("name"):
                        names[label]["global_id"] = uid
                        names[label]["display"] = unknowns.display(uid)
            # else: one-time speakers (focus groups) — unnamed voices keep
            # transcript-local "Speaker N" labels and are never registered
            # globally; enrolling later from this meeting's caches still works

        report("writing", 0.2)
        spans = (diar["refine_stats"].get("spans", []) if diar else []) + loop_spans
        segments, labeled_words = merge.assign_and_group(
            words, turns, names, overlaps=overlaps, spans=spans,
            overlap_min_sec=0.0 if strict else config.OVERLAP_FLAG_MIN_SEC)
        if config.PUNCTUATE:
            punctuate.restore_segments(segments)
        speakers = output.build_speakers(labels, names)

        verify_engine, verify_regions = None, None
        if do_verify and words:
            report("verifying", 0.0)
            try:
                verify_regions, verify_engine = verify.run(
                    wav, words, asr_out["engine"], progress=lambda f: report("verifying", f))
                verify.apply_flags(segments, verify_regions)
            except Exception as e:
                # verification is a bonus pass — its failure must never cost
                # the transcript itself
                print(f"   verify pass failed ({e}); transcript kept without "
                      "second-opinion flags", flush=True)
                verify_regions, verify_engine = None, None
    finally:
        wav.unlink(missing_ok=True)
        if wav_mic:
            wav_mic.unlink(missing_ok=True)
        if wav_sys:
            wav_sys.unlink(missing_ok=True)

    named = [s["name"] for s in speakers if s["name"]]
    # when transcription actually ran (initial OR redo). Distinct from
    # generated_at, which write_json re-stamps on every save including a review
    # edit — this one is set only here and preserved by relabel and by edits, so
    # it answers "when was this last transcribed". Shared by the txt header.
    processed_at = _dt.datetime.now().isoformat(timespec="seconds")
    header = output.txt_header(src.name, dur, speakers, strict, processed_at)

    # Hold this meeting's per-base lock for the WHOLE write phase: the date
    # resolution (which reads the stored json to preserve a human's correction),
    # decision archiving, and every output/cache write. Without it a Redo raced
    # a concurrent panel edit (set_date / review) or a relabel on the SAME base
    # — all of which take this exact lock — so whichever write landed last
    # silently won, or a rename moved the folder out from under these writes.
    # The lock spans only this fast tail, never the minutes of ASR/diarization.
    from . import review
    with review.lock_meeting(base):
        # human-owned flags survive a Redo exactly like the corrected date: the
        # Work/Personal category and the reviewed state were set in the panel, and
        # rebuilding the meta from scratch must not silently clear them.
        # A brand-new meeting has no prior json, so it lands reviewed=False and
        # queues in the panel's inbox for naming/dating rather than dropping
        # unannounced into a list of a hundred. (Meetings processed before this
        # existed carry NO reviewed key at all, and the panel treats a missing key
        # as already-reviewed — so nothing retroactively floods the inbox.)
        category, reviewed = None, False
        try:
            import json as _json
            prev = _json.loads(json_path.read_text())
            category = prev.get("category")
            reviewed = prev.get("reviewed", True)  # an existing meeting stays put
        except (OSError, ValueError):
            pass
        meta = {
            "source_file": src.name,
            # identity, not display: resolve_base matches (name, mtime) to tell
            # "this exact recording, reprocessed" from "a different recording
            # that shares the filename" (copy2 preserves mtime, so a Redo from
            # the stored audio still matches; a same-named re-export does not)
            "source_mtime": _src_mtime(src),
            # resolved ONCE, here: filename convention (MMDDYYYY) is the honest
            # signal — file mtime/creation_time reflect the Voice Memos EXPORT,
            # often weeks after the meeting. Grouping and sorting read this
            # stored field; a human can correct it in the panel. Read under the
            # lock so a set_meeting_date landing mid-write is not clobbered.
            "date": _meeting_date(src, json_path),
            "processed_at": processed_at,
            "duration_sec": round(dur, 1),
            "asr_engine": asr_out["engine"],
            "diarizer": config.DIARIZATION_MODEL if do_diarize else None,
            "n_speakers": len(labels),
            "strict": strict,
            "one_time_speakers": not track_unknowns,
            "punctuated": bool(config.PUNCTUATE),
            "verify_engine": verify_engine,
            "overlap_spans": [[s, e] for s, e in overlaps],
            "refine_stats": diar["refine_stats"] if diar else None,
        }
        if category:
            meta["category"] = category
        meta["reviewed"] = bool(reviewed)
        if channel_layout:  # a recording that declared a me/them layout
            meta["channel_layout"] = channel_layout
            meta["mic_speaker"] = mic_speaker
            meta["channel_mode"] = channel_mode  # stereo_channel_aware or a mono_fallback_*
            if channel_stats:
                meta["channel_stats"] = channel_stats
        # a redo invalidates old review decisions (new cluster ids) and any stale
        # verify regions (new word timings) — archive/remove them, never half-apply
        review.archive_decisions(base, dest_dir=dest_dir)
        if verify_regions is None:
            verify.sidecar_path(base, dest_dir).unlink(missing_ok=True)

        output.write_txt(txt_path, segments, header=header)
        output.write_json(json_path, meta, speakers, segments, labeled_words)
        if verify_regions is not None:
            # sidecar: relabel rebuilds segments from the diar cache and re-flags from this
            verify.save_sidecar(base, verify_regions, verify_engine, dest_dir=dest_dir)

        saved_emb = None
        if save_embeddings and diar and diar.get("embeddings"):
            # atomic: write to a temp sibling then os.replace, so a crash mid-write
            # can't leave a truncated cache next to a complete transcript. Pass a
            # file handle (not the path) so np.savez does not append its own .npz to
            # the .tmp name and leave os.replace chasing a file that isn't there.
            emb_tmp = emb_path.with_suffix(emb_path.suffix + ".tmp")
            with open(emb_tmp, "wb") as fh:
                np.savez(fh, **{k: v for k, v in diar["embeddings"].items()})
            os.replace(emb_tmp, emb_path)
            saved_emb = emb_path
            # cache raw turns + per-turn embeddings + overlaps so relabel can
            # re-attribute later without re-diarizing. Persist the SYSTEM-channel
            # overlaps only (not the mic/system double-talk added by combine) plus
            # the mic spans + their embeddings, so relabel reconstructs the exact
            # same overlay by re-gating against the CURRENT voiceprint.
            mark = mic_plan or {}
            diarcache.save(diar_path, diar["raw_turns"], diar["turn_embeddings"],
                           diar["embeddings"], overlaps=diar["overlaps"],
                           mark_spans=mark.get("spans"), mark_embs=mark.get("embs"),
                           channel_mode=channel_mode if channel_layout else None,
                           mic_speaker=mic_speaker)

    return {
        "base": base, "txt": txt_path, "json": json_path, "emb": saved_emb,
        "duration_sec": dur, "n_speakers": len(labels), "identified": named,
    }

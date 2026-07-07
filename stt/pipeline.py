"""Process one audio file: transcribe -> diarize -> name -> merge -> punctuate -> write.

This never touches the iCloud original; the batch orchestrator owns the
copy/move/delete lifecycle. Output writes are atomic (see output._atomic_write).
"""
from pathlib import Path

import numpy as np

from . import (audio, config, diarcache, diarize, merge, output, sanitize,
               punctuate, unknowns, verify)


def _load_asr():
    if config.ASR_BACKEND == "mlxwhisper":
        from . import asr_mlxwhisper as asr
    else:
        from . import asr_parakeet as asr
    return asr


def process_file(src, dest_dir=None, do_diarize=True, save_embeddings=True,
                 strict=None, allowed_names=None, report=None, do_verify=None,
                 num_speakers=None, min_speakers=None, max_speakers=None) -> dict:
    src = Path(src)
    report = report or (lambda *a, **k: None)
    strict = config.STRICT if strict is None else strict
    do_verify = config.VERIFY if do_verify is None else do_verify
    dest_dir = Path(dest_dir) if dest_dir else config.MEETINGS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = src.stem
    txt_path = dest_dir / f"{base}.txt"
    json_path = dest_dir / f"{base}.json"
    emb_path = dest_dir / f"{base}.emb.npz"
    diar_path = dest_dir / f"{base}.diar.npz"

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav = config.WORK_DIR / f"{base}.16k.wav"
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
        audio.to_wav16k(src, wav)
        if dur is None:
            dur = audio.duration_sec(wav)
            report("converting", 1.0, dur)

        report("transcribing", 0.0)
        asr = _load_asr()
        asr_out = asr.transcribe(wav, progress=lambda f: report("transcribing", f))
        # collapse ASR hallucination loops before anything downstream sees them
        words, loop_spans = sanitize.collapse_repeats(asr_out["words"])

        turns, labels, names, overlaps, diar = [], [], {}, [], None
        if do_diarize:
            report("diarizing", 0.0)
            diar = diarize.diarize(wav, strict=strict, words=words,
                                   allowed_names=allowed_names, context=base,
                                   num_speakers=num_speakers,
                                   min_speakers=min_speakers, max_speakers=max_speakers,
                                   progress=lambda f: report("diarizing", f))
            turns, labels, names = diar["turns"], diar["labels"], diar["names"]
            overlaps = diar["overlaps"]
            # stable global numbering for unknown voices across meetings
            uid_map = unknowns.assign(diar["embeddings"], diar["cluster_names"], base)
            for label, uid in uid_map.items():
                if label in names and not names[label].get("name"):
                    names[label]["global_id"] = uid
                    names[label]["display"] = unknowns.display(uid)

        report("writing", 0.2)
        spans = (diar["refine_stats"].get("spans", []) if diar else []) + loop_spans
        segments, labeled_words = merge.assign_and_group(words, turns, names,
                                                         overlaps=overlaps, spans=spans)
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

    named = [s["name"] for s in speakers if s["name"]]
    header = output.txt_header(src.name, dur, speakers, strict)

    meta = {
        "source_file": src.name,
        "duration_sec": round(dur, 1),
        "asr_engine": asr_out["engine"],
        "diarizer": config.DIARIZATION_MODEL if do_diarize else None,
        "n_speakers": len(labels),
        "strict": strict,
        "punctuated": bool(config.PUNCTUATE),
        "verify_engine": verify_engine,
        "overlap_spans": [[s, e] for s, e in overlaps],
        "refine_stats": diar["refine_stats"] if diar else None,
    }
    # a redo invalidates old review decisions (new cluster ids) and any stale
    # verify regions (new word timings) — archive/remove them, never half-apply
    from . import review
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
        np.savez(emb_path, **{k: v for k, v in diar["embeddings"].items()})
        saved_emb = emb_path
        # cache raw turns + per-turn embeddings + overlaps so relabel can
        # re-attribute later without re-diarizing
        diarcache.save(diar_path, diar["raw_turns"], diar["turn_embeddings"],
                       diar["embeddings"], overlaps=overlaps)

    return {
        "base": base, "txt": txt_path, "json": json_path, "emb": saved_emb,
        "duration_sec": dur, "n_speakers": len(labels), "identified": named,
    }

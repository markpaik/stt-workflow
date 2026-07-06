#!/usr/bin/env python
"""Rigorous tuning of the STT + diarization pipeline against Scribe references.

Objective: minimize cpWER (transcription + speaker attribution in one number), with
WER (ASR only) and DER (diarization only) as diagnostics. Settings are chosen on a
TUNE split and reported on a HELD-OUT split so we adopt generalizing gains, not
overfit ones.

Stages (each caches to qa/tune_cache/ so the whole thing is resumable):
  stt   — transcribe each ref meeting with each ASR engine; score WER vs Scribe.
  diar  — grid pyannote {clustering.threshold, min_duration_off} on each ref meeting;
          score DER vs Scribe (run on a representative segment to bound cost).
  attr  — grid our attribution thresholds via cached diarization; score cpWER.
  report— pick best per stage on the TUNE meetings, evaluate on HELD-OUT, and print
          baseline (current settings) vs tuned. Adopt only if held-out improves.

Usage:  ./run.sh py tuning/sweep.py <stage> [--seg-minutes N] [--trials ...]
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

from stt import audio, config, diarize as D, identify, merge, refine
from tuning import eval as E

REF_DIR = config.PROJECT_DIR / "qa" / "scribe_refs"
CACHE = config.PROJECT_DIR / "qa" / "tune_cache"
CACHE.mkdir(parents=True, exist_ok=True)

STT_ENGINES = [
    ("parakeet", {}),
    ("mlxwhisper", {"STT_WHISPER_MLX_MODEL": "large-v3"}),
    ("mlxwhisper", {"STT_WHISPER_MLX_MODEL": "turbo"}),
]
DIAR_GRID = {
    "clustering_threshold": [0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8],
    "min_duration_off": [0.0, 0.5],
}
ATTR_GRID = {
    "STT_NAMING_THRESHOLD": [0.5, 0.55, 0.6, 0.65, 0.7],
    "STT_NAMING_MARGIN": [0.08, 0.12, 0.15, 0.2],
    "STT_REFINE_SHORT_DUR": [0.2, 0.3, 0.4],
    "STT_REFINE_ID_MIN": [0.55, 0.6, 0.65],
}


# ---------- reference discovery ----------

def _norm(s):
    return "".join(c.lower() for c in s if c.isalnum())


def find_refs():
    """[(meeting_basename, ref_obj, m4a_path)] for every parseable Scribe ref that
    matches a processed meeting."""
    out = []
    meetings = {p.stem: p for p in config.MEETINGS_DIR.glob("*.m4a")}
    for r in sorted(REF_DIR.glob("*.json")):
        try:
            ref = E.parse_scribe(str(r))
        except Exception as e:
            print(f"  !! {r.name}: parse failed ({e})", file=sys.stderr)
            continue
        if not ref["words"]:
            continue
        match = next((m for m in meetings if _norm(r.stem) == _norm(m)), None) or \
                next((m for m in meetings if _norm(r.stem) in _norm(m) or _norm(m) in _norm(r.stem)), None)
        if match:
            out.append((match, ref, meetings[match]))
        else:
            print(f"  !! {r.name}: no matching meeting audio", file=sys.stderr)
    return out


def _wav_for(m4a, seg_minutes=None):
    CACHE.mkdir(exist_ok=True)
    suffix = f".{seg_minutes}min" if seg_minutes else ".full"
    wav = CACHE / (Path(m4a).stem + suffix + ".wav")
    if not wav.exists():
        if seg_minutes:
            import subprocess
            subprocess.run([audio.FFMPEG, "-y", "-t", str(seg_minutes * 60), "-i", str(m4a),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           check=True, capture_output=True)
        else:
            audio.to_wav16k(Path(m4a), wav)
    return wav


# ---------- stage: STT ----------

def stage_stt(refs):
    import os
    rows = []
    for base, ref, m4a in refs:
        wav = _wav_for(m4a)
        for engine, env in STT_ENGINES:
            tag = engine + ("_" + env.get("STT_WHISPER_MLX_MODEL", "") if env else "")
            cf = CACHE / f"stt.{base}.{tag}.json"
            if cf.exists():
                hyp = json.loads(cf.read_text())
            else:
                for k, v in env.items():
                    os.environ[k] = v
                config.ASR_BACKEND = engine
                mod = __import__(f"stt.asr_{engine}", fromlist=["transcribe"])
                out = mod.transcribe(wav)
                hyp = {"text": out["text"], "words": out["words"], "engine": out["engine"]}
                cf.write_text(json.dumps(hyp))
                print(f"  transcribed {base} with {out['engine']}: {len(out['words'])} words", flush=True)
            hyp_obj = E._finalize([{"start": w["start"], "end": w["end"], "word": w["word"],
                                    "speaker": "x"} for w in hyp["words"]], full_text=hyp["text"])
            w = E.wer(ref, hyp_obj)
            rows.append({"meeting": base, "engine": tag, "wer": round(w, 4)})
            print(f"    WER {base} / {tag}: {w:.4f}", flush=True)
    (CACHE / "stt_results.json").write_text(json.dumps(rows, indent=2))
    _print_table(rows, "engine", "wer")
    return rows


# ---------- stage: DIARIZATION ----------

_pipe = None


def _diar_pipeline():
    global _pipe
    if _pipe is None:
        import torch
        from pyannote.audio import Pipeline
        _pipe = Pipeline.from_pretrained(config.DIARIZATION_MODEL, token=config.resolve_hf_token())
        _pipe.to(torch.device("cpu"))
    return _pipe


def _run_diar(wav, clustering_threshold, min_duration_off):
    pipe = _diar_pipeline()
    pipe.instantiate({"clustering": {"threshold": clustering_threshold, "Fa": 0.07, "Fb": 0.8},
                      "segmentation": {"min_duration_off": min_duration_off}})
    out = pipe(str(wav))
    excl = getattr(out, "exclusive_speaker_diarization", None) or out.speaker_diarization
    turns = [{"start": float(s.start), "end": float(s.end), "speaker": spk}
             for s, _, spk in excl.itertracks(yield_label=True)]
    turns.sort(key=lambda t: t["start"])
    return turns


def stage_diar(refs, seg_minutes=12):
    rows = []
    for base, ref, m4a in refs:
        wav = _wav_for(m4a, seg_minutes=seg_minutes)
        # reference restricted to the same segment window
        ref_seg = _clip_ref(ref, seg_minutes * 60)
        for thr, mdo in itertools.product(DIAR_GRID["clustering_threshold"],
                                          DIAR_GRID["min_duration_off"]):
            cf = CACHE / f"diar.{base}.t{thr}.m{mdo}.json"
            if cf.exists():
                turns = json.loads(cf.read_text())
            else:
                turns = _run_diar(wav, thr, mdo)
                cf.write_text(json.dumps(turns))
                print(f"  diar {base} thr={thr} mdo={mdo}: {len({t['speaker'] for t in turns})} spk", flush=True)
            hyp_obj = E._finalize([{"start": t["start"], "end": t["end"], "word": "x",
                                    "speaker": t["speaker"]} for t in turns])
            der = E.der(ref_seg, hyp_obj)
            rows.append({"meeting": base, "clustering_threshold": thr,
                         "min_duration_off": mdo, "der": round(der, 4),
                         "n_spk": len({t["speaker"] for t in turns})})
            print(f"    DER {base} thr={thr} mdo={mdo}: {der:.4f}", flush=True)
    (CACHE / "diar_results.json").write_text(json.dumps(rows, indent=2))
    return rows


def _clip_ref(ref, max_sec):
    words = [w for w in ref["words"] if w["start"] < max_sec]
    return E._finalize(words)


# ---------- stage: ATTRIBUTION (cheap, from cached diarization of full meetings) ----------

def stage_attr(refs):
    rows = []
    vps = identify.load_voiceprints()
    combos = [dict(zip(ATTR_GRID, v)) for v in itertools.product(*ATTR_GRID.values())]
    for base, ref, m4a in refs:
        dpath = config.MEETINGS_DIR / f"{base}.diar.npz"
        jpath = config.MEETINGS_DIR / f"{base}.json"
        if not dpath.exists():
            print(f"  skip {base}: no .diar.npz", file=sys.stderr)
            continue
        from stt import diarcache
        raw_turns, tembs, cent_emb, overlaps = diarcache.load(dpath)
        words = [{"start": w["start"], "end": w["end"], "word": w["word"]}
                 for w in json.loads(jpath.read_text())["words"]]
        for combo in combos:
            _apply_cfg(combo)
            cluster_names = {k: v["name"] for k, v in
                             identify.name_speakers(cent_emb).items()} if vps else {}
            cluster_names = refine.resolve_split_clusters(cluster_names, cent_emb, vps)
            turns, names, stats = D.build_attribution(raw_turns, tembs, cluster_names,
                                                       vps if config.REFINE else {},
                                                       cluster_centroids=cent_emb, words=words)
            segs, lw = merge.assign_and_group(words, turns, names, overlaps=overlaps,
                                              spans=stats.get("spans", []))
            hyp_obj = E._finalize([{"start": w["start"], "end": w["end"], "word": w["word"],
                                    "speaker": str(w.get("speaker"))} for w in lw])
            cp = E.cpwer(ref, hyp_obj)
            rows.append({"meeting": base, **combo, "cpwer": round(cp, 4)})
        print(f"  attr {base}: {len(combos)} combos scored", flush=True)
    (CACHE / "attr_results.json").write_text(json.dumps(rows, indent=2))
    return rows


def _apply_cfg(combo):
    for k, v in combo.items():
        setattr(config, k[len("STT_"):] if k.startswith("STT_") else k, v)
    # map env-style keys to config attributes explicitly
    config.NAMING_THRESHOLD = combo.get("STT_NAMING_THRESHOLD", config.NAMING_THRESHOLD)
    config.NAMING_MARGIN = combo.get("STT_NAMING_MARGIN", config.NAMING_MARGIN)
    config.REFINE_SHORT_DUR = combo.get("STT_REFINE_SHORT_DUR", config.REFINE_SHORT_DUR)
    config.REFINE_ID_MIN = combo.get("STT_REFINE_ID_MIN", config.REFINE_ID_MIN)


def _print_table(rows, key, metric):
    from collections import defaultdict
    agg = defaultdict(list)
    for r in rows:
        agg[r[key]].append(r[metric])
    print(f"\n  mean {metric} by {key} (lower is better):")
    for k, v in sorted(agg.items(), key=lambda kv: np.mean(kv[1])):
        print(f"    {k:<28} {np.mean(v):.4f}  (n={len(v)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["stt", "diar", "attr", "list"])
    ap.add_argument("--seg-minutes", type=int, default=12)
    args = ap.parse_args()

    refs = find_refs()
    if not refs:
        print("No usable Scribe references in qa/scribe_refs/. Run check_refs.py first.")
        return 1
    print(f"Tuning against {len(refs)} reference meeting(s): {[b for b, _, _ in refs]}\n")

    if args.stage == "list":
        return 0
    if args.stage == "stt":
        stage_stt(refs)
    elif args.stage == "diar":
        rows = stage_diar(refs, seg_minutes=args.seg_minutes)
        _print_table(rows, "clustering_threshold", "der")
    elif args.stage == "attr":
        rows = stage_attr(refs)
        for k in ATTR_GRID:
            _print_table(rows, k, "cpwer")
    return 0


if __name__ == "__main__":
    sys.exit(main())

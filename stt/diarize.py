"""Speaker diarization via pyannote community-1 (CPU), with identity-first refinement.

pyannote's MPS path has a wontfix bug that can silently corrupt segment
timestamps, so we pin the pipeline to CPU — fine for overnight batch on 48 GB.

pyannote.audio 4.x returns a DiarizeOutput with speaker_diarization (may overlap),
exclusive_speaker_diarization (one speaker at a time — used for word assignment),
and speaker_embeddings (centroids). Per the pipeline source, cluster i maps in
order to SPEAKER_0i, so sorted labels align row-for-row with the centroid array —
we assert that invariant instead of assuming it survives upgrades.

KNOWN LIMITATION (accepted, no clean fix available): the assertion below only
checks that the COUNT of centroids matches the count of labels — it cannot
detect a future pyannote release that keeps the same speaker count but changes
the row ORDER of speaker_embeddings relative to sorted(labels). That would
attach every enrolled voice's name to the wrong cluster, silently — nothing
would crash to reveal it. There's no independent ground truth available here
to verify order against, so this is a documented risk to watch for on any
pyannote upgrade (re-run the tuning/qa benchmark and confirm identified names
are still correct), not something the code below actually guarantees.

Overlap regions (>=2 simultaneous speakers, from the non-exclusive annotation) are
returned so words there can be flagged, and are EXCLUDED from per-turn embeddings —
overlapped audio contaminates the very identity evidence refinement leans on.
"""
import numpy as np

from . import config, identify, refine

_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        import torch
        from pyannote.audio import Pipeline

        token = config.resolve_hf_token()
        if not token:
            raise RuntimeError(
                "No HuggingFace token. Set HF_TOKEN (or run `huggingface-cli login`) "
                "and accept the gated terms at "
                "https://huggingface.co/pyannote/speaker-diarization-community-1"
            )
        pipe = Pipeline.from_pretrained(config.DIARIZATION_MODEL, token=token)
        if pipe is None:
            raise RuntimeError(
                "Pipeline.from_pretrained returned None — the gated-model terms are "
                "likely not accepted for this token (or the model id is wrong)."
            )
        pipe.to(torch.device("cpu"))
        _pipeline = pipe
    return _pipeline


def _overlap_spans(ann):
    """[(start,end)] where >=2 speakers are active, from the non-exclusive annotation."""
    try:
        tl = ann.get_overlap()
        return [(round(float(s.start), 3), round(float(s.end), 3)) for s in tl]
    except Exception:
        return []


def _clean_subspans(start, end, overlaps):
    """Portions of [start,end] not covered by any overlap span."""
    spans, cur = [], start
    for os_, oe in sorted(overlaps):
        if oe <= cur or os_ >= end:
            continue
        if os_ > cur:
            spans.append((cur, min(os_, end)))
        cur = max(cur, oe)
    if cur < end:
        spans.append((cur, end))
    return spans


class _ProgressHook:
    """Adapt pyannote's step hook (step_name, artifact, total, completed) into a
    single 0..1 fraction of the diarization stage. Segmentation and embedding are
    the long steps; everything after is fast."""
    _ranges = {"segmentation": (0.0, 0.50), "embeddings": (0.50, 0.85),
               "speaker_counting": (0.85, 0.87), "discrete_diarization": (0.87, 0.90)}

    def __init__(self, cb):
        self.cb = cb
        self.last = 0.0

    def __call__(self, step_name, step_artifact, file=None, total=None, completed=None):
        lo, hi = self._ranges.get(step_name, (self.last, min(0.90, self.last + 0.02)))
        f = lo + (completed / total) * (hi - lo) if (total and completed is not None) else hi
        f = min(0.90, max(self.last, f))
        if f - self.last >= 0.01:
            self.last = f
            try:
                self.cb(f)
            except Exception:
                pass


def _embed_turns(pipe, wav_path, turns, overlaps, progress=None):
    """Embed each turn on its longest overlap-free subspan (>=0.5s); fall back to the
    full span when overlap swallows the turn. Returns (embeddings, failures).
    Reports the 0.90-1.00 tail of the diarization stage."""
    import soundfile as sf
    import torch

    data, sr = sf.read(str(wav_path))
    wav = torch.tensor(np.asarray(data), dtype=torch.float32)
    emb = pipe._embedding
    min_samples = int(0.1 * sr)
    out, failures = [], 0
    n = max(1, len(turns))
    for i, t in enumerate(turns):
        if progress and i % 25 == 0:
            try:
                progress(0.90 + 0.10 * i / n)
            except Exception:
                pass
        clean = _clean_subspans(t["start"], t["end"], overlaps)
        best = max(clean, key=lambda s: s[1] - s[0], default=None)
        s, e = (best if best and (best[1] - best[0]) >= 0.5 else (t["start"], t["end"]))
        seg = wav[int(s * sr):int(e * sr)]
        if seg.shape[-1] < min_samples:
            out.append(None)
            failures += 1
            continue
        try:
            out.append(np.asarray(emb(seg.reshape(1, 1, -1))).reshape(-1))
        except Exception:
            out.append(None)
            failures += 1
    return out, failures


def embed_spans(wav_path, spans, min_dur=0.5):
    """Embed each (start, end) span of `wav_path` with the SAME pyannote
    embedder used for diarization, so the vectors are directly comparable to
    enrolled voiceprints. Returns a list aligned with `spans` (None where a span
    is too short or embedding failed). Used for the mic-channel safety net; no
    overlap cleaning is needed because mic spans are already me-dominant."""
    import soundfile as sf
    import torch

    pipe = _get_pipeline()
    data, sr = sf.read(str(wav_path))
    wav = torch.tensor(np.asarray(data), dtype=torch.float32)
    emb = pipe._embedding
    min_samples = int(max(0.1, min_dur) * sr)
    out = []
    for s, e in spans:
        seg = wav[int(s * sr):int(e * sr)]
        if seg.shape[-1] < min_samples:
            out.append(None)
            continue
        try:
            out.append(np.asarray(emb(seg.reshape(1, 1, -1))).reshape(-1))
        except Exception:
            out.append(None)
    return out


def diarize(wav_path, voiceprints=None, do_refine=None, strict=None, words=None,
            allowed_names=None, num_speakers=None, min_speakers=None,
            max_speakers=None, context="", progress=None) -> dict:
    if do_refine is None:
        do_refine = config.REFINE
    pipe = _get_pipeline()
    hook = _ProgressHook(progress) if progress else None
    try:
        out = pipe(str(wav_path), num_speakers=num_speakers,
                   min_speakers=min_speakers, max_speakers=max_speakers, hook=hook)
    except TypeError:  # pyannote version without hook support
        out = pipe(str(wav_path), num_speakers=num_speakers,
                   min_speakers=min_speakers, max_speakers=max_speakers)

    ann = getattr(out, "speaker_diarization", out)
    excl = getattr(out, "exclusive_speaker_diarization", None) or ann
    labels = sorted(ann.labels())
    overlaps = _overlap_spans(ann)

    cent_emb = {}
    centroids = getattr(out, "speaker_embeddings", None)
    if centroids is not None:
        centroids = np.asarray(centroids)
        # count-only: catches a pyannote upgrade that changes how MANY
        # centroids come back, but a same-count row REORDER would pass this
        # silently — see the module docstring's "KNOWN LIMITATION" note.
        if len(centroids) != len(labels):
            raise RuntimeError(
                f"centroid/label misalignment: {len(centroids)} centroids vs "
                f"{len(labels)} labels — pyannote behavior changed; do not trust naming."
            )
        for i, label in enumerate(labels):
            v = np.asarray(centroids[i], dtype=float)
            if v.size and np.isfinite(v).all() and np.linalg.norm(v) > 0:
                cent_emb[label] = v

    raw_turns = [{"start": round(float(s.start), 3), "end": round(float(s.end), 3), "cluster": spk}
                 for s, _, spk in excl.itertracks(yield_label=True)]
    raw_turns.sort(key=lambda t: t["start"])

    vps = voiceprints if voiceprints is not None else identify.load_voiceprints()
    if allowed_names is not None:
        vps = {n: s for n, s in vps.items() if n in allowed_names}
    cluster_names = ({k: v["name"] for k, v in
                      identify.name_speakers(cent_emb, allowed_names=allowed_names,
                                             context=context).items()}
                     if vps else {k: None for k in cent_emb})
    cluster_names = refine.resolve_split_clusters(cluster_names, cent_emb, vps)

    turn_embeddings, emb_failures = _embed_turns(pipe, wav_path, raw_turns, overlaps,
                                                 progress=progress)
    if raw_turns and emb_failures / len(raw_turns) > 0.3:
        print(f"   warning: {emb_failures}/{len(raw_turns)} turn embeddings failed "
              "— identity refinement degraded for this file")

    turns, names, stats = build_attribution(
        raw_turns, turn_embeddings, cluster_names, vps if do_refine else {},
        cluster_centroids=cent_emb, words=words, strict=strict)
    stats["overlap_spans"] = len(overlaps)

    return {"turns": turns, "names": names, "labels": sorted(names.keys()),
            "embeddings": cent_emb, "raw_turns": raw_turns,
            "turn_embeddings": turn_embeddings, "cluster_names": cluster_names,
            "overlaps": overlaps, "refine_stats": stats}


def build_attribution(raw_turns, turn_embeddings, cluster_names, voiceprints,
                      cluster_centroids=None, words=None, strict=None):
    """Produce final turns + names, applying identity refinement iff voiceprints given.
    Shared by diarize() and relabel (which re-runs it from cached embeddings)."""
    if voiceprints:
        turns, stats = refine.refine_turns(
            raw_turns, turn_embeddings, cluster_names, voiceprints,
            cluster_centroids=cluster_centroids, words=words, strict=strict)
    else:
        base = [{"start": t["start"], "end": t["end"],
                 "speaker": cluster_names.get(t["cluster"]) or t["cluster"]}
                for t in raw_turns]
        turns = refine.merge_adjacent(base)
        stats = {"reassigned": 0, "smoothed": 0, "flagged": 0,
                 "protected_overridden": 0, "strict": bool(strict),
                 "turns_in": len(raw_turns), "turns_out": len(turns), "spans": []}
    names = refine.names_from_speakers([t["speaker"] for t in turns], cluster_names,
                                       voiceprints or {}, turns=turns)
    return turns, names, stats

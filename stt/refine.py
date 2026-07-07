"""Identity-first attribution refinement for KNOWN (enrolled) speakers.

Unsupervised diarization clusters voices without knowing who they are. With
enrolled voiceprints we can do better, carefully:

  1. Identity reassignment — on reliable-length turns (>= min_reliable_dur), if the
     turn's own voice clearly matches an enrolled person, assign that person.
     Open-set guard: moving a turn away from an ANONYMOUS cluster (possibly an
     unknown visitor) requires a higher absolute score (id_min_openset) than
     correcting a turn inside an already-named cluster.
  2. Evidence-gated smoothing — tiny sandwiched fragments are re-attributed to the
     surrounding speaker ONLY when the fragment's own embedding is unusable or
     actually favors the neighbour. Bare answers ("yes", "no", ...) are never
     smoothed: a one-word reply in a confidential conversation means something, and attributing it
     to the questioner is worse than leaving a fragment.

STRICT mode (sensitive recordings): no smoothing, no open-set reassignment —
fragile turns keep the diarizer's label and are flagged for human review.

Every turn carries provenance: attribution in {"diarized","reassigned","smoothed"}
plus id_score, so transcripts are auditable rather than silently tidied.
"""
import numpy as np

from . import config
from .identify import cosine, score_against


def merge_adjacent(turns):
    """Collapse consecutive turns that share a speaker. Flags/attribution are NOT
    merged up — a 0.2s uncertain fragment must not contaminate a 30s clean turn;
    uncertainty is carried separately as time spans (see refine_turns stats)."""
    merged = []
    for t in turns:
        if merged and merged[-1]["speaker"] == t["speaker"]:
            merged[-1]["end"] = t["end"]
        else:
            merged.append({"start": t["start"], "end": t["end"], "speaker": t["speaker"]})
    return merged


def _usable(e):
    return e is not None and np.isfinite(np.asarray(e)).all() and np.linalg.norm(e) > 0


def _words_in(turn, words):
    mid_in = [w["word"].strip().lower().strip(".,!?")
              for w in words
              if turn["start"] <= (w["start"] + w["end"]) / 2 <= turn["end"]]
    return mid_in


def _ref_vector(speaker, voiceprints, cluster_centroids):
    """Reference sample-set for a speaker key: enrolled samples if named, else
    the diarizer's cluster centroid."""
    if speaker in voiceprints:
        return voiceprints[speaker]
    c = cluster_centroids.get(speaker)
    return c.reshape(1, -1) if c is not None else None


def refine_turns(turns, turn_embeddings, cluster_names, voiceprints,
                 cluster_centroids=None, words=None, strict=None,
                 min_reliable_dur=None, id_min=None, id_margin=None,
                 id_min_openset=None, short_dur=None):
    """turns: [{start,end,cluster}] (exclusive, time-sorted); turn_embeddings aligned
    to turns; cluster_names: {cluster_label: name|None}; voiceprints:
    {name: (n_samples, dim) array}; cluster_centroids: {label: vec}; words: ASR words
    (for the protected-answer guard).

    Returns (refined_turns [{start,end,speaker,attribution,id_score,flags}], stats).
    """
    strict = config.STRICT if strict is None else strict
    min_reliable_dur = config.REFINE_MIN_RELIABLE_DUR if min_reliable_dur is None else min_reliable_dur
    id_min = config.REFINE_ID_MIN if id_min is None else id_min
    id_margin = config.REFINE_ID_MARGIN if id_margin is None else id_margin
    id_min_openset = config.REFINE_ID_MIN_OPENSET if id_min_openset is None else id_min_openset
    short_dur = config.REFINE_SHORT_DUR if short_dur is None else short_dur
    cluster_centroids = cluster_centroids or {}
    words = words or []
    n = len(turns)

    def base_key(c):
        nm = cluster_names.get(c)
        return nm if nm else c

    out = [{"start": t["start"], "end": t["end"], "speaker": base_key(t["cluster"]),
            "attribution": "diarized", "id_score": None, "flags": []}
           for t in turns]
    reassigned = smoothed = flagged = 0

    # 1. identity reassignment on reliable-length turns (open-set aware)
    if voiceprints:
        names = list(voiceprints)
        for i, t in enumerate(turns):
            dur = t["end"] - t["start"]
            e = turn_embeddings[i]
            if dur < min_reliable_dur or not _usable(e):
                continue
            scored = sorted(((score_against(e, voiceprints[nm]), nm) for nm in names),
                            reverse=True)
            bs, best = scored[0]
            ss = scored[1][0] if len(scored) > 1 else -1.0
            out[i]["id_score"] = round(bs, 3)
            if best == out[i]["speaker"]:
                continue
            own_named = out[i]["speaker"] in voiceprints
            floor = id_min if own_named else id_min_openset
            if strict and not own_named:
                if bs >= floor and (bs - ss) >= id_margin:
                    out[i]["flags"].append(f"possible:{best}")
                    flagged += 1
                continue
            if bs >= floor and (bs - ss) >= id_margin:
                out[i]["speaker"] = best
                out[i]["attribution"] = "reassigned"
                reassigned += 1

    # 2. evidence-gated smoothing of tiny sandwiched fragments
    if not strict:
        for _ in range(2):
            for i, t in enumerate(turns):
                if (t["end"] - t["start"]) >= short_dur:
                    continue
                if out[i]["attribution"] == "smoothed":
                    continue
                left = out[i - 1]["speaker"] if i > 0 else None
                right = out[i + 1]["speaker"] if i < n - 1 else None
                target = left if (left is not None and left == right) else \
                         (left if right is None else (right if left is None else None))
                if target is None or out[i]["speaker"] == target:
                    continue
                # never smooth a meaningful one-word answer
                toks = _words_in(t, words)
                if toks and all(tok in config.PROTECTED_WORDS for tok in toks):
                    if "protected_answer" not in out[i]["flags"]:
                        out[i]["flags"].append("protected_answer")
                        flagged += 1
                    continue
                e = turn_embeddings[i]
                if not _usable(e):
                    out[i]["speaker"] = target        # no voice evidence at all:
                    out[i]["attribution"] = "smoothed"  # timing sandwich is the best guess
                    smoothed += 1
                    continue
                own_ref = _ref_vector(out[i]["speaker"], voiceprints, cluster_centroids)
                tgt_ref = _ref_vector(target, voiceprints, cluster_centroids)
                own_sc = score_against(e, own_ref) if own_ref is not None else -1.0
                tgt_sc = score_against(e, tgt_ref) if tgt_ref is not None else -1.0
                if tgt_sc > own_sc + 0.05:            # voice actually favors neighbour
                    out[i]["speaker"] = target
                    out[i]["attribution"] = "smoothed"
                    smoothed += 1
                else:                                  # keep diarizer's call, note it
                    out[i]["flags"].append("short_low_confidence")
                    flagged += 1
    # any short turn that was neither smoothed nor already marked stays visible but
    # flagged: its attribution rests on thin evidence regardless of neighbors
    for i, t in enumerate(turns):
        if (t["end"] - t["start"]) < short_dur and out[i]["attribution"] != "smoothed" \
                and not out[i]["flags"]:
            out[i]["flags"].append("short_low_confidence")
            flagged += 1

    # mid-length turns (short_dur..min_reliable_dur) attributed to a NAMED speaker:
    # verify the voice against that person; a clear mismatch is flagged, not hidden.
    # Correct short-turn matches score ~0.5+; the "But."-style misattributions ~0.1.
    if voiceprints:
        for i, t in enumerate(turns):
            dur = t["end"] - t["start"]
            spk = out[i]["speaker"]
            if (short_dur <= dur < min_reliable_dur and spk in voiceprints
                    and out[i]["attribution"] == "diarized" and not out[i]["flags"]
                    and _usable(turn_embeddings[i])):
                own_sc = score_against(turn_embeddings[i], voiceprints[spk])
                out[i]["id_score"] = round(own_sc, 3)
                if own_sc < 0.40:
                    out[i]["flags"].append("id_mismatch")
                    flagged += 1

    # uncertainty/provenance as TIME SPANS, so downstream can flag exactly the
    # affected words instead of whole merged turns
    spans = []
    for o in out:
        for fl in o["flags"]:
            spans.append({"start": o["start"], "end": o["end"], "flag": fl})
        if o["attribution"] != "diarized":
            spans.append({"start": o["start"], "end": o["end"], "flag": o["attribution"]})

    merged = merge_adjacent(out)
    return merged, {"reassigned": reassigned, "smoothed": smoothed, "flagged": flagged,
                    "strict": strict, "turns_in": n, "turns_out": len(merged),
                    "spans": spans}


def resolve_split_clusters(cluster_names, cent_emb, voiceprints, threshold=0.75):
    """When diarization splits one person into two clusters, both centroids match the
    same voiceprint AND each other. Name both (bypassing greedy uniqueness) so a
    4-person meeting stops reporting 5 speakers."""
    if not voiceprints:
        return cluster_names
    named = {}
    for label, nm in cluster_names.items():
        if nm:
            named.setdefault(nm, []).append(label)
    out = dict(cluster_names)
    for label, nm in cluster_names.items():
        if nm is not None:
            continue
        e = cent_emb.get(label)
        if e is None:
            continue
        for person, labels in named.items():
            if score_against(e, voiceprints[person]) >= config.NAMING_THRESHOLD and any(
                cosine(e, cent_emb[l]) >= threshold for l in labels if l in cent_emb
            ):
                out[label] = person
                break
    return out


def names_from_speakers(speaker_keys, cluster_names, voiceprints, turns=None):
    """Build {key: {"name", "score"}} for output. Scores are REAL evidence: for
    enrolled speakers, the mean per-turn id_score observed (or None if reassignment
    never scored them); never a fabricated 1.0."""
    turns = turns or []
    per_key_scores = {}
    for t in turns:
        if t.get("id_score") is not None:
            per_key_scores.setdefault(t["speaker"], []).append(t["id_score"])
    out = {}
    for key in set(speaker_keys):
        if key in voiceprints:
            scores = per_key_scores.get(key)
            out[key] = {"name": key,
                        "score": round(float(np.mean(scores)), 3) if scores else None}
        else:
            out[key] = {"name": cluster_names.get(key), "score": 0.0}
    return out

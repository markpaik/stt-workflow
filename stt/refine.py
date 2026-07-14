"""Identity-first attribution refinement for KNOWN (enrolled) speakers.

Unsupervised diarization clusters voices without knowing who they are. With
enrolled voiceprints we can do better, carefully:

  1. Identity reassignment — on reliable-length turns (>= min_reliable_dur), if the
     turn's own voice clearly matches an enrolled person, assign that person.
     Open-set guard: moving a turn away from an ANONYMOUS cluster (possibly an
     unknown visitor) requires a higher absolute score (id_min_openset) than
     correcting a turn inside an already-named cluster.
  2. Mid-band open-set rescue — a 0.6-1.5s turn stranded in an UNNAMED cluster
     moves to the enrolled speaker its own embedding ranks first (clear margin),
     over a floor that relaxes only when that speaker also owns an adjacent
     turn. Named-cluster turns in this band are flagged, never moved.
  3. Evidence-gated smoothing — tiny sandwiched fragments are re-attributed to the
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
    reassigned = smoothed = flagged = protected_overridden = 0

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

    # 2. mid-band open-set rescue (short_dur..min_reliable_dur). Turns here
    # used to keep the diarizer's label unconditionally; the 07/2026 eval
    # showed every fixable miss in the band is a turn STRANDED IN AN UNNAMED
    # cluster (the diarizer's leftover blob or split-off junk) whose own
    # embedding ranks the true enrolled speaker first with a clear margin.
    # Rank-1 + margin alone needs the open-set bar; when the candidate also
    # owns an ADJACENT turn, adjacency corroborates and a lower floor
    # suffices. Named-cluster turns are never touched (measured: zero fixable
    # cases; flipping them regressed human corrections — experiments.md run D)
    # and strict mode never runs this (fragile calls stay a human's call).
    if voiceprints and not strict:
        prev = [o["speaker"] for o in out]  # post-step-1 labels: a rescue
        for i, t in enumerate(turns):       # may not anchor on another rescue
            dur = t["end"] - t["start"]
            if not (short_dur <= dur < min_reliable_dur):
                continue
            if out[i]["speaker"] in voiceprints:  # named cluster: id_mismatch
                continue                          # flagging governs, below
            e = turn_embeddings[i]
            if not _usable(e):
                continue
            scored = sorted(((score_against(e, voiceprints[nm]), nm)
                             for nm in voiceprints), reverse=True)
            bs, best = scored[0]
            ss = scored[1][0] if len(scored) > 1 else -1.0
            if (bs - ss) < config.REFINE_MIDBAND_RESCUE_MARGIN:
                continue
            neighbour = best in ((prev[i - 1] if i > 0 else None),
                                 (prev[i + 1] if i < n - 1 else None))
            floor = (config.REFINE_MIDBAND_NEIGHBOR_MIN if neighbour
                     else config.REFINE_MIDBAND_RESCUE_MIN)
            if bs >= floor:
                out[i]["speaker"] = best
                out[i]["attribution"] = "reassigned"
                out[i]["id_score"] = round(bs, 3)
                reassigned += 1

    # 3. evidence-gated smoothing of tiny sandwiched fragments
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
                # a meaningful one-word answer is protected — it may move ONLY
                # on strong, turn-local voice evidence: the context speaker
                # must beat the assigned one by MORE than the override margin
                # on the turn's OWN embedding. Context alone (the sandwich)
                # never re-attributes a protected answer, and an unusable
                # embedding is not evidence (unconditional inheritance was
                # measured on the real library and rejected).
                toks = _words_in(t, words)
                if toks and all(tok in config.PROTECTED_WORDS for tok in toks):
                    e = turn_embeddings[i]
                    own_ref = _ref_vector(out[i]["speaker"], voiceprints, cluster_centroids)
                    tgt_ref = _ref_vector(target, voiceprints, cluster_centroids)
                    if (_usable(e) and own_ref is not None and tgt_ref is not None
                            and (score_against(e, tgt_ref) - score_against(e, own_ref))
                            > config.REFINE_PROTECTED_OVERRIDE_MARGIN):
                        if "protected_answer" in out[i]["flags"]:  # pass-1 leftover
                            out[i]["flags"].remove("protected_answer")
                            flagged -= 1
                        out[i]["speaker"] = target
                        out[i]["attribution"] = "smoothed"
                        smoothed += 1
                        protected_overridden += 1
                    elif "protected_answer" not in out[i]["flags"]:
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
                    # the two-pass loop can revisit this turn unchanged; guard the
                    # append (like protected_answer above) so it isn't doubled
                    if "short_low_confidence" not in out[i]["flags"]:
                        out[i]["flags"].append("short_low_confidence")
                        flagged += 1
    # STRICT only: any short turn that was neither smoothed nor already marked
    # stays visible but flagged — its attribution rests on thin evidence, and in
    # a sensitive recording that is a human's call. In normal mode the
    # evidence-gated branch above (voice actually contradicts the label) already
    # flagged the real problems; flagging every remaining short turn wholesale
    # buried them in confident diarizer calls (measured on the 41-meeting library).
    if strict:
        for i, t in enumerate(turns):
            if (t["end"] - t["start"]) < short_dur and out[i]["attribution"] != "smoothed" \
                    and not out[i]["flags"]:
                out[i]["flags"].append("short_low_confidence")
                flagged += 1

    # mid-length turns (short_dur..min_reliable_dur) attributed to a NAMED speaker:
    # verify the voice against that person; a clear mismatch is flagged, not hidden.
    # A LOW OWN-SCORE ALONE IS NOT A MISMATCH at these lengths: the score is a
    # duration artifact — the median correctly-attributed mid-band turn on the
    # real 41-meeting library scored 0.32, under the old flat 0.40 gate, so the
    # bare threshold flagged the expected case. Normal mode (closed roster: every
    # cluster named) demands a COMPARATIVE signal — some OTHER enrolled voice
    # scores >= REFINE_MISMATCH_OTHER_MIN and beats the owner by
    # >= REFINE_MISMATCH_MARGIN, as the true "But."-style misattributions do
    # (owner ~0.1, real speaker clearly ahead). Strict mode, and any meeting with
    # an UNNAMED cluster (open roster: the low score may simply be the un-enrolled
    # person speaking, whom no voiceprint can out-score), keep the old
    # unconditional flag on own-score < REFINE_MISMATCH_OWN_MAX.
    if voiceprints:
        # open roster = an un-enrolled ATTENDEE may be present. An unnamed
        # cluster carrying almost no talk is junk (a noise floor the diarizer
        # split off), not an attendee — counting it flipped whole meetings to
        # the unconditional flag and buried the real mismatches. Strict mode
        # below keeps unconditional semantics regardless.
        talk = {}
        for t in turns:
            talk[t["cluster"]] = talk.get(t["cluster"], 0.0) + (t["end"] - t["start"])
        open_roster = any(not nm and talk.get(c, 0.0) >= config.UNKNOWN_MIN_TALK_SECS
                          for c, nm in cluster_names.items())
        for i, t in enumerate(turns):
            dur = t["end"] - t["start"]
            spk = out[i]["speaker"]
            if (short_dur <= dur < min_reliable_dur and spk in voiceprints
                    and out[i]["attribution"] == "diarized" and not out[i]["flags"]
                    and _usable(turn_embeddings[i])):
                own_sc = score_against(turn_embeddings[i], voiceprints[spk])
                out[i]["id_score"] = round(own_sc, 3)
                if own_sc >= config.REFINE_MISMATCH_OWN_MAX:
                    continue
                if not (strict or open_roster):
                    other_sc = max((score_against(turn_embeddings[i], vp)
                                    for nm, vp in voiceprints.items() if nm != spk),
                                   default=-1.0)
                    if not (other_sc >= config.REFINE_MISMATCH_OTHER_MIN
                            and (other_sc - own_sc) >= config.REFINE_MISMATCH_MARGIN):
                        continue
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
                    "protected_overridden": protected_overridden,
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

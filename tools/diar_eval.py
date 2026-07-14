#!/usr/bin/env python3
"""Speaker diarization/identification evaluation harness: one reproducible
number (plus its breakdown) for "how accurate is our speaker pipeline", so
tuning experiments get honest before/after scores.

Three ground-truth sources:
  reviews  Human review decisions that CHANGED a speaker (live .reviews.json
           and archived .reviews.superseded.json sidecars) = labeled turns.
           Scored against a CACHE-ONLY re-attribution (.diar.npz + current
           voiceprints, the relabel compute path) — never against the stored
           transcript, which already contains the human's fix. Read-only on
           the real library: nothing under the meetings store or the
           voiceprint registry is ever written.
  stereo   Recorder stereo captures (L = mic/"me", R = system/"them"): a
           conservative channel-energy labeler with an abstain band turns
           per-turn dominance into binary truth (is-Mark vs not-Mark).
  synth    tools/synth_corpus.py conversations with exact scripted truth,
           run through the REAL full pipeline inside an STT_HOME sandbox.

Output: a scoreboard (json + markdown) under qa/eval/runs/, with a run header
(git sha, --date passed in, config-constant snapshot) so runs are comparable.
`--baseline NAME` stores the run as the named baseline; later runs print
deltas against the current baseline.

Typical use:
    .venv/bin/python tools/diar_eval.py --source all --date 2026-07-13 \
        --baseline pre-tuning
    # ... change a constant, then:
    .venv/bin/python tools/diar_eval.py --source all --date 2026-07-14

Heavy stages run niced (never caffeinated) and only after the control panel's
/api/state confirms no batch is running.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import numpy as np
import soundfile as sf

import synth_corpus
from stt import (audio, channels, config, diarcache, diarize, identify, merge,
                 refine, sanitize)

# turn-duration bands (seconds): where the pipeline is strong vs weak
BANDS = [(0.0, 0.3), (0.3, 0.6), (0.6, 1.0), (1.0, 1.5), (1.5, 2.5),
         (2.5, float("inf"))]
BAND_LABELS = ["<0.3s", "0.3-0.6s", "0.6-1.0s", "1.0-1.5s", "1.5-2.5s", ">=2.5s"]

# channel-energy labeler: CONSERVATIVE dominance margin (wider than the
# pipeline's own 8 dB gate) with an abstain band between — only confident
# labels count as truth.
ENERGY_MARGIN_DB = 10.0
ENERGY_FLOOR_DBFS = -45.0

# config constants that shape attribution — snapshotted into every run header
SNAPSHOT_KEYS = [
    "NAMING_THRESHOLD", "NAMING_MARGIN", "REFINE", "REFINE_MIN_RELIABLE_DUR",
    "REFINE_ID_MIN", "REFINE_ID_MARGIN", "REFINE_ID_MIN_OPENSET",
    "REFINE_SHORT_DUR", "REFINE_PROTECTED_OVERRIDE_MARGIN",
    "REFINE_MIDBAND_RESCUE_MIN", "REFINE_MIDBAND_RESCUE_MARGIN",
    "REFINE_MIDBAND_NEIGHBOR_MIN",
    "REFINE_MISMATCH_OWN_MAX", "REFINE_MISMATCH_OTHER_MIN",
    "REFINE_MISMATCH_MARGIN", "OVERLAP_FLAG_MIN_SEC", "UNKNOWN_MIN_TALK_SECS",
    "UNKNOWN_MIN_RELIABLE_TURNS", "CHANNEL_FLOOR_DBFS", "CHANNEL_DOMINANCE_DB",
    "CHANNEL_ENTER_MS", "CHANNEL_EXIT_MS", "CHANNEL_BRIDGE_MS",
    "CHANNEL_MIN_SPAN_MS", "CHANNEL_FORCE_MIN", "CHANNEL_PASS_FRACTION",
    "STRICT", "ASR_BACKEND", "DIARIZATION_MODEL",
]


def band_label(dur: float) -> str:
    for (lo, hi), label in zip(BANDS, BAND_LABELS):
        if lo <= dur < hi:
            return label
    return BAND_LABELS[-1]


def _norm_name(name) -> str:
    return (name or "").strip().casefold()


# ===================================================== source 1: reviews ====

# ids that are diarization/registry artifacts, not people — in a SUPERSEDED
# sidecar they reference clusters from a diarization that no longer exists
_CLUSTERISH = re.compile(r"^(SPEAKER_\d+|MANUAL_\d+|U\d{3,})$")


def _spec_to_name(spec, by_id):
    """Resolve a decision's speaker spec to a person's name, or None when it
    can't honestly be resolved. Specs come in three shapes: "name:<who>" (the
    panel's new-person form), a meeting speaker id (which IS the person's name
    once a cluster is named — see merge/build_attribution — else SPEAKER_xx),
    or a bare name recorded by earlier panel versions."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.startswith("name:"):
        return spec[5:].strip() or None
    if by_id is not None and spec in by_id:
        return by_id[spec].get("name")  # None for an unnamed cluster
    if _CLUSTERISH.match(spec):
        return None  # cluster id with no current-roster referent
    return spec


def extract_review_truth(dest_dir=None):
    """Every review decision that changed a speaker -> a labeled turn
    {meeting, start, end, correct_speaker, source, action}. Live sidecars
    resolve ids against the meeting's current roster; superseded sidecars
    (whose cluster ids died with the old diarization) contribute only
    name-shaped specs. Returns (records, stats)."""
    records = []
    stats = {"decisions": 0, "labeled": 0, "unresolved_speaker": 0,
             "accepts": 0, "text_only_edits": 0, "deletes": 0, "inserts": 0,
             "meetings_with_decisions": 0}
    for base in config.meeting_bases(dest_dir):
        jpath = config.meeting_file(base, ".json", dest_dir)
        try:
            roster = json.loads(jpath.read_text()).get("speakers", [])
        except (OSError, ValueError):
            roster = []
        by_id = {s["id"]: s for s in roster}
        had_any = False
        for suffix, tag in ((".reviews.json", "reviews"),
                            (".reviews.superseded.json", "reviews_superseded")):
            p = config.meeting_file(base, suffix, dest_dir)
            if not p.exists():
                continue
            try:
                decisions = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            # a superseded sidecar predates the CURRENT diarization: its
            # cluster ids are meaningless now, but names still resolve
            ids = by_id if tag == "reviews" else None
            for d in decisions:
                had_any = True
                stats["decisions"] += 1
                action = d.get("action")
                spans = []
                if action == "edit":
                    if d.get("speaker_id"):
                        spans = [(d["start"], d.get("end", d["start"]),
                                  d["speaker_id"])]
                    else:
                        stats["text_only_edits"] += 1
                elif action == "split":
                    # half A keeps/changes to speaker_id (None = unchanged:
                    # not a labeled claim); half B is always a claim
                    if d.get("speaker_id"):
                        spans.append((d["start"], d.get("cut", d["start"]),
                                      d["speaker_id"]))
                    if d.get("speaker_b"):
                        spans.append((d.get("cut", d["start"]),
                                      d.get("end", d["start"]), d["speaker_b"]))
                elif action == "accept":
                    stats["accepts"] += 1
                elif action == "delete":
                    stats["deletes"] += 1
                elif action == "insert":
                    stats["inserts"] += 1
                for s, e, spec in spans:
                    name = _spec_to_name(spec, ids)
                    if not name or float(e) <= float(s):
                        stats["unresolved_speaker"] += 1
                        continue
                    stats["labeled"] += 1
                    records.append({"meeting": base, "start": float(s),
                                    "end": float(e), "correct_speaker": name,
                                    "source": tag, "action": action})
        if had_any:
            stats["meetings_with_decisions"] += 1
    return records, stats


# ============================================ cache-only re-attribution =====

def predict_segments(base, dest_dir=None):
    """The pipeline's OWN current attribution for a processed meeting, rebuilt
    from its .diar.npz cache + the live voiceprint registry — the exact
    relabel compute path, minus every write (no unknown-registry assignment,
    no decision replay, no output files). This is what human corrections are
    scored against: the stored transcript already contains the fixes."""
    jpath = config.meeting_file(base, ".json", dest_dir)
    dpath = config.meeting_file(base, ".diar.npz", dest_dir)
    if not jpath.exists() or not dpath.exists():
        return None
    data = json.loads(jpath.read_text())
    words = [{"start": w["start"], "end": w["end"], "word": w["word"]}
             for w in data.get("words", [])]
    words, loop_spans = sanitize.collapse_repeats(words)
    strict = bool(data.get("strict"))

    vps = identify.load_voiceprints()
    raw_turns, turn_embeddings, cent_emb, overlaps = diarcache.load(dpath)
    cluster_names = ({k: v["name"] for k, v in
                      identify.name_speakers(cent_emb,
                                             context=f"diar_eval:{base}").items()}
                     if vps else {k: None for k in cent_emb})
    cluster_names = refine.resolve_split_clusters(cluster_names, cent_emb, vps)
    turns, names, stats = diarize.build_attribution(
        raw_turns, turn_embeddings, cluster_names, vps if config.REFINE else {},
        cluster_centroids=cent_emb, words=words, strict=strict)

    # channel-aware recordings: replay the cached mic overlay, re-gated
    # against the current voiceprint (mirrors relabel_one)
    ch = diarcache.load_channel(dpath)
    if ch["mic_speaker"] and ch["spans"]:
        mark_vp = vps.get(ch["mic_speaker"])
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

    segments, _ = merge.assign_and_group(
        words, turns, names, overlaps=overlaps,
        spans=stats.get("spans", []) + loop_spans,
        overlap_min_sec=0.0 if strict else config.OVERLAP_FLAG_MIN_SEC)
    return segments


def span_speaker(segments, start, end):
    """The speaker NAME (None = unnamed cluster) the segments assign to
    [start, end], by maximum temporal overlap."""
    best, best_ov = None, 0.0
    for seg in segments:
        ov = min(end, seg["end"]) - max(start, seg["start"])
        if ov > best_ov:
            best_ov, best = ov, seg.get("name")
    return best


# ---------------------------------------------------------- aggregation -----

def _empty_bands():
    return {label: {"n": 0, "correct": 0} for label in BAND_LABELS}


def _acc(d):
    return round(d["correct"] / d["n"], 4) if d["n"] else None


def aggregate_rows(rows):
    """rows: [{dur, correct(bool), truth, predicted, meeting, ...}] ->
    {n, accuracy, by_band, confusions, per_meeting}."""
    bands = _empty_bands()
    per_meeting = {}
    confusions = {}
    n_correct = 0
    for r in rows:
        b = bands[band_label(r["dur"])]
        b["n"] += 1
        m = per_meeting.setdefault(r["meeting"], {"n": 0, "correct": 0})
        m["n"] += 1
        if r["correct"]:
            b["correct"] += 1
            m["correct"] += 1
            n_correct += 1
        else:
            key = (r["truth"], r["predicted"] or "(unnamed)")
            confusions[key] = confusions.get(key, 0) + 1
    for b in bands.values():
        b["accuracy"] = _acc(b)
    for m in per_meeting.values():
        m["accuracy"] = _acc(m)
    conf = sorted(([t, p, n] for (t, p), n in confusions.items()),
                  key=lambda x: -x[2])
    return {"n": len(rows), "correct": n_correct,
            "accuracy": _acc({"n": len(rows), "correct": n_correct}),
            "by_band": bands, "confusions": conf, "per_meeting": per_meeting}


def run_reviews(dest_dir=None):
    """Score every human speaker correction against the cache-only rebuild."""
    records, stats = extract_review_truth(dest_dir)
    rows, skipped_no_cache = [], 0
    segments_of = {}
    for r in records:
        base = r["meeting"]
        if base not in segments_of:
            segments_of[base] = predict_segments(base, dest_dir)
        segs = segments_of[base]
        if segs is None:
            skipped_no_cache += 1
            continue
        predicted = span_speaker(segs, r["start"], r["end"])
        rows.append({"meeting": base, "start": r["start"], "end": r["end"],
                     "dur": r["end"] - r["start"], "truth": r["correct_speaker"],
                     "predicted": predicted, "source": r["source"],
                     "correct": _norm_name(predicted) == _norm_name(
                         r["correct_speaker"])})
    out = aggregate_rows(rows)
    by_source = {}
    for tag in ("reviews", "reviews_superseded"):
        sub = [r for r in rows if r["source"] == tag]
        by_source[tag] = {"n": len(sub),
                          "accuracy": _acc({"n": len(sub),
                                            "correct": sum(r["correct"] for r in sub)})}
    out.update({"extraction": stats, "by_sidecar": by_source,
                "skipped_no_cache": skipped_no_cache, "rows": rows})
    return out


# ====================================================== source 2: stereo ====

def _rms_db(x) -> float:
    r = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    return float(20 * np.log10(r)) if r > 1e-9 else -120.0


def label_stereo_spans(mic_path, sys_path, spans, margin_db=ENERGY_MARGIN_DB,
                       floor_dbfs=ENERGY_FLOOR_DBFS):
    """Binary channel-energy truth for each (start, end) span of a stereo
    me/them capture: "mic" when the mic channel dominates the system channel
    by >= margin_db, "sys" when the system dominates by the same margin, and
    None (ABSTAIN) in between or when neither channel carries speech. The
    margin is deliberately wider than the pipeline's own dominance gate: only
    labels the energy split is sure of count as ground truth."""
    mic, sr = sf.read(str(mic_path), dtype="float64")
    sysd, sr2 = sf.read(str(sys_path), dtype="float64")
    if getattr(mic, "ndim", 1) > 1:
        mic = mic.mean(axis=1)
    if getattr(sysd, "ndim", 1) > 1:
        sysd = sysd.mean(axis=1)
    if sr != sr2:
        raise ValueError(f"sample-rate mismatch: {sr} vs {sr2}")
    labels = []
    for s, e in spans:
        a = mic[int(s * sr):int(e * sr)]
        b = sysd[int(s * sr):int(e * sr)]
        mdb, sdb = _rms_db(a), _rms_db(b)
        if max(mdb, sdb) < floor_dbfs:   # no speech on either channel
            labels.append(None)
            continue
        delta = mdb - sdb
        labels.append("mic" if delta >= margin_db
                      else "sys" if delta <= -margin_db else None)
    return labels


def find_stereo_meetings(dest_dir=None):
    """Meetings whose source was the recorder: a declared me/them channel
    layout, or stored audio that is actually 2-channel."""
    out = []
    for base in config.meeting_bases(dest_dir):
        jpath = config.meeting_file(base, ".json", dest_dir)
        try:
            data = json.loads(jpath.read_text())
        except (OSError, ValueError):
            continue
        src = config.meeting_audio(base, dest_dir)
        if src is None:
            continue
        declared = data.get("channel_layout") == "mic_left_system_right"
        stereo = False
        if not declared:
            try:
                stereo = audio.probe_channels(src) >= 2
            except Exception:
                stereo = False
        if declared or stereo:
            out.append({"base": base, "audio": src,
                        "mic_speaker": data.get("mic_speaker"),
                        "channel_mode": data.get("channel_mode"),
                        "declared": declared})
    return out


def find_loose_captures():
    """Raw stereo captures still sitting in the recordings folder (recorded
    but not yet processed) — reported, not scored (no transcript to score)."""
    rec = config.recordings_dir()
    out = []
    try:
        entries = sorted(rec.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return out
    for p in entries:
        if p.suffix.lower() in config.AUDIO_EXTS and p.is_file():
            try:
                if audio.probe_channels(p) >= 2:
                    out.append(p.name)
            except Exception:
                continue
    return out


def run_stereo(dest_dir=None):
    """Binary is-Mark/not-Mark accuracy on recorder stereo captures: the
    channel-energy labeler provides truth (confident labels only), the
    cache-only rebuild provides the pipeline's attribution."""
    import tempfile
    meetings = find_stereo_meetings(dest_dir)
    rows, per_meeting_meta, abstained = [], {}, 0
    default_mic = config.mic_speaker()
    for m in meetings:
        mic_name = m["mic_speaker"] or default_mic
        if not mic_name:
            per_meeting_meta[m["base"]] = {"skipped": "no mic speaker configured"}
            continue
        segs = predict_segments(m["base"], dest_dir)
        if segs is None:
            per_meeting_meta[m["base"]] = {"skipped": "no .diar.npz cache"}
            continue
        with tempfile.TemporaryDirectory() as td:
            mic_wav = Path(td) / "mic.wav"
            sys_wav = Path(td) / "sys.wav"
            audio.to_wav16k_channel(m["audio"], mic_wav, 0)
            audio.to_wav16k_channel(m["audio"], sys_wav, 1)
            spans = [(s["start"], s["end"]) for s in segs]
            labels = label_stereo_spans(mic_wav, sys_wav, spans)
        n_scored = 0
        for seg, lab in zip(segs, labels):
            if lab is None:
                abstained += 1
                continue
            truth_is_mic = (lab == "mic")
            pred_is_mic = _norm_name(seg.get("name")) == _norm_name(mic_name)
            rows.append({"meeting": m["base"], "start": seg["start"],
                         "end": seg["end"], "dur": seg["end"] - seg["start"],
                         "truth": "is-mic" if truth_is_mic else "not-mic",
                         "predicted": ("is-mic" if pred_is_mic else
                                       (seg.get("name") or "(unnamed)")),
                         "correct": truth_is_mic == pred_is_mic})
            n_scored += 1
        per_meeting_meta[m["base"]] = {"channel_mode": m["channel_mode"],
                                       "mic_speaker": mic_name,
                                       "scored_turns": n_scored}
    out = aggregate_rows(rows)
    out.update({"n_stereo_meetings": len(meetings),
                "meetings": per_meeting_meta, "abstained_turns": abstained,
                "loose_captures_unscored": find_loose_captures(),
                "labeler": {"margin_db": ENERGY_MARGIN_DB,
                            "floor_dbfs": ENERGY_FLOOR_DBFS},
                "rows": rows})
    return out


# ======================================================= source 3: synth ====

def _hf_token_from_env_file():
    p = REPO / "stt.env"
    if p.exists():
        for ln in p.read_text().splitlines():
            if ln.strip().startswith("HF_TOKEN="):
                return ln.split("=", 1)[1].strip()
    return None


def panel_busy(port: int) -> bool:
    """Is a batch running? Ask the live panel first; fall back to the status
    file when no panel is up."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            js = json.load(r)
        return bool(js.get("running") or js.get("active"))
    except Exception:
        try:
            from stt import status
            return bool(status.read().get("running"))
        except Exception:
            return False


def wait_for_idle(port: int, poll=30, timeout=7200):
    waited = 0
    while panel_busy(port):
        if waited >= timeout:
            sys.exit("a batch has been running for the whole wait window — "
                     "try again later")
        print(f"  panel reports a live batch — waiting {poll}s ...", flush=True)
        time.sleep(poll)
        waited += poll


def _spawn_stage(stage, corpus, home, force=False):
    """Run a synth_corpus stage in a CHILD process: STT_HOME points at the
    sandbox, every real-path override is stripped, and the whole thing is
    niced (and never caffeinated) so it can't disturb interactive work."""
    env = dict(os.environ)
    for k in ("STT_MEETINGS_DIR", "STT_ICLOUD_DIR", "STT_RECORDINGS_DIR",
              "STT_VOICEPRINTS_DIR", "STT_PROJECT_DIR", "STT_MIC_SPEAKER",
              "STT_STRICT", "STT_VERIFY"):
        env.pop(k, None)
    env["STT_HOME"] = str(home)
    env["STT_PUNCTUATE"] = "0"
    env["PYTHONPATH"] = str(REPO)
    if not env.get("HF_TOKEN"):
        tok = _hf_token_from_env_file()
        if tok:
            env["HF_TOKEN"] = tok
    cmd = ["nice", "-n", "10", sys.executable,
           str(REPO / "tools" / "synth_corpus.py"),
           "--stage", stage, "--corpus", str(corpus)]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, env=env, check=True, cwd=str(REPO))


def frame_metrics(truth_turns, segments, duration, hop=0.01):
    """DER-style frame accounting with KNOWN identities (no permutation
    search): at each 10 ms frame, the predicted name must be one of the truth
    speakers active there. Returns miss / false-alarm / confusion rates over
    truth speech time, plus their sum (der_like)."""
    n = max(1, int(duration / hop))
    miss = fa = err = speech = 0
    # precompute sorted spans for scanning
    t_idx = [(t["start"], t["end"], _norm_name(t["speaker"])) for t in truth_turns]
    s_idx = [(s["start"], s["end"], _norm_name(s.get("name"))) for s in segments]
    for i in range(n):
        t = (i + 0.5) * hop
        active = {nm for a, b, nm in t_idx if a <= t < b}
        pred = next((nm for a, b, nm in s_idx if a <= t < b), None)
        if active:
            speech += 1
            if pred is None:
                miss += 1
            elif pred not in active:
                err += 1
        elif pred is not None:
            fa += 1
    if not speech:
        return {"miss": None, "false_alarm": None, "confusion": None,
                "der_like": None}
    return {"miss": round(miss / speech, 4),
            "false_alarm": round(fa / speech, 4),
            "confusion": round(err / speech, 4),
            "der_like": round((miss + fa + err) / speech, 4)}


def boundary_error(truth_turns, segments, cap=2.0):
    """Local boundary agreement: for each truth turn START, the distance to
    the nearest predicted segment boundary (capped). Median + mean, ms."""
    bounds = sorted({round(s["start"], 3) for s in segments}
                    | {round(s["end"], 3) for s in segments})
    if not bounds:
        return {"median_ms": None, "mean_ms": None}
    dists = []
    for t in truth_turns:
        d = min(abs(t["start"] - b) for b in bounds)
        dists.append(min(d, cap))
    dists = np.asarray(dists)
    return {"median_ms": round(float(np.median(dists)) * 1000, 1),
            "mean_ms": round(float(np.mean(dists)) * 1000, 1)}


def score_synth_outputs(manifest, home: Path):
    """Score the sandbox pipeline outputs against the scripted truth."""
    processed_path = home / "eval_processed.json"
    try:
        processed = json.loads(processed_path.read_text())
    except (OSError, ValueError):
        processed = {}
    meetings_dir = home / "meetings"
    rows, frame_per_conv, boundary_per_conv = [], {}, {}
    missing = []
    for conv in manifest["conversations"]:
        base = processed.get(conv["name"])
        jpath = meetings_dir / base / f"{base}.json" if base else None
        if not base or not jpath.exists():
            missing.append(conv["name"])
            continue
        data = json.loads(jpath.read_text())
        segments = data.get("segments", [])
        for t in conv["turns"]:
            predicted = span_speaker(segments, t["start"], t["end"])
            rows.append({"meeting": conv["name"], "start": t["start"],
                         "end": t["end"], "dur": t["end"] - t["start"],
                         "truth": t["speaker"], "predicted": predicted,
                         "correct": _norm_name(predicted) == _norm_name(
                             t["speaker"])})
        frame_per_conv[conv["name"]] = frame_metrics(
            conv["turns"], segments, conv["duration"])
        boundary_per_conv[conv["name"]] = boundary_error(conv["turns"], segments)
    out = aggregate_rows(rows)

    # weighted frame summary across conversations
    ders = [v["der_like"] for v in frame_per_conv.values()
            if v["der_like"] is not None]
    out.update({
        "frame": frame_per_conv,
        "frame_der_like_mean": round(float(np.mean(ders)), 4) if ders else None,
        "boundary": boundary_per_conv,
        "missing_outputs": missing,
        "rows": rows,
    })
    return out


def validate_stereo_labeler(manifest, corpus: Path):
    """Run the channel-energy labeler on the corpus's stereo render (known
    truth): agreement of confident labels + abstain rate. This is the
    'validate on a trustworthy capture' step while zero real recorder
    captures exist."""
    sv = manifest.get("stereo_validation")
    if not sv:
        return None
    stereo = corpus / sv["file"]
    if not stereo.exists():
        return None
    conv = next(c for c in manifest["conversations"]
                if c["name"] == sv["conversation"])
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mic_wav = Path(td) / "mic.wav"
        sys_wav = Path(td) / "sys.wav"
        audio.to_wav16k_channel(stereo, mic_wav, 0)
        audio.to_wav16k_channel(stereo, sys_wav, 1)
        spans = [(t["start"], t["end"]) for t in conv["turns"]]
        labels = label_stereo_spans(mic_wav, sys_wav, spans)
    n = correct = abstain = 0
    for t, lab in zip(conv["turns"], labels):
        truth_is_mic = _norm_name(t["speaker"]) == _norm_name(sv["mic_speaker"])
        if lab is None:
            abstain += 1
            continue
        n += 1
        if (lab == "mic") == truth_is_mic:
            correct += 1
    return {"conversation": sv["conversation"], "mic_speaker": sv["mic_speaker"],
            "bleed": sv["bleed"], "n_turns": len(conv["turns"]),
            "labeled": n, "abstained": abstain,
            "labeler_accuracy": round(correct / n, 4) if n else None}


def run_synth(out_root: Path, panel_port: int, full=False, skip_pipeline=False):
    corpus = out_root / "synth_corpus"
    home = out_root / "synth_home"
    manifest = synth_corpus.ensure_corpus(corpus)

    # a sandbox that processed an OLDER corpus is stale wholesale (different
    # audio, different enrollment clips) — rebuild it from nothing rather than
    # ever scoring new truth against old outputs
    sha_file = home / "corpus_sha.txt"
    if (home.exists() and (home / synth_corpus.HOME_MARKER).exists()
            and sha_file.exists()
            and sha_file.read_text().strip() != manifest["script_sha"]):
        print("  synth: corpus changed — rebuilding the sandbox home", flush=True)
        shutil.rmtree(home)
    synth_corpus.build_home(home)
    sha_file.write_text(manifest["script_sha"] + "\n")

    processed_path = home / "eval_processed.json"
    have_outputs = False
    if processed_path.exists():
        try:
            processed = json.loads(processed_path.read_text())
            have_outputs = all(
                (home / "meetings" / processed[c["name"]] /
                 f"{processed[c['name']]}.json").exists()
                for c in manifest["conversations"] if c["name"] in processed
            ) and len(processed) == len(manifest["conversations"])
        except (OSError, ValueError, KeyError):
            have_outputs = False

    mode = "existing_outputs"
    if not skip_pipeline:
        wait_for_idle(panel_port)
        if not have_outputs or full:
            print("  synth: running the FULL pipeline in the sandbox "
                  "(ASR + diarization; several minutes) ...", flush=True)
            _spawn_stage("process", corpus, home, force=full)
            mode = "full_pipeline"
        else:
            print("  synth: cache-only rescore (relabel) in the sandbox ...",
                  flush=True)
            _spawn_stage("rescore", corpus, home)
            mode = "cache_rescore"
    out = score_synth_outputs(manifest, home)
    out["eval_mode"] = mode
    out["corpus"] = {"script_sha": manifest["script_sha"],
                     "conversations": len(manifest["conversations"]),
                     "total_audio_sec": manifest["total_audio_sec"],
                     "total_speech_sec": manifest["total_speech_sec"],
                     "n_truth_turns": sum(len(c["turns"])
                                          for c in manifest["conversations"])}
    out["stereo_labeler_validation"] = validate_stereo_labeler(manifest, corpus)
    return out


# ============================================================ scoreboard ====

def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(REPO), capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def config_snapshot() -> dict:
    return {k: getattr(config, k) for k in SNAPSHOT_KEYS if hasattr(config, k)}


def build_scoreboard(date: str, sources: dict) -> dict:
    scored = [s for s in sources.values() if s and s.get("n")]
    total = sum(s["n"] for s in scored)
    correct = sum(s["correct"] for s in scored)
    return {
        "run": {"date": date, "git_sha": _git_sha(),
                "tool": "tools/diar_eval.py",
                "sources": sorted(sources.keys()),
                "config": config_snapshot()},
        "headline": {
            "turn_accuracy_overall": round(correct / total, 4) if total else None,
            "n_labeled_turns": total,
            "per_source": {k: (v.get("accuracy") if v else None)
                           for k, v in sources.items()},
        },
        "sources": {k: (_strip_rows(v) if v else None)
                    for k, v in sources.items()},
    }


def _strip_rows(source_result: dict) -> dict:
    """Scoreboards stay diff-able: per-row details go to a sibling file, not
    the scoreboard itself."""
    return {k: v for k, v in source_result.items() if k != "rows"}


def _fmt_pct(x):
    return "—" if x is None else f"{100 * x:.1f}%"


def render_markdown(board: dict, deltas=None) -> str:
    run = board["run"]
    lines = [
        f"# Speaker-attribution eval — {run['date']} (git {run['git_sha']})",
        "",
        f"Headline: **{_fmt_pct(board['headline']['turn_accuracy_overall'])}** "
        f"turn attribution over {board['headline']['n_labeled_turns']} labeled "
        "turns (all sources).",
        "",
    ]
    if deltas:
        lines += ["## vs baseline "
                  f"`{deltas['baseline_name']}` ({deltas['baseline_date']})", ""]
        for k, (old, new) in deltas["metrics"].items():
            arrow = "→"
            diff = ("" if old is None or new is None
                    else f"  ({'+' if new - old >= 0 else ''}{100 * (new - old):.1f} pts)")
            lines.append(f"- {k}: {_fmt_pct(old)} {arrow} {_fmt_pct(new)}{diff}")
        lines.append("")
    for name in ("reviews", "stereo", "synth"):
        src = board["sources"].get(name)
        if src is None:
            continue
        lines += [f"## {name}", ""]
        lines.append(f"- labeled turns scored: **{src.get('n', 0)}**"
                     + (f", accuracy **{_fmt_pct(src.get('accuracy'))}**"
                        if src.get("n") else ""))
        if name == "reviews":
            ex = src.get("extraction", {})
            lines.append(f"- decisions seen: {ex.get('decisions', 0)} "
                         f"(speaker-labeled {ex.get('labeled', 0)}, "
                         f"accepts {ex.get('accepts', 0)}, "
                         f"text-only {ex.get('text_only_edits', 0)}, "
                         f"deletes {ex.get('deletes', 0)}, "
                         f"inserts {ex.get('inserts', 0)}, "
                         f"unresolved {ex.get('unresolved_speaker', 0)})")
            bs = src.get("by_sidecar", {})
            for tag, v in bs.items():
                lines.append(f"  - {tag}: n={v['n']}, acc {_fmt_pct(v['accuracy'])}")
        if name == "stereo":
            lines.append(f"- stereo meetings found: {src.get('n_stereo_meetings', 0)}; "
                         f"abstained turns: {src.get('abstained_turns', 0)}; "
                         f"raw unprocessed captures: "
                         f"{len(src.get('loose_captures_unscored', []))}")
        if name == "synth":
            lines.append(f"- eval mode: {src.get('eval_mode')}; corpus "
                         f"{src.get('corpus', {}).get('total_audio_sec')}s audio, "
                         f"script {src.get('corpus', {}).get('script_sha')}")
            if src.get("frame_der_like_mean") is not None:
                lines.append(f"- frame DER-like (mean over conversations): "
                             f"**{_fmt_pct(src['frame_der_like_mean'])}**")
            sv = src.get("stereo_labeler_validation")
            if sv:
                lines.append(f"- channel labeler validation (synthetic stereo, "
                             f"bleed {sv['bleed']}): {_fmt_pct(sv['labeler_accuracy'])} "
                             f"on {sv['labeled']}/{sv['n_turns']} confident labels "
                             f"({sv['abstained']} abstained)")
        if src.get("n"):
            lines += ["", "| duration band | n | correct | accuracy |",
                      "|---|---:|---:|---:|"]
            for label in BAND_LABELS:
                b = src["by_band"][label]
                lines.append(f"| {label} | {b['n']} | {b['correct']} | "
                             f"{_fmt_pct(b['accuracy'])} |")
            conf = src.get("confusions", [])[:8]
            if conf:
                lines += ["", "Top confusions (truth → predicted):", ""]
                for t, p, n in conf:
                    lines.append(f"- {t} → {p}: {n}")
            pm = src.get("per_meeting", {})
            if pm:
                lines += ["", "Per meeting:", ""]
                for base, v in sorted(pm.items()):
                    lines.append(f"- {base}: {v['correct']}/{v['n']} "
                                 f"({_fmt_pct(v['accuracy'])})")
        lines.append("")
    lines += ["---", "",
              "Config snapshot: see scoreboard.json. Compare an experiment "
              "against the stored baseline by re-running the same command "
              "without `--baseline`.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------- baseline ---

def _baseline_dir(out_root: Path) -> Path:
    return out_root / "baselines"


def store_baseline(out_root: Path, name: str, board: dict):
    bdir = _baseline_dir(out_root)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / f"{name}.json").write_text(json.dumps(board, indent=2))
    (bdir / "CURRENT").write_text(name + "\n")


def load_current_baseline(out_root: Path):
    bdir = _baseline_dir(out_root)
    cur = bdir / "CURRENT"
    if not cur.exists():
        return None, None
    name = cur.read_text().strip()
    p = bdir / f"{name}.json"
    if not p.exists():
        return None, None
    try:
        return name, json.loads(p.read_text())
    except json.JSONDecodeError:
        return None, None


def compute_deltas(baseline: dict, board: dict, name: str):
    """Headline + per-source + per-band accuracy movements vs the baseline."""
    metrics = {}

    def _add(key, old, new):
        if old is not None or new is not None:
            metrics[key] = (old, new)

    _add("overall", baseline["headline"]["turn_accuracy_overall"],
         board["headline"]["turn_accuracy_overall"])
    for src in ("reviews", "stereo", "synth"):
        b_old = (baseline["sources"].get(src) or {})
        b_new = (board["sources"].get(src) or {})
        _add(src, b_old.get("accuracy"), b_new.get("accuracy"))
        for label in BAND_LABELS:
            ob = (b_old.get("by_band") or {}).get(label, {})
            nb = (b_new.get("by_band") or {}).get(label, {})
            if ob.get("n") or nb.get("n"):
                _add(f"{src} {label}", ob.get("accuracy"), nb.get("accuracy"))
    if (baseline["sources"].get("synth") or {}).get("frame_der_like_mean") is not None:
        _add("synth frame DER-like",
             baseline["sources"]["synth"]["frame_der_like_mean"],
             (board["sources"].get("synth") or {}).get("frame_der_like_mean"))
    return {"baseline_name": name,
            "baseline_date": baseline["run"]["date"],
            "metrics": metrics}


# ------------------------------------------------------------------ main ----

def _run_dir(out_root: Path, date: str, sha: str) -> Path:
    base = out_root / "runs" / f"{date}_{sha}"
    d, i = base, 2
    while d.exists():
        d = Path(f"{base}_{i}")
        i += 1
    d.mkdir(parents=True)
    return d


def write_scoreboard(out_root: Path, date: str, sources: dict,
                     baseline_name=None):
    board = build_scoreboard(date, sources)
    deltas = None
    cur_name, cur = load_current_baseline(out_root)
    if cur is not None:
        deltas = compute_deltas(cur, board, cur_name)
        board["vs_baseline"] = {"name": cur_name,
                                "metrics": {k: {"baseline": o, "run": n}
                                            for k, (o, n) in
                                            deltas["metrics"].items()}}
    run_dir = _run_dir(out_root, date, board["run"]["git_sha"])
    (run_dir / "scoreboard.json").write_text(json.dumps(board, indent=2))
    (run_dir / "scoreboard.md").write_text(render_markdown(board, deltas))
    rows = {k: v.get("rows", []) for k, v in sources.items() if v}
    (run_dir / "rows.json").write_text(json.dumps(rows, indent=2))
    if baseline_name:
        store_baseline(out_root, baseline_name, board)
    return run_dir, board, deltas


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="all",
                    choices=["reviews", "stereo", "synth", "all"])
    ap.add_argument("--date", required=True,
                    help="run date (stamped into the header; passed in, not "
                         "read from the clock, so reruns are reproducible)")
    ap.add_argument("--baseline",
                    help="store this run as the named baseline (later runs "
                         "print deltas against it)")
    ap.add_argument("--out", default=str(REPO / "qa" / "eval"),
                    help="output root (default qa/eval — gitignored)")
    ap.add_argument("--full-synth", action="store_true",
                    help="force a full pipeline re-run of the synth corpus "
                         "(default: cache-only rescore when outputs exist)")
    ap.add_argument("--skip-pipeline", action="store_true",
                    help="synth: score existing sandbox outputs as-is")
    ap.add_argument("--panel-port", type=int,
                    default=int(os.environ.get("STT_PANEL_PORT", "8737")))
    args = ap.parse_args(argv)

    if os.environ.get("STT_HOME"):
        sys.exit("refusing: run diar_eval without STT_HOME — the reviews/stereo "
                 "sources read the REAL library read-only; the synth sandbox "
                 "is managed internally.")

    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # keep eval scoring out of the real calibration log (name_speakers logs
    # every match attempt; these are evaluation replays, not live decisions)
    config.CALIBRATION_LOG = out_root / "calibration.eval.jsonl"

    wanted = (["reviews", "stereo", "synth"] if args.source == "all"
              else [args.source])
    sources = {}
    if "reviews" in wanted:
        print("scoring source: reviews (cache-only, read-only) ...", flush=True)
        sources["reviews"] = run_reviews()
    if "stereo" in wanted:
        print("scoring source: stereo recorder captures ...", flush=True)
        sources["stereo"] = run_stereo()
    if "synth" in wanted:
        print("scoring source: synthetic corpus ...", flush=True)
        sources["synth"] = run_synth(out_root, args.panel_port,
                                     full=args.full_synth,
                                     skip_pipeline=args.skip_pipeline)

    run_dir, board, deltas = write_scoreboard(out_root, args.date, sources,
                                              baseline_name=args.baseline)
    print(f"\nscoreboard: {run_dir / 'scoreboard.md'}")
    print(f"headline:   {_fmt_pct(board['headline']['turn_accuracy_overall'])} "
          f"over {board['headline']['n_labeled_turns']} labeled turns")
    for k, v in board["headline"]["per_source"].items():
        print(f"  {k:8s} {_fmt_pct(v)}")
    if deltas:
        print(f"vs baseline {deltas['baseline_name']}:")
        for k, (old, new) in deltas["metrics"].items():
            if old is not None and new is not None and abs(new - old) > 1e-9:
                print(f"  {k}: {_fmt_pct(old)} -> {_fmt_pct(new)}")
    if args.baseline:
        print(f"stored as baseline '{args.baseline}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

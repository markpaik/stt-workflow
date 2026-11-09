"""speakrs diarization: run the native helper and read its outputs back.

native/speakrs builds `stt-speakrs`, a small Rust program around speakrs 0.7.0
(the pyannote community-1 recipe on Core ML). It writes segments plus the raw
chunk embeddings and hard clusters; this module turns those into the same
pieces the pyannote path produces: sorted labels, exclusive turns, overlap
spans, and one centroid per label.

Centroids follow pyannote's own rule: the mean of the chunk-speaker embeddings
assigned to the cluster that have at least MIN_CLEAN_SEC of overlap-free speech
(all assigned rows when none qualifies). The embedder is the same WeSpeaker
ResNet34 converted to Core ML; on the bakeoff corpus the talk-weighted cosine
to pyannote's centroids was 0.995-1.000 and naming matched on every meeting.

Label NN in "SPEAKER_NN" IS hard cluster id NN, and centroids are keyed by
label, so there is no row-order assumption to get wrong.

The helper runs under sandbox-exec with all network access denied: audio
never leaves the machine.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import config

ENGINE = "speakrs-0.7.0-coreml"
FRAME_STEP_SEC = 0.016875   # segmentation-3.0 frame step
MIN_CLEAN_SEC = 2.0         # speakrs CleanFrameDuration default
_SANDBOX = "(version 1)\n(allow default)\n(deny network*)\n"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
TMP_PREFIX = "speakrs-"     # run_batch.clean_scratch sweeps WORK_DIR/speakrs-*

# The model bundle (huggingface.co/avencera/speakrs-models). The fetch tool
# downloads exactly these files; available() requires every one of them, so an
# interrupted download never passes as a usable bundle.
MODEL_PLAIN = ["plda_lda.npy", "plda_tr.npy", "plda_mu.npy", "plda_psi.npy",
               "plda_mean1.npy", "plda_mean2.npy",
               "wespeaker-voxceleb-resnet34.min_num_samples.txt"]
MODEL_STEMS = ["segmentation-3.0", "segmentation-3.0-b32", "segmentation-3.0-b64",
               "wespeaker-fbank", "wespeaker-fbank-b32", "wespeaker-fbank-30s",
               "wespeaker-multimask-tail-b32", "wespeaker-voxceleb-resnet34-tail",
               "wespeaker-voxceleb-resnet34-tail-b3", "wespeaker-voxceleb-resnet34-tail-b32",
               "wespeaker-chunk-emb-p1s-w21", "wespeaker-chunk-emb-p1s-w36",
               "wespeaker-chunk-emb-p1s-w51", "wespeaker-chunk-emb-p1s-w81",
               "wespeaker-chunk-emb-p1s-w111"]
MODEL_PARTS = ("model.mil", "coremldata.bin", "weights/weight.bin",
               "analytics/coremldata.bin")


def model_files() -> list:
    return MODEL_PLAIN + [f"{s}.mlmodelc/{p}" for s in MODEL_STEMS for p in MODEL_PARTS]


class SpeakrsUnavailable(RuntimeError):
    """The helper or its models are missing, or the run failed."""


def available() -> tuple:
    """(ok, reason). ok is False when the binary or any model file is missing."""
    if not Path(config.SPEAKRS_BIN).is_file():
        return False, f"speakrs helper not built ({config.SPEAKRS_BIN}); run init.sh"
    models = Path(config.SPEAKRS_MODELS)
    missing = [f for f in model_files() if not (models / f).is_file()]
    if missing:
        return False, (f"speakrs models missing or incomplete ({models}: "
                       f"{len(missing)} file(s), e.g. {missing[0]}); "
                       "run python3 tools/fetch_speakrs_models.py")
    return True, ""


def engine_id(engine: str = ENGINE) -> str:
    return f"{engine} ahc={config.SPEAKRS_AHC:g}"


def overlap_spans(segments):
    """[(start, end)] where >=2 speakers are active, from the non-exclusive
    segments (one speaker's own segments never overlap each other)."""
    ev = []
    for s, e, _ in segments:
        if e > s:
            ev.append((s, 1))
            ev.append((e, -1))
    ev.sort(key=lambda x: (x[0], x[1]))  # ends before starts at the same instant
    out, active, start = [], 0, None
    for t, d in ev:
        active += d
        if active >= 2 and start is None:
            start = t
        elif active < 2 and start is not None:
            if t > start:
                out.append((round(float(start), 3), round(float(t), 3)))
            start = None
    return out


def centroids(emb, clusters, clean, labels):
    """{label: centroid} from (chunks, speakers, dim) embeddings, (chunks,
    speakers) hard cluster ids and clean-frame counts."""
    min_frames = int(MIN_CLEAN_SEC / FRAME_STEP_SEC)
    fin = np.isfinite(emb).all(axis=2)
    out = {}
    for lab in labels:
        cid = int(lab.rsplit("_", 1)[1])
        rows = (clusters == cid) & fin
        good = rows & (clean >= min_frames)
        pick = good if good.any() else rows
        if pick.any():
            v = emb[pick].astype(float).mean(axis=0)
            if np.isfinite(v).all() and np.linalg.norm(v) > 0:
                out[lab] = v
    return out


def _no_speech(r) -> bool:
    """True when the helper found no speech: both segment lists present
    and empty."""
    return (isinstance(r, dict) and r.get("segments") == []
            and r.get("exclusive_segments") == [])


def _empty(r: dict) -> dict:
    return {"labels": [], "raw_turns": [], "overlaps": [], "centroids": {},
            "engine": engine_id(str(r.get("engine") or ENGINE)),
            "run_sec": r.get("pipeline_run_sec")}


def run(wav_path) -> dict:
    """Diarize one 16 kHz mono WAV. Returns {"labels", "raw_turns", "overlaps",
    "centroids", "engine", "run_sec"}. Raises SpeakrsUnavailable on any failure
    so the caller can fall back to pyannote."""
    ok, why = available()
    if not ok:
        raise SpeakrsUnavailable(why)
    # fail closed: the helper never runs without the network-deny profile
    if not Path(SANDBOX_EXEC).exists():
        raise SpeakrsUnavailable(f"{SANDBOX_EXEC} missing; the helper will not "
                                 "run without network access denied")
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=config.WORK_DIR))
    try:
        cmd = [SANDBOX_EXEC, "-p", _SANDBOX,
               str(config.SPEAKRS_BIN), str(config.SPEAKRS_MODELS), str(wav_path),
               str(out_dir), f"{config.SPEAKRS_AHC:g}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=config.SPEAKRS_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SpeakrsUnavailable(f"speakrs did not finish: {e}") from e
        res_path = out_dir / "result.json"
        if p.returncode != 0 or not res_path.exists():
            tail = (p.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise SpeakrsUnavailable(f"speakrs exit {p.returncode}: {tail[0][:300]}")
        try:
            r = json.loads(res_path.read_text())
            if _no_speech(r):
                # silence (a muted recording, a silent stereo channel): the
                # helper writes no hard clusters, so the arrays cannot be
                # shaped. Nobody spoke, which is a result, not a failure
                return _empty(r)
            chunks, spk, dim = r["emb_shape"]
            emb = np.fromfile(out_dir / "emb.f32", dtype="<f4").reshape(chunks, spk, dim)
            hc = np.fromfile(out_dir / "clusters.i32", dtype="<i4").reshape(chunks, spk)
            clean = np.fromfile(out_dir / "clean.f32", dtype="<f4").reshape(chunks, spk)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            raise SpeakrsUnavailable(f"speakrs output unreadable: {e}") from e
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    # any malformed segment or label must fall back too, not fail the file
    try:
        excl = [(float(s), float(e), str(k)) for s, e, k in r["exclusive_segments"] if e > s]
        segs = [(float(s), float(e), str(k)) for s, e, k in r["segments"] if e > s]
        labels = sorted({k for _, _, k in segs} | {k for _, _, k in excl})
        raw_turns = sorted(({"start": round(s, 3), "end": round(e, 3), "cluster": k}
                            for s, e, k in excl), key=lambda t: t["start"])
        cents = centroids(emb, hc, clean, labels)
    except (KeyError, ValueError, TypeError, IndexError) as e:
        raise SpeakrsUnavailable(f"speakrs output malformed: {e!r}") from e
    return {"labels": labels, "raw_turns": raw_turns,
            "overlaps": overlap_spans(segs), "centroids": cents,
            "engine": engine_id(str(r.get("engine") or ENGINE)),
            "run_sec": r.get("pipeline_run_sec")}

"""The Qwen3-ASR child process: one model server for one recording.

stt/asr_qwen3.py starts this file under its own interpreter
(STT_QWEN_ASR_PY), because mlx-audio needs a newer mlx than parakeet-mlx
pins in the main environment. It runs with `python -I`, so it imports
nothing from this package: only the standard library, numpy, mlx_audio and
huggingface_hub (for the cache lookup).

    python -I qwen3_worker.py --wav W --asr REPO_OR_DIR --aligner REPO_OR_DIR

Protocol: one JSON object per line. The first line out is {"ready": true}
or {"error": "...", "unavailable": true}. Then each request line gets one
reply line:

    {"op": "transcribe", "start": s, "end": e, "repetition_penalty": null}
        -> {"text": "..."}
    {"op": "align", "start": s, "end": e, "text": "..."}
        -> {"items": [[word, start, end], ...]}   (seconds from `start`)
    {"op": "quit"} -> {"bye": true, "peak_gb": ...}

A failed request answers {"error": "..."}. The models load from the local
Hugging Face cache only (local_files_only): a model that is not downloaded
is an error here, never a download in the middle of a batch.

Library output (warnings, progress bars) goes to stderr: fd 1 is moved to
stderr at start, and the replies go to a private copy of the original
stdout. Nothing here prints transcript text.
"""
import argparse
import json
import os
import resource
import sys
import wave

LANGUAGE = "English"
MAX_TOKENS = 4096          # per call; a 300 s chunk of speech is ~1,200 tokens
REPETITION_CONTEXT = 100


def _local_dir(repo: str) -> str:
    """The local folder of a model: the path itself, or its snapshot in the
    Hugging Face cache, checked with the file patterns mlx-audio's loader
    downloads (a README it never fetched is no gap). Raises LookupError when
    it is not downloaded."""
    if os.path.isdir(os.path.expanduser(repo)):
        return os.path.expanduser(repo)
    try:
        from huggingface_hub import snapshot_download
        from mlx_audio.utils import DEFAULT_ALLOW_PATTERNS
        return snapshot_download(repo, local_files_only=True,
                                 allow_patterns=DEFAULT_ALLOW_PATTERNS)
    except ImportError as e:
        raise LookupError(f"mlx-audio does not import in {sys.executable} ({e})")
    except Exception as e:  # LocalEntryNotFoundError, a bad repo id
        raise LookupError(f"model {repo} is not downloaded ({e.__class__.__name__})")


class _Wav:
    """Reads stretches of a 16 kHz mono 16-bit WAV on demand, so a 4-hour
    recording never sits in memory as float32."""

    def __init__(self, path):
        self.w = wave.open(path, "rb")
        if (self.w.getnchannels(), self.w.getsampwidth(), self.w.getframerate()) != (1, 2, 16000):
            raise ValueError("the working WAV must be 16 kHz mono 16-bit")
        self.sr = 16000
        self.n = self.w.getnframes()

    def read(self, start, end):
        import numpy as np
        a = max(0, min(self.n, int(round(start * self.sr))))
        b = max(a, min(self.n, int(round(end * self.sr))))
        self.w.setpos(a)
        raw = self.w.readframes(b - a)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _peak_gb():
    """The larger of MLX's peak allocation and this process's peak RSS."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9  # bytes on macOS
    try:
        import mlx.core as mx
        return round(max(rss, mx.get_peak_memory() / 1e9), 2)
    except Exception:
        return round(rss, 2)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--asr", required=True)
    ap.add_argument("--aligner", required=True)
    args = ap.parse_args(argv)
    # owner-only files, as stt.config sets for every other entry point (this
    # child cannot import it); it inherits the batch's umask anyway
    os.umask(0o077)

    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)   # anything a library prints lands on stderr

    def send(obj):
        out.write(json.dumps(obj) + "\n")
        out.flush()

    try:
        asr_dir, al_dir = _local_dir(args.asr), _local_dir(args.aligner)
    except LookupError as e:
        send({"error": str(e), "unavailable": True})
        return 3
    try:
        import mlx.core as mx
        from mlx_audio.stt import load
    except ImportError as e:
        send({"error": f"mlx-audio does not import in {sys.executable} ({e})",
              "unavailable": True})
        return 3
    try:
        wav = _Wav(args.wav)
        asr = load(asr_dir)
        aligner = load(al_dir)
    except Exception as e:
        send({"error": f"the models did not load ({e.__class__.__name__}: {e})",
              "unavailable": True})
        return 3
    send({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "quit":
                break
            seg = wav.read(float(req["start"]), float(req["end"]))
            if op == "transcribe":
                kw = {}
                if req.get("repetition_penalty"):
                    kw = {"repetition_penalty": float(req["repetition_penalty"]),
                          "repetition_context_size": REPETITION_CONTEXT}
                r = asr.generate(seg, language=LANGUAGE, chunk_duration=1200.0,
                                 max_tokens=MAX_TOKENS, **kw)
                mx.clear_cache()
                send({"text": (r.text or "").strip()})
            elif op == "align":
                r = aligner.generate(seg, text=str(req["text"]), language=LANGUAGE)
                send({"items": [[it.text, float(it.start_time), float(it.end_time)]
                                for it in r]})
            else:
                send({"error": f"unknown op {op!r}"})
        except Exception as e:
            send({"error": f"{e.__class__.__name__}: {e}"})
    send({"bye": True, "peak_gb": _peak_gb()})
    return 0


if __name__ == "__main__":
    sys.exit(main())

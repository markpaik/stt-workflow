"""Central configuration. Override most values via environment variables."""
import os
from pathlib import Path

HOME = Path.home()

# --- Paths ---
ICLOUD_DIR = Path(
    os.environ.get(
        "STT_ICLOUD_DIR",
        HOME / "Library/Mobile Documents/com~apple~CloudDocs/Voice Recordings",
    )
)
MEETINGS_DIR = Path(os.environ.get("STT_MEETINGS_DIR", HOME / "Projects/brain/meetings"))
PROJECT_DIR = Path(__file__).resolve().parent.parent
VOICEPRINTS_DIR = Path(os.environ.get("STT_VOICEPRINTS_DIR",
                                      PROJECT_DIR / "voiceprints"))
MANIFEST_PATH = PROJECT_DIR / "manifest.json"
WORK_DIR = PROJECT_DIR / "work"  # scratch space for normalized wavs
LOG_DIR = PROJECT_DIR / "logs"

# --- Models ---
# Parakeet TDT 0.6B v2 is English-only and the WER leader with an MLX runtime.
PARAKEET_MODEL = os.environ.get("STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v2")
DIARIZATION_MODEL = os.environ.get(
    "STT_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1"
)

# ASR backend: "parakeet" (default, native MLX) or "whisperx" (CPU, robustness hedge)
ASR_BACKEND = os.environ.get("STT_ASR_BACKEND", "parakeet")

# --- Audio (video containers welcome too — ffmpeg extracts the audio track) ---
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".mp4", ".caf", ".m4b", ".flac",
              ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ogg", ".opus", ".wma", ".aiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SAMPLE_RATE = 16000

# --- Speaker naming (cosine similarity of pyannote embeddings) ---
# Start ~0.5-0.65 and tune on real audio. Higher = stricter (fewer false names).
NAMING_THRESHOLD = float(os.environ.get("STT_NAMING_THRESHOLD", "0.60"))

# Cluster naming also requires this margin over the 2nd-best voiceprint (open-set
# guard: a stranger near one enrolled voice must not inherit that person's name).
NAMING_MARGIN = float(os.environ.get("STT_NAMING_MARGIN", "0.15"))

# --- Identity-first refinement (uses enrolled voiceprints to correct attribution) ---
REFINE = os.environ.get("STT_REFINE", "1") == "1"
# Only turns at least this long are trusted for per-turn identity (short ones are noisy).
REFINE_MIN_RELIABLE_DUR = float(os.environ.get("STT_REFINE_MIN_DUR", "1.5"))
# A reliable turn is reassigned to an enrolled voice only above this cosine AND margin.
# Correct matches sit ~0.9; cross-speaker confusions ~0.5, so 0.6/0.15 is safe.
REFINE_ID_MIN = float(os.environ.get("STT_REFINE_ID_MIN", "0.60"))
REFINE_ID_MARGIN = float(os.environ.get("STT_REFINE_ID_MARGIN", "0.15"))
# Reassigning a turn AWAY from an unnamed (anonymous) cluster is an open-set claim
# about a possibly-unknown person and needs stronger evidence.
REFINE_ID_MIN_OPENSET = float(os.environ.get("STT_REFINE_ID_MIN_OPENSET", "0.70"))
# Turns shorter than this are *candidates* for smoothing into neighbours — but only
# with evidence (unusable/contrary embedding) and never for bare yes/no answers.
REFINE_SHORT_DUR = float(os.environ.get("STT_REFINE_SHORT_DUR", "0.3"))
# Words that are never smoothed away into a neighbouring speaker: one-word answers
# carry meaning ("yes" in a confidential conversation), even when the voice evidence is thin.
PROTECTED_WORDS = {"yes", "no", "yeah", "yep", "nope", "right", "correct", "agreed",
                   "true", "false", "sure", "okay", "ok", "uh-huh", "mm-hmm"}

# STRICT mode (confidential conversations): no smoothing, no open-set
# reassignment — fragile attributions are flagged for human review instead of guessed.
STRICT = os.environ.get("STT_STRICT", "0") == "1"

# VERIFY mode (second opinion): a second ASR engine transcribes too, and the
# regions where the engines disagree are flagged for review with both candidates.
# Measured 07/2026: where the engines agree (~95% of words) they match the
# Scribe benchmark ~94% of the time, so only disagreements need human ears.
VERIFY = os.environ.get("STT_VERIFY", "0") == "1"

# --- Punctuation restoration (fixes Parakeet's lowercase run-ons; word-preserving) ---
PUNCTUATE = os.environ.get("STT_PUNCTUATE", "1") == "1"

# Append accepted/rejected identity-match scores here to build a real calibration
# picture (enrolled-vs-stranger score distributions) over time.
CALIBRATION_LOG = PROJECT_DIR / "calibration.jsonl"

# Nightly runs abort early on battery below this % (caffeinate -s is ignored on
# battery; a run at 2% kills the machine). The next trigger retries.
BATTERY_FLOOR = int(os.environ.get("STT_BATTERY_FLOOR", "20"))

# --- Progress/ETA model: measured realtime multiples on this M5 Pro ---
# (speed = audio_seconds / wall_seconds; diarization is CPU-bound and dominates)
EST_RATES = {
    "convert": 80.0,
    "asr": {"parakeet": 30.0, "mlxwhisper:large-v3": 4.2, "mlxwhisper:turbo": 8.6},
    "diarize": 2.25,
    "writing_fixed_sec": 20.0,  # merge + punctuation + output, roughly flat
}

# --- HuggingFace token (needed for the gated pyannote model) ---
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def _env_file() -> dict:
    """Parse stt.env (the persisted settings file) fresh — long-running processes
    (the GUI) call these instead of the import-time constants above."""
    p = PROJECT_DIR / "stt.env"
    kv = {}
    if p.exists():
        for ln in p.read_text().splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                kv[k.strip()] = v.strip()
    return kv


def source_dir() -> Path:
    return Path(_env_file().get("STT_ICLOUD_DIR") or ICLOUD_DIR)


def meetings_dir() -> Path:
    return Path(_env_file().get("STT_MEETINGS_DIR") or MEETINGS_DIR)


# --- Per-meeting folder layout ---
# Every meeting's artifacts live in their own folder:
#   <meetings_dir>/<base>/<base>.json  (+ .txt, .m4a, .diar.npz, .emb.npz,
#   .reviews.json, .verify.json, ...)
# The folder AND the files carry the meeting name, so a rename in the GUI
# renames both and everything stays greppable/openable from Finder.

AUDIO_SUFFIXES = (".m4a", ".mp4", ".wav", ".mp3", ".aiff", ".mov")
# sidecar jsons that are NOT a meeting's main transcript
_SIDECAR_JSON = (".reviews.json", ".verify.json", ".reviews.superseded.json")


def meeting_dir(base: str, dest_dir=None) -> Path:
    return Path(dest_dir or meetings_dir()) / base


def meeting_file(base: str, suffix: str, dest_dir=None) -> Path:
    """One artifact of a meeting, e.g. meeting_file(b, ".json")."""
    return meeting_dir(base, dest_dir) / f"{base}{suffix}"


def meeting_bases(dest_dir=None) -> list:
    """All meetings on disk: folders holding a matching <name>.json."""
    d = Path(dest_dir or meetings_dir())
    try:
        return sorted(p.name for p in d.iterdir()
                      if p.is_dir() and not p.name.startswith(".")
                      and (p / f"{p.name}.json").exists())
    except FileNotFoundError:
        return []


def meeting_audio(base: str, dest_dir=None):
    """The meeting's stored audio file, or None."""
    for e in AUDIO_SUFFIXES:
        p = meeting_file(base, e, dest_dir)
        if p.exists():
            return p
    return None


def migrate_flat_meetings(dest_dir=None) -> int:
    """One-time layout migration: move flat  <dir>/<base>.*  files into
    per-meeting folders  <dir>/<base>/<base>.* . Idempotent and additive —
    nothing is ever deleted or overwritten (os.replace within one directory
    tree; a file already in place is left alone). Also stamps a "date" into
    any meeting json missing one, so month grouping stops re-deriving it
    from the filename on every panel poll."""
    d = Path(dest_dir or meetings_dir())
    if not d.exists():
        return 0
    moved = 0
    bases = [j.stem for j in d.glob("*.json")
             if not j.name.endswith(_SIDECAR_JSON)]
    for base in bases:
        folder = d / base
        folder.mkdir(exist_ok=True)
        for f in list(d.iterdir()):
            if f.is_file() and f.name.startswith(base + "."):
                os.replace(f, folder / f.name)
                moved += 1
    for base in meeting_bases(d):
        _ensure_meeting_date(base, d)
    return moved


def _ensure_meeting_date(base: str, dest_dir=None):
    """Backfill a stored "date" (ISO) into a meeting json that lacks one:
    filename convention first, else the audio/json file mtime."""
    import json as _json
    from datetime import date as _date

    from . import dates
    j = meeting_file(base, ".json", dest_dir)
    try:
        data = _json.loads(j.read_text())
    except (OSError, ValueError):
        return
    if data.get("date"):
        return
    src = meeting_audio(base, dest_dir) or j
    data["date"] = (dates.meeting_date(base)
                    or _date.fromtimestamp(src.stat().st_mtime).isoformat())
    tmp = j.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, j)


def resolve_hf_token():
    """Return the HF token from env, or the cached CLI login, or None."""
    if HF_TOKEN:
        return HF_TOKEN
    try:
        from huggingface_hub import HfFolder

        return HfFolder.get_token()
    except Exception:
        return None


# --- Behavior ---
# Delete the iCloud original only after both .txt and .json are written.
MOVE_AFTER_SUCCESS = os.environ.get("STT_MOVE_AFTER_SUCCESS", "1") == "1"

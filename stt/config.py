"""Central configuration. Override most values via environment variables."""
import os
from pathlib import Path

HOME = Path.home()

# --- Data home override (demo / QA isolation) ------------------------------
# STT_HOME points the ENTIRE data home at one self-contained directory: the
# watched source + recordings folders, the meetings store, voiceprints, the
# manifest, live status + permanent history, queue holds, and the queued-runs
# file — plus the settings file (stt.env) and scratch (work/, logs/), which all
# hang off PROJECT_DIR below. It exists so a build, test, or demo run can drive
# the panel against SYNTHETIC data with zero risk of reading or writing a real
# recording (see tools/demo_seed.py). It redirects STATE only; the served page
# and every code/static asset load relative to their own __file__, so the panel
# still runs straight from this checkout. A specific STT_<X>_DIR env var still
# wins over STT_HOME for that one path. When STT_HOME is unset, every path below
# is exactly what it was before — fully backwards compatible.
STT_HOME = os.environ.get("STT_HOME")
_HOME = Path(STT_HOME).expanduser() if STT_HOME else None


def _home_default(sub: str, hard_default) -> str:
    """Default for a state path: rooted under STT_HOME when that is set, else the
    real on-disk location. A specific STT_<X>_DIR env var overrides either."""
    return str(_HOME / sub if _HOME else hard_default)


# --- Paths ---
ICLOUD_DIR = Path(os.environ.get("STT_ICLOUD_DIR", _home_default(
    "source", HOME / "Library/Mobile Documents/com~apple~CloudDocs/Voice Recordings")))
MEETINGS_DIR = Path(os.environ.get("STT_MEETINGS_DIR",
                                   _home_default("meetings", HOME / "Projects/brain/meetings")))
# where the on-device meeting recorder drops finished captures — a LOCAL folder
# (not iCloud): these calls are sensitive and the files are large, so they never
# round-trip the cloud. Watched by the batch agent as a second WatchPaths entry.
RECORDINGS_DIR = Path(os.environ.get("STT_RECORDINGS_DIR", _home_default(
    "recordings", HOME / "Library/Application Support/com.stt-workflow/recordings")))
# The state root: manifest, status/history, holds, queue, voiceprints, stt.env,
# work/, logs/ all hang off this. Defaults to STT_HOME when set (so all of that
# state travels with the demo home), else this checkout. Code and static assets
# never key off it — they resolve from __file__ — so the panel runs from the
# repo no matter where PROJECT_DIR points.
PROJECT_DIR = Path(os.environ.get("STT_PROJECT_DIR",
                                  _HOME or Path(__file__).resolve().parent.parent))
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
# Widened 0.3 -> 0.6 (07/2026 audit): the 0.3–0.6s band sits squarely in the
# anti-signal zone where per-turn embeddings mislead; extending the
# evidence-gated sandwich there produced 637 evidence-backed label changes on
# the real library. Flips stay evidence-gated — unconditional inheritance was
# measured and REJECTED.
REFINE_SHORT_DUR = float(os.environ.get("STT_REFINE_SHORT_DUR", "0.6"))
# A protected one-word answer ("yeah", "okay", …) sandwiched in another
# speaker's context may re-attribute ONLY when the context speaker beats the
# assigned one by MORE than this margin on the turn's own embedding — strong,
# turn-local evidence, not context inheritance (audit: releases 45 of 538
# protected blocks on the real library).
REFINE_PROTECTED_OVERRIDE_MARGIN = float(
    os.environ.get("STT_REFINE_PROTECTED_OVERRIDE_MARGIN", "0.15"))
# Mid-length turns (short_dur..min_reliable_dur) on a NAMED speaker: the own-voice
# score at these lengths is a DURATION artifact, not evidence — across the real
# 41-meeting library the MEDIAN correctly-attributed mid-band turn scored 0.32,
# under the old flat 0.40 gate, so "own score is low" is the expected case. A
# mismatch flag in normal mode therefore needs a COMPARATIVE signal: some OTHER
# enrolled voice must score >= OTHER_MIN and beat the owner by >= MARGIN.
# Own-score < OWN_MAX remains the precondition; strict mode and meetings with an
# unnamed cluster (open roster) keep the old unconditional flag.
REFINE_MISMATCH_OWN_MAX = float(os.environ.get("STT_REFINE_MISMATCH_OWN_MAX", "0.40"))
REFINE_MISMATCH_OTHER_MIN = float(os.environ.get("STT_REFINE_MISMATCH_OTHER_MIN", "0.50"))
REFINE_MISMATCH_MARGIN = float(os.environ.get("STT_REFINE_MISMATCH_MARGIN", "0.15"))
# A segment earns the "overlap" review flag only when the crosstalk inside it
# SUMS to at least this long — sub-second brushes (backchannels, breaths) keep
# their word-level provenance but don't demand human review of the whole turn.
# Strict-mode callers pass 0.0 (any crosstalk at all flags, as before).
OVERLAP_FLAG_MIN_SEC = float(os.environ.get("STT_OVERLAP_FLAG_MIN_SEC", "1.0"))
# Words that are never smoothed away into a neighbouring speaker: one-word answers
# carry meaning ("yes" in a confidential conversation), even when the voice evidence is thin.
PROTECTED_WORDS = {"yes", "no", "yeah", "yep", "nope", "right", "correct", "agreed",
                   "true", "false", "sure", "okay", "ok", "uh-huh", "mm-hmm"}

# --- Unknown-speaker registry: minting quality floor (07/2026 audit) ---
# A NEW global unknown ("Speaker N") requires real evidence: at least this much
# total talk AND this many reliable turns (>= REFINE_MIN_RELIABLE_DUR each) in
# the cluster. Noise floors and split-off junk stay transcript-local instead of
# becoming nameable registry entries. Matching an EXISTING unknown is never
# restricted — a returning stranger heard briefly still keeps their number.
# The same floor gates enrolling a cluster as a named person.
UNKNOWN_MIN_TALK_SECS = float(os.environ.get("STT_UNKNOWN_MIN_TALK_SECS", "30"))
UNKNOWN_MIN_RELIABLE_TURNS = int(os.environ.get("STT_UNKNOWN_MIN_RELIABLE_TURNS", "10"))

# Adding a NEW sample to a person's EXISTING voiceprint stack: below this
# cosine against their own stack (or matching some other profile better than
# their own) the enrollment is suspect — the API/CLI demand an explicit
# confirm instead of silently poisoning the profile.
ENROLL_STACK_MIN = float(os.environ.get("STT_ENROLL_STACK_MIN", "0.45"))

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

# --- Channel-aware diarization (stereo recordings from the meeting recorder:
# LEFT = mic/"me", RIGHT = system/"them"). Only engages when a recording
# declares its layout AND names the mic speaker, who must be enrolled. Every
# threshold is env-overridable so it can be calibrated against real recordings;
# the voiceprint safety net + mono fallback keep a mis-tuned day from being
# worse than plain mono. See stt/channels.py.
MIC_SPEAKER = os.environ.get("STT_MIC_SPEAKER") or None  # who "me" is; None = feature off
CHANNEL_FLOOR_DBFS = float(os.environ.get("STT_CHANNEL_FLOOR_DBFS", "-45"))   # mic noise gate
CHANNEL_DOMINANCE_DB = float(os.environ.get("STT_CHANNEL_DOMINANCE_DB", "8")) # mic must beat system by this
CHANNEL_ENTER_MS = float(os.environ.get("STT_CHANNEL_ENTER_MS", "200"))       # hysteresis: enter me-active
CHANNEL_EXIT_MS = float(os.environ.get("STT_CHANNEL_EXIT_MS", "250"))         # hysteresis: leave me-active
CHANNEL_BRIDGE_MS = float(os.environ.get("STT_CHANNEL_BRIDGE_MS", "200"))     # merge me-spans closer than this
CHANNEL_MIN_SPAN_MS = float(os.environ.get("STT_CHANNEL_MIN_SPAN_MS", "300")) # drop me-spans shorter than this
CHANNEL_FORCE_MIN = float(os.environ.get("STT_CHANNEL_FORCE_MIN", "0.55"))    # cosine gate: span really the mic speaker
CHANNEL_PASS_FRACTION = float(os.environ.get("STT_CHANNEL_PASS_FRACTION", "0.5"))  # below this -> abandon, use mono

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


def recordings_dir() -> Path:
    return Path(_env_file().get("STT_RECORDINGS_DIR") or RECORDINGS_DIR)


def mic_speaker() -> str | None:
    """Who "me" is on the meeting recorder's mic channel — read fresh from
    stt.env so the menu-bar recorder (launched by launchd without stt.env in
    its environment) still picks up a change without a rebuild. None = the
    channel-aware path stays off and recordings process as mono."""
    return _env_file().get("STT_MIC_SPEAKER") or MIC_SPEAKER


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


# --- Archive ---
# Archived meetings live in a dot-prefixed folder INSIDE the meetings store:
# same volume (folder moves are atomic os.rename), travels with any backup of
# the store, and the leading dot means meeting_bases()/globs can never list it —
# which in turn means every endpoint gated on live-meeting membership excludes
# archived meetings automatically (list, search, Ask, export, voice clips).
# A meeting name itself can never collide with it: rename and the recorder both
# strip leading dots.
ARCHIVE_DIRNAME = ".archive"


def archive_dir(dest_dir=None) -> Path:
    return Path(dest_dir or meetings_dir()) / ARCHIVE_DIRNAME


def archived_bases(dest_dir=None) -> list:
    """Archived meetings: same folder-holding-its-json rule as meeting_bases."""
    d = archive_dir(dest_dir)
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

"""Central configuration. Override most values via environment variables."""
import contextlib
import fcntl
import math
import os
import re
import sys
import threading
import uuid
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


def private_umask() -> None:
    """New files owner-only (0600, folders 0700). Meetings, transcripts,
    state and voiceprints (biometric data) must never be readable by other
    local accounts. Every entry point calls this first; ffmpeg and other
    children inherit it. It also runs when this module is imported, so a
    tool script that writes meeting json or state (retitle_all, fix_dates,
    purge and the rest) never writes a world-readable file."""
    os.umask(0o077)


private_umask()


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
# The checkout itself: run.sh, .venv and the code. A spawned command starts
# from here, never from PROJECT_DIR, which under STT_HOME is a data home with
# no run.sh in it: the panel's run sat queued while spawned.log said
# ".../run.sh: No such file or directory".
CODE_DIR = Path(__file__).resolve().parent.parent
VOICEPRINTS_DIR = Path(os.environ.get("STT_VOICEPRINTS_DIR",
                                      PROJECT_DIR / "voiceprints"))
MANIFEST_PATH = PROJECT_DIR / "manifest.json"
WORK_DIR = PROJECT_DIR / "work"  # scratch space for normalized wavs
LOG_DIR = PROJECT_DIR / "logs"

# state files at the root of PROJECT_DIR (the checkout, outside STT_HOME):
# the manifest, status, indexes, search.db, stt.env and their backups
_ROOT_STATE_GLOBS = ("*.json", "*.jsonl", "*.gz", "*.npz", "*.db", "*.db-*",
                     "*.bak", "stt.env", "stt.env.*")
_ROOT_STATE_KEEP = {"stt.env.example"}


def private_roots() -> list:
    """The folders whose whole tree is private data."""
    return [MEETINGS_DIR, VOICEPRINTS_DIR, RECORDINGS_DIR, WORK_DIR, LOG_DIR]


def loose_private_paths() -> list:
    """Every private folder or file that a group or other account can read,
    write or enter. Files written before the owner-only umask keep their old
    mode until something rewrites them. Symlinks are skipped. Reads only."""
    import stat as _stat
    out, seen = [], set()

    def look(p: Path):
        try:
            st = os.lstat(p)
        except OSError:
            return
        if _stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) in seen:
            return
        seen.add((st.st_dev, st.st_ino))
        if st.st_mode & 0o077:
            out.append(p)

    for root in private_roots():
        if not root.is_dir():
            continue
        look(root)
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                look(Path(dirpath) / n)
    if PROJECT_DIR.is_dir():
        for g in _ROOT_STATE_GLOBS:
            for p in sorted(PROJECT_DIR.glob(g)):
                if p.name not in _ROOT_STATE_KEEP and p.is_file():
                    look(p)
    return out


def tighten_private_paths(paths=None) -> int:
    """Remove the group and other bits from `paths` (default: every loose
    private path). Returns how many changed."""
    n = 0
    for p in (loose_private_paths() if paths is None else paths):
        try:
            mode = os.lstat(p).st_mode
            os.chmod(p, mode & 0o7700)
            n += 1
        except OSError:
            pass
    return n


def migration_dir() -> Path:
    """Where one-time store changes keep their undo journals (mode 700).
    Outside the repo on purpose: a journal holds the old values of meeting
    fields, so it must never sit where `git add` can reach it.
    STT_MIGRATION_DIR wins, then STT_HOME, then Application Support."""
    env = os.environ.get("STT_MIGRATION_DIR")
    if env:
        return Path(env).expanduser()
    if _HOME:
        return _HOME / "migration"
    return HOME / "Library/Application Support/com.stt-workflow/migration"

def _env_number(name, default, cast, valid, rule):
    """An import-time number setting from the environment. An empty value
    means the default. A value that does not parse, or that breaks `rule`,
    prints a warning that names the setting and keeps the default: a typo
    in stt.env must never stop every command that imports this module."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = cast(raw.strip())
        if valid(v):
            return v
    except ValueError:
        pass
    print(f"warning: {name}={raw!r} is not {rule}; using {default}",
          file=sys.stderr, flush=True)
    return default


# --- Models ---
# Parakeet TDT 0.6B v3 (Nov 2026 swap from v2): closer to Scribe v2 in a shadow test, keeps fillers, ~15% slower.
PARAKEET_MODEL = os.environ.get("STT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
DIARIZATION_MODEL = os.environ.get(
    "STT_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1"
)

# Diarizer engine (Nov 2026 bakeoff): "speakrs" (default) runs the pyannote
# community-1 recipe in Rust on Core ML, about 100x faster, and matched or beat
# pyannote against a Scribe v2 reference on every benchmark meeting. "pyannote"
# is the original CPU path and the automatic fallback when the speakrs helper
# or its models are missing or the run fails. Per-turn embeddings and
# absorption splitting use the pyannote embedder under BOTH engines. Cluster
# centroids (centroid naming, unknown ids, enroll --audio) come from the engine
# that ran: under speakrs, its Core ML port of the same WeSpeaker embedder
# (cosine 0.995-1.000 to pyannote's centroids in the bakeoff).
DIARIZERS = ("speakrs", "pyannote")


def _diarizer_setting() -> str:
    """STT_DIARIZER, checked: diarize() runs speakrs only on an exact
    "speakrs", so a typo ("speakr") used to switch every file to the slow
    CPU engine with no message. An unknown value warns and keeps the
    default; an empty one is the default."""
    raw = os.environ.get("STT_DIARIZER", "")
    v = raw.strip().lower()
    if not v:
        return DIARIZERS[0]
    if v not in DIARIZERS:
        print(f"warning: STT_DIARIZER={raw!r} is not one of "
              f"{' or '.join(DIARIZERS)}; using {DIARIZERS[0]}",
              file=sys.stderr, flush=True)
        return DIARIZERS[0]
    return v


DIARIZER = _diarizer_setting()
# AHC merge threshold. speakrs' default 0.6 merged two late speakers into the
# main one in a 4-hour meeting; 0.55 kept them apart and changed nothing else.
SPEAKRS_AHC = _env_number("STT_SPEAKRS_AHC", 0.55, float,
                          lambda v: math.isfinite(v) and v >= 0,
                          "a finite number 0 or above")
SPEAKRS_BIN = Path(os.environ.get(
    "STT_SPEAKRS_BIN",
    Path(__file__).resolve().parent.parent / "native/speakrs/target/release/stt-speakrs"))
# Core ML model bundle from huggingface.co/avencera/speakrs-models, pinned to
# the revision the bakeoff used (tools/fetch_speakrs_models.py downloads and
# checks it once; nothing is sent anywhere after that).
SPEAKRS_MODELS_REV = "67f963b6a1d94fcd4678948237b3cc8bd69bf1d8"
SPEAKRS_MODELS = Path(os.environ.get(
    "STT_SPEAKRS_MODELS",
    HOME / "Library/Application Support/com.stt-workflow/speakrs-models" / SPEAKRS_MODELS_REV))
SPEAKRS_TIMEOUT_SEC = _env_number("STT_SPEAKRS_TIMEOUT_SEC", 3600, int,
                                  lambda v: v > 0, "a whole number of seconds above 0")

# Turn-level recluster after speakrs (stt/recluster.py): a turn whose own
# pyannote embedding fits another cluster's centroid better than its own (by
# more than MARGIN, own weight left out) moves there. Centroids are built from
# turns >= CENTROID_MIN_DUR; turns >= MOVE_MIN_DUR may move; a turn never moves
# into a cluster with under DST_MIN_SECS of centroid-building talk. Measured
# 2026-11 on 7 meetings vs Scribe v2 (qa/diar-opt/REPORT.md), final stage,
# with the junk-repair guard below: mean agreement 89.73 -> 91.99, no meeting
# lower. The moves carry S1 (+0.9) and S6 (+4.4). Swept on S1-S4, S5-S7 held
# out. Margin 0-0.02 is a plateau; centroid turns >= 8 s lost 0.4 on S3; with
# no destination floor S3 lost 2.4-3.6 (public commenters absorbed). Inert
# under ~2 min (no cluster reaches the floor). speakrs engine only: the
# pyannote fallback was not measured. STT_RECLUSTER=0 turns it off.
RECLUSTER = os.environ.get("STT_RECLUSTER", "1") == "1"
RECLUSTER_MARGIN = float(os.environ.get("STT_RECLUSTER_MARGIN", "0.02"))
RECLUSTER_CENTROID_MIN_DUR = float(os.environ.get("STT_RECLUSTER_CENTROID_MIN_DUR", "5.0"))
RECLUSTER_MOVE_MIN_DUR = float(os.environ.get("STT_RECLUSTER_MOVE_MIN_DUR", "0.3"))
RECLUSTER_DST_MIN_SECS = float(os.environ.get("STT_RECLUSTER_DST_MIN_SECS", "120"))
RECLUSTER_ITERS = int(os.environ.get("STT_RECLUSTER_ITERS", "2"))

# ASR backend: "parakeet" (default, native MLX), "mlxwhisper" (Whisper on the
# GPU, the variant in STT_WHISPER_MLX_MODEL), or "cloud:<provider>".
ASR_BACKENDS = ("parakeet", "mlxwhisper")
CLOUD_ASR = ("scribe", "openai", "voxtral")      # asr_cloud.PROVIDERS


def _asr_problem(raw):
    """(backend, None) for a usable STT_ASR_BACKEND value, else
    ("parakeet", why). Case and spaces are ignored. pipeline._load_asr runs
    Whisper only on an exact "mlxwhisper", so a typo ('mlx-whisper',
    'whisper', or the 'whisperx' an older stt.env.example named) ran
    Parakeet with no message."""
    v = str(raw or "").strip().lower()
    if not v:
        return ASR_BACKENDS[0], None
    if v in ASR_BACKENDS or (v.startswith("cloud:") and v[6:] in CLOUD_ASR):
        return v, None
    return ASR_BACKENDS[0], (f"{raw!r} is not parakeet, mlxwhisper, or cloud:"
                             f"{'/'.join(CLOUD_ASR)}; using parakeet")


def _asr_backend_setting() -> str:
    raw = os.environ.get("STT_ASR_BACKEND", "")
    v, why = _asr_problem(raw)
    if why:
        print(f"warning: STT_ASR_BACKEND={why}", file=sys.stderr, flush=True)
    return v


ASR_BACKEND = _asr_backend_setting()

# --- Audio (video containers welcome too — ffmpeg extracts the audio track) ---
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".mp4", ".caf", ".m4b", ".flac",
              ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ogg", ".opus", ".wma", ".aiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SAMPLE_RATE = 16000


def is_source_audio(p) -> bool:
    """True when a file in a watched folder is a recording the batch takes:
    an audio or video type, not a hidden file, not an Office owner/lock
    file ("~$name"), and not empty. A 0-byte file (a failed export, a copy
    still being made) is skipped until it has bytes. The batch, the panel's
    queue and the menu bar share this test, so the panel never lists a file
    the batch will not pick."""
    p = Path(p)
    if p.suffix.lower() not in AUDIO_EXTS or p.name.startswith((".", "~$")):
        return False
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False

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
# Mid-band open-set rescue (exp1-band-gap, 07/2026 eval): a 0.6-1.5s turn
# stranded in an UNNAMED cluster is reassigned to an enrolled speaker who is
# roster rank-1 on the turn's OWN embedding by >= RESCUE_MARGIN over the
# runner-up AND clears a floor: RESCUE_MIN anywhere (the same open-set bar the
# reliable-band reassignment uses), or NEIGHBOR_MIN when that speaker also
# owns an ADJACENT turn — adjacency + rank-1 + margin are three agreeing
# signals, so weaker voice evidence suffices (the two human-verified rescues
# on the real library scored 0.511 and 0.375). Named-cluster turns are NEVER
# reassigned in this band: the eval found zero fixable named->named cases and
# the swept variants that allowed them regressed real human corrections
# (correct mid-band own-scores have median ~0.32-0.37, so a rival clearing a
# low bar is noise, not identity — see qa/eval/experiments.md, run D).
REFINE_MIDBAND_RESCUE_MIN = float(
    os.environ.get("STT_REFINE_MIDBAND_RESCUE_MIN", "0.70"))
REFINE_MIDBAND_RESCUE_MARGIN = float(
    os.environ.get("STT_REFINE_MIDBAND_RESCUE_MARGIN", "0.10"))
REFINE_MIDBAND_NEIGHBOR_MIN = float(
    os.environ.get("STT_REFINE_MIDBAND_NEIGHBOR_MIN", "0.35"))

# Junk-fragment repair (exp2-cluster-repair, 07/2026 eval): the diarizer
# scatters echo/system-audio fragments of the REAL attendees into small
# unnamed clusters (Mark C. Weekly holds 206 such turns; their CLUSTER
# centroids are cross-voice mixtures, so cluster-level merging measures dead —
# max centroid-vs-named cosine 0.33). Per-TURN evidence works: a still-unnamed
# turn whose own embedding matches a NAMED cluster's centroid in the SAME
# meeting (same room/mic — echo fragments score 0.5-0.8 here vs 0.4-0.75
# against cross-meeting voiceprints) inherits that name when the cosine clears
# JUNK_REPAIR_MIN with JUNK_REPAIR_MARGIN over the runner-up named centroid
# AND the enrolled roster ranks the same person first on the turn's own
# embedding. Attendee-anchored by construction (the reference cluster already
# cleared open-set naming — nobody absent from the meeting can be introduced),
# and the two independent references must agree. Swept 07/2026
# (qa/eval/experiments.md exp2): floor 0.50 fixes nothing; 0.45 fixes 2 human
# corrections; 0.40 fixes 4 with zero row-level breaks, and its full library
# blast radius is 70 fragments across 7 meetings, every one roster-corroborated
# (70/70) — unlike the rejected roster-wide low floors of exp1, this gate
# cannot name anyone without a named cluster in the room. Margin 0.10 vs 0.15
# scored identically; 0.10 matches the mid-band rescue margin. Observed noise
# fragments (the Weekly Anthropic junk cluster) top out ~0.35 vs named
# centroids, so 0.40 keeps clearance. Set the floor above 1.0 to disable.
# Strict mode never runs this step.
REFINE_JUNK_REPAIR_MIN = float(
    os.environ.get("STT_REFINE_JUNK_REPAIR_MIN", "0.40"))
REFINE_JUNK_REPAIR_MARGIN = float(
    os.environ.get("STT_REFINE_JUNK_REPAIR_MARGIN", "0.10"))
# Junk repair skips a turn whose own cluster holds more than this much talk:
# a fragment cluster is small, a large one is a real unmatched person. On S2
# junk repair moved ~2000 s of three unmatched clusters (1239-3438 s each)
# into one enrolled name. Measured 2026-11 with the recluster above: S2
# 90.8 -> 93.0-93.3 at 100 s, 92.0 at 150-300 s, 93.4 at 80 s; none lower.
# 100 s, not 80: S3 has a dense band of 70-85 s clusters, so a cut at 80 is
# chance. Without the recluster the guard cost S6 0.5. 1e9 turns it off.
REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS = float(
    os.environ.get("STT_REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS", "100"))

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

# --- Absorption splitting (exp2-cluster-repair, 07/2026 eval) ---
# pyannote's exclusive diarization can hand back one long "turn" spanning
# several speakers it clustered together (the 4-voice synth conversation keeps
# 12-18s turns holding three people; the diarizer never cut a boundary, so no
# cache-side relabel can fix them). At DIARIZE time — audio and embedder in
# hand — each turn >= SPLIT_MIN_DUR is probed with sliding sub-windows
# (SPLIT_WIN long, every SPLIT_HOP); a cut lands only where BOTH halves'
# mean embeddings rank DIFFERENT enrolled speakers, each by a real margin
# (SPLIT_HALF_MARGIN), and the halves' cosine dips to <= SPLIT_DIP_MAX;
# recursion up to SPLIT_MAX_DEPTH handles 3+-speaker spans. Measured on the
# synth corpus (qa/eval/experiments.md exp2): single-voice turns NEVER pass
# the different-names gate (their dip floor is 0.52 and both halves always
# rank the same person — zero false splits on the all-correct conversation),
# while multi-voice turns cut within ~0.2s of scripted truth: synth 72.2 ->
# 84.1%, the 4-speaker conversation 42.4 -> 90.9%, frame DER-like 26.5 ->
# 12.8%, 18 row fixes and 0 breaks. Dip 0.60 lost two of those fixes; margin
# 0.15 scored the same as 0.10. Split turns land in the .diar.npz cache, so
# relabel and the eval replay them without re-diarizing (existing caches are
# untouched until a meeting is reprocessed). Dip <= 0 disables. Strict mode
# and meetings with no enrolled voiceprints never split (identity evidence IS
# the gate).
SPLIT_DIP_MAX = float(os.environ.get("STT_SPLIT_DIP_MAX", "0.65"))
SPLIT_HALF_MARGIN = float(os.environ.get("STT_SPLIT_HALF_MARGIN", "0.10"))
SPLIT_MIN_DUR = float(os.environ.get("STT_SPLIT_MIN_DUR", "2.4"))
SPLIT_WIN = float(os.environ.get("STT_SPLIT_WIN", "1.2"))
SPLIT_HOP = float(os.environ.get("STT_SPLIT_HOP", "0.3"))
SPLIT_MAX_DEPTH = int(os.environ.get("STT_SPLIT_MAX_DEPTH", "3"))

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
    # keyed by the engine that will actually run (rates.effective_diarizer):
    # pyannote on CPU ~2.25x. speakrs on Core ML ran 130-160x in the bench,
    # but the pyannote-embedder turn pass and model load dominate after it:
    # 31.6x end to end on a 24-minute meeting. Learned rates replace this.
    "diarize": {"pyannote": 2.25, "speakrs": 30.0},
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


def is_live_store(d) -> bool:
    """True when folder `d` is the meetings library (meetings_dir()). A run
    that writes somewhere else (a test or demo dest) must never touch the
    library's index, or its meetings by name.

    Two folders that both exist compare as files (os.path.samefile): APFS
    folds letter case and resolve() does not, so '~/projects/...' and
    '~/Projects/...' read as two folders and a run into the library wrote
    no descriptions."""
    try:
        a, b = Path(d), meetings_dir()
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
        return a.resolve() == b.resolve()
    except OSError:
        return False


def recordings_dir() -> Path:
    return Path(_env_file().get("STT_RECORDINGS_DIR") or RECORDINGS_DIR)


def source_dirs(source=None) -> list:
    """Every folder the batch takes source audio from, in THE resolution
    order: the primary source (iCloud), the recorder's staging folder, and
    the Voice Memos sync's staging copies. The batch, the panel's queue, the
    menu bar, the doctor and the archive note all read this one list, so a
    file the batch will transcribe is never invisible to the others. The
    Voice Memos library itself is never a source. `source` overrides the
    primary folder (run_batch --source)."""
    out = [Path(source) if source is not None else source_dir()]
    rec = recordings_dir()
    if rec not in out:
        out.append(rec)
    try:
        from . import voicememos
        vm = voicememos.staging_dir()
        if vm not in out:
            out.append(vm)
    except Exception:
        pass
    return out


def mic_speaker() -> str | None:
    """Who "me" is on the meeting recorder's mic channel — read fresh from
    stt.env so the menu-bar recorder (launched by launchd without stt.env in
    its environment) still picks up a change without a rebuild. None = the
    channel-aware path stays off and recordings process as mono."""
    return _env_file().get("STT_MIC_SPEAKER") or MIC_SPEAKER


# --- Local LLM hardware limits (opt-in quiet mode) ---
# By default local LLM runs (Ollama Qwen 3.8, or the MLX Qwen3-8B fallback)
# run at full speed: normal priority, no rest between meetings. Quiet mode is
# opt-in. With STT_LLM_NICE above 0, before each call the Ollama processes are
# set to that nice (an absolute value, never an increment) and put on
# background QoS (`taskpolicy -b`, which also lowers their GPU priority), and
# the MLX subprocess starts at that nice under `taskpolicy -b`. At 0 neither
# happens. A loop over many meetings (tools/retitle_all.py, the batch's
# summary sweeps) rests after each meeting only when the rest settings ask:
# inside STT_LLM_QUIET_HOURS (default 07:00-22:00) it rests STT_LLM_REST_RATIO
# times the wall time the meeting took, and at least STT_LLM_REST_MIN_SEC.
# Outside them it rests STT_LLM_REST_RATIO_NIGHT times that time. All three
# default to 0. One panel request (Regenerate, Ask) never rests. Read fresh
# from stt.env: no restart.
LLM_NICE_DEFAULT = 0
LLM_REST_RATIO_DEFAULT = 0.0
LLM_REST_RATIO_NIGHT_DEFAULT = 0.0
LLM_REST_MIN_SEC_DEFAULT = 0.0
LLM_QUIET_HOURS_DEFAULT = "07:00-22:00"


def _num_setting(name, default, cast=float, lo=0):
    raw = _env_file().get(name) or os.environ.get(name)
    try:
        v = cast(raw) if raw not in (None, "") else default
    except ValueError:
        v = default
    return max(lo, v)


def llm_nice() -> int:
    return min(20, _num_setting("STT_LLM_NICE", LLM_NICE_DEFAULT, int))


def llm_background() -> bool:
    """True in quiet mode (STT_LLM_NICE above 0): local model processes are
    reniced and put on background QoS. False runs them at normal priority."""
    return llm_nice() > 0


def llm_rest_ratio() -> float:
    return _num_setting("STT_LLM_REST_RATIO", LLM_REST_RATIO_DEFAULT)


def llm_rest_ratio_night() -> float:
    return _num_setting("STT_LLM_REST_RATIO_NIGHT", LLM_REST_RATIO_NIGHT_DEFAULT)


def llm_rest_min_sec() -> float:
    return _num_setting("STT_LLM_REST_MIN_SEC", LLM_REST_MIN_SEC_DEFAULT)


def _hm(s: str) -> int:
    """Minutes since midnight for 'HH:MM' or a bare hour ('10')."""
    h, _, m = s.strip().partition(":")
    h, m = int(h), int(m or 0)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(s)
    return h * 60 + m


def quiet_window(raw: str):
    """(start, end) in minutes since midnight for an STT_LLM_QUIET_HOURS
    value. The two times are joined by '-', an en or em dash, or 'to', with
    or without spaces ('10:00–12:00', '10-12', '10:00 to 12:00'). A time is
    'HH:MM' or a bare hour. Raises ValueError for anything else. The same
    time at both ends is all day (llm_quiet_now): quiet hours only make the
    model rest longer, so the reading that rests more is the safe one."""
    parts = re.split(r"\s*(?:-|\u2013|\u2014|\bto\b)\s*", str(raw or "").strip(),
                     flags=re.I)
    if len(parts) != 2:
        raise ValueError(raw)
    return _hm(parts[0]), _hm(parts[1])


_setting_warned = set()


def _warn_once(key, msg: str):
    """One warning per process for each unreadable value of a setting that
    is read fresh on every call: a loop must not print it every meeting."""
    if key not in _setting_warned:
        _setting_warned.add(key)
        print(f"warning: {msg}", file=sys.stderr, flush=True)


def _warn_setting(name: str, raw, rule: str, using):
    _warn_once((name, raw), f"{name}={raw!r} is not {rule}; using {using}")


def _quiet_raw():
    return _env_file().get("STT_LLM_QUIET_HOURS") or os.environ.get("STT_LLM_QUIET_HOURS")


def setting_problems() -> list:
    """[(setting, why)] for each stt.env value that cannot be read and is
    replaced by its default. The doctor lists them: a typo there used to
    change behavior with no word anywhere."""
    out = []
    raw = _quiet_raw()
    if raw:
        try:
            quiet_window(raw)
        except ValueError:
            out.append(("STT_LLM_QUIET_HOURS",
                        f"{raw!r} is not a window such as 07:00-22:00; "
                        f"using {LLM_QUIET_HOURS_DEFAULT}"))
    from . import summarize
    why = summarize.ollama_url_problem()
    if why:
        out.append(("STT_OLLAMA_URL", why))
    _, why = _asr_problem(_env_file().get("STT_ASR_BACKEND")
                          or os.environ.get("STT_ASR_BACKEND"))
    if why:
        out.append(("STT_ASR_BACKEND", why))
    from . import voicememos
    why = voicememos.delete_setting_problem()
    if why:
        out.append((voicememos.DELETE_SETTING, why))
    return out


def llm_quiet_now(now=None) -> bool:
    """True inside STT_LLM_QUIET_HOURS ("HH:MM-HH:MM", may wrap midnight).
    An unreadable value warns once and falls back to the default window."""
    from datetime import datetime as _dt
    now = now or _dt.now()
    raw = _quiet_raw()
    try:
        start, end = quiet_window(raw or LLM_QUIET_HOURS_DEFAULT)
    except ValueError:
        _warn_setting("STT_LLM_QUIET_HOURS", raw, "a window such as 07:00-22:00",
                      LLM_QUIET_HOURS_DEFAULT)
        start, end = quiet_window(LLM_QUIET_HOURS_DEFAULT)
    t = now.hour * 60 + now.minute
    if start == end:
        return True
    return start <= t < end if start < end else (t >= start or t < end)


def llm_rest_seconds(work_sec: float, now=None) -> float:
    """How long a multi-meeting LLM loop rests after one meeting that took
    `work_sec` of wall time."""
    work = max(0.0, float(work_sec or 0))
    if llm_quiet_now(now):
        return max(llm_rest_min_sec(), llm_rest_ratio() * work)
    return llm_rest_ratio_night() * work


def llm_rest(work_sec: float, log=None, sleep=None) -> float:
    """Rest after one meeting of a multi-meeting LLM loop, and log it."""
    import time as _t
    sec = llm_rest_seconds(work_sec)
    if sec > 0:
        if log:
            log(f"   resting {sec:.0f}s after {float(work_sec or 0):.0f}s of model work "
                "(STT_LLM_REST_RATIO / STT_LLM_REST_MIN_SEC / STT_LLM_QUIET_HOURS)")
        (sleep or _t.sleep)(sec)
    return sec


# --- Per-meeting folder layout ---
# Every meeting's artifacts live in their own folder:
#   <meetings_dir>/<base>/<base>.json  (+ .txt, .m4a, .diar.npz, .emb.npz,
#   .reviews.json, .verify.json, ...)
# The folder AND the files carry the meeting name, so a rename in the GUI
# renames both and everything stays greppable/openable from Finder.

# a meeting's stored audio: every type the batch ingests, since a non-video
# source is stored under its own suffix (<base>.flac). One list: the order
# only says which copy meeting_audio() prefers when a folder holds two.
_AUDIO_PREFERRED = (".m4a", ".mp4", ".wav", ".mp3", ".aiff", ".mov")
AUDIO_SUFFIXES = _AUDIO_PREFERRED + tuple(sorted(AUDIO_EXTS - set(_AUDIO_PREFERRED)))
# sidecar jsons that are NOT a meeting's main transcript
_SIDECAR_JSON = (".reviews.json", ".verify.json", ".reviews.superseded.json")


# A meeting's folder name is also the stem of every file in it, and the
# longest artifact name adds about 40 bytes (".reviews.superseded.unmerged-
# <time>"), plus a " (NN)" twin suffix. macOS allows 255 bytes per name, so
# a folder name made from a source file name is capped well under that.
MAX_BASE_BYTES = 120


def cap_name(name: str, limit: int = MAX_BASE_BYTES) -> str:
    """`name` cut to at most `limit` UTF-8 bytes, never inside a character,
    with no trailing space, dot, dash or underscore left by the cut."""
    raw = str(name).encode("utf-8")
    if len(raw) <= limit:
        return name
    cut = raw[:limit].decode("utf-8", "ignore")
    return cut.rstrip(" .-_") or cut


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


# --- Stable meeting id -----------------------------------------------------
# A basename is a meeting's NAME, not its identity: a title edit or a date
# correction renames the folder, and six stores keyed on that basename then had
# to be chased by hand on every rename (best-effort, inside one swallowing try).
# meeting_id is minted once — by the pipeline for a new meeting, by
# backfill_meeting_ids for every meeting that predates this — written into the
# meeting json, and never changed again. A rename becomes a folder move plus
# one index update; a lock file, a registry reference or a queued job can name
# a meeting that is being renamed underneath it and still land on the right one.
MEETINGS_INDEX_PATH = PROJECT_DIR / "meetings_index.json"

# The full-text index behind /api/search (stt/search.py). SQLite FTS5, mode
# 600, rebuildable from the meeting jsons at any time, so losing it loses
# nothing. It holds transcript text, so it sits under PROJECT_DIR beside the
# manifest (STT_HOME redirects it for tests and demos) and the root rule
# /*.db* keeps it, and any -journal, -wal or -shm file, out of git.
SEARCH_DB = PROJECT_DIR / "search.db"

# "meeting_id" as it appears in a json's head. A transcript could in principle
# SAY the words "meeting_id": the negative lookbehind makes the escaped quotes
# of json string content ( \"meeting_id\" ) fail to match, so only a real key
# can. The head read exists because a rebuild parses every meeting in the
# library and a transcript json runs to megabytes; the id is written as the
# FIRST key precisely so this bounded read finds it.
_ID_HEAD_BYTES = 65536
_ID_RE = re.compile(rb'(?<!\\)"meeting_id"\s*:\s*"([^"\\]{1,64})"')

_index_lock_depth = threading.local()


def new_meeting_id() -> str:
    """16 hex chars of a uuid4 — short enough to read inside a lock filename,
    wide enough (64 bits) that two meetings never collide."""
    return uuid.uuid4().hex[:16]


def id_of(base: str, dest_dir=None):
    """This meeting's immutable id, or None when its json carries none (a
    legacy meeting the backfill has not reached, or no meeting at all)."""
    j = meeting_file(base, ".json", dest_dir)
    try:
        with open(j, "rb") as fh:
            head = fh.read(_ID_HEAD_BYTES)
    except OSError:
        return None
    m = _ID_RE.search(head)
    if m:
        return m.group(1).decode("utf-8", "replace") or None
    if len(head) < _ID_HEAD_BYTES:
        return None                      # whole file read: there is no id
    try:                                 # a big json that buried it — pay full price
        import json as _json
        return _json.loads(j.read_text()).get("meeting_id") or None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def _index_locked():
    """Serialize read-modify-write of the id index across processes (the panel,
    a CLI relabel, the batch). Re-entrant within one thread, like
    review.lock_meeting and identify.lock_registry."""
    depth = getattr(_index_lock_depth, "n", 0)
    if depth:
        _index_lock_depth.n = depth + 1
        try:
            yield
        finally:
            _index_lock_depth.n = depth
        return
    p = Path(str(MEETINGS_INDEX_PATH) + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _index_lock_depth.n = 1
        try:
            yield
        finally:
            _index_lock_depth.n = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_meetings_index() -> dict:
    """{"ids": {meeting_id: base}} — always that shape, even when the file is
    missing or corrupt. The index is a CACHE of what the meeting jsons say, so
    an unreadable one is rebuilt from a scan, never mourned."""
    import json as _json
    try:
        data = _json.loads(MEETINGS_INDEX_PATH.read_text())
    except (OSError, ValueError):
        return {"ids": {}}
    ids = data.get("ids")
    return {"ids": ids if isinstance(ids, dict) else {}}


def _save_meetings_index(ids: dict):
    import json as _json
    MEETINGS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEETINGS_INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps({"ids": ids}, indent=2, ensure_ascii=False))
    os.replace(tmp, MEETINGS_INDEX_PATH)


def rebuild_meetings_index(dest_dir=None) -> dict:
    """Rescan the LIVE library and rewrite the index from what the jsons say.
    Archived meetings are deliberately absent: they are not resolvable targets,
    and restore_meeting puts the id back on the way in."""
    ids = {}
    for base in meeting_bases(dest_dir):
        mid = id_of(base, dest_dir)
        if mid:
            ids[mid] = base
    with _index_locked():
        _save_meetings_index(ids)
    return ids


# meeting_id -> (index path, index mtime_ns, meetings-dir mtime_ns) of the last
# lookup that settled on "not live". One entry per id, so it stays as small as
# the set of ids anyone asks about.
_miss_memo = {}


def _mtime_ns(p):
    try:
        return Path(p).stat().st_mtime_ns
    except OSError:
        return None


def base_of(meeting_id: str):
    """The live basename of a meeting id, or None.

    The index is checked against the meeting it names — a mapping whose json is
    gone, or now carries a different id, is stale — and a stale mapping always
    costs one rebuild, so an index left behind by an out-of-band move heals
    itself on the first lookup instead of failing until someone notices.

    A clean miss (the id is simply not in the index) is the common case, not a
    fault: every archived or deleted meeting a registry still names lands here,
    and resolved_meetings / resolved_sources ask on every 2 s panel poll. So a
    clean miss rebuilds only when the index can be behind the library — the
    file is missing, or it is not newer than the meetings dir (a folder was
    added, moved or renamed since it was written). A miss is then memoized per
    (id, index mtime, meetings-dir mtime): the same question against an
    unchanged index and library is answered without a scan or a rewrite, even
    where the mtimes cannot order themselves (an index that could not be
    written, a clock that runs ahead)."""
    if not meeting_id:
        return None
    base = load_meetings_index()["ids"].get(meeting_id)
    if base and id_of(base) == meeting_id:
        return base
    idx_m = _mtime_ns(MEETINGS_INDEX_PATH)
    dir_m = _mtime_ns(meetings_dir())
    stamp = (str(MEETINGS_INDEX_PATH), idx_m, dir_m)
    if idx_m is not None and _miss_memo.get(meeting_id) == stamp:
        return None
    if base is None and idx_m is not None and dir_m is not None and idx_m > dir_m:
        _miss_memo[meeting_id] = stamp      # nothing has moved since the write
        return None
    found = rebuild_meetings_index().get(meeting_id)
    if found:
        _miss_memo.pop(meeting_id, None)
    else:
        _miss_memo[meeting_id] = (str(MEETINGS_INDEX_PATH),
                                  _mtime_ns(MEETINGS_INDEX_PATH),
                                  _mtime_ns(meetings_dir()))
    return found


def index_set(meeting_id: str, base: str) -> bool:
    """Point an id at the base it now lives under. Called by every move: the
    rename in summarize._move_folder, a restore, and the pipeline's own write."""
    if not meeting_id or not base:
        return False
    with _index_locked():
        ids = load_meetings_index()["ids"]
        if ids.get(meeting_id) == base:
            return False
        ids[meeting_id] = base
        _save_meetings_index(ids)
    return True


def index_drop(meeting_id: str) -> bool:
    """Forget an id: the meeting was archived (no longer live) or deleted."""
    if not meeting_id:
        return False
    with _index_locked():
        ids = load_meetings_index()["ids"]
        if ids.pop(meeting_id, None) is None:
            return False
        _save_meetings_index(ids)
    return True


def lock_key(base: str) -> str:
    """The lock-file stem for a meeting: its id when it has one, else its base.

    Keying on the id is what lets a lock be HELD ACROSS a rename — the holder
    and a second caller who only knows the new name still queue on the same
    file — and it is why a rename no longer strews orphan lock files (the live
    library had 262 of them against 101 meetings)."""
    return id_of(base) or id_of(base, archive_dir()) or base


def _requestable_lock_keys(dest_dir=None) -> set:
    """Every lock-file stem lock_key can still ask for.

    That is the key of every LIVE meeting — its id, or its base when it has no
    id yet (a legacy meeting still locks on its name) — and the key of every
    ARCHIVED meeting too: restore_meeting and a delete from the archive take
    lock_meeting on an archived base, and lock_key answers that with the
    archived id. A base-named lock for a meeting that now carries an id is not
    in here: lock_key can never ask for that name again."""
    d = Path(dest_dir) if dest_dir else meetings_dir()
    keep = set()
    for b in meeting_bases(d):
        keep.add(id_of(b, d) or b)
    arc = archive_dir(d)
    for b in archived_bases(d):
        keep.add(id_of(b, arc) or b)
    return keep


def _dead_lock_files(dest_dir=None) -> list:
    """Lock files whose stem nobody can request again, WITHOUT the held test:
    a base-named lock for a meeting that now has an id, or a stem that names
    no live or archived meeting at all."""
    d = Path(dest_dir or meetings_dir()) / ".locks"
    if not d.is_dir():
        return []
    keep = _requestable_lock_keys(dest_dir)
    return [f for f in sorted(d.iterdir())
            if f.is_file() and f.suffix == ".lock" and f.stem not in keep]


# how many candidate lock files are held open at once while the live set is
# re-read: bounded so a library with hundreds of orphans never runs the panel
# process out of file descriptors (macOS defaults to 256).
_PRUNE_CHUNK = 64


def _sweep_lock_files(dest_dir=None, unlink=False, cands=None) -> list:
    """The dead lock files nobody holds, re-checked UNDER the flock.

    Each candidate is taken non-blocking first (a held one is left alone).
    Then, with the chunk's flocks still held, the requestable keys are read
    again from disk, and only a stem that is STILL dead is returned — and,
    with unlink, removed before its flock is released. The re-read catches a
    stem that came back to life between the first scan and the flock: a
    legacy meeting that arrived under that name, or a meeting restored from
    outside the store carrying that id. `cands` limits the sweep to those
    lock files (drop_meeting_locks)."""
    cands = _dead_lock_files(dest_dir) if cands is None else cands
    out = []
    for i in range(0, len(cands), _PRUNE_CHUNK):
        with contextlib.ExitStack() as stack:
            taken = []
            for f in cands[i:i + _PRUNE_CHUNK]:
                try:
                    fh = stack.enter_context(open(f, "r+"))
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held? leave it
                except OSError:
                    continue
                taken.append(f)
            if not taken:
                continue
            keep = _requestable_lock_keys(dest_dir)
            for f in taken:
                if f.stem in keep:
                    continue                 # requestable again: never unlink
                if unlink:
                    try:
                        f.unlink()
                    except OSError:
                        continue
                out.append(f)
    return out


def orphan_lock_files(dest_dir=None) -> list:
    """The dead lock files nobody is holding right now — exactly what
    prune_lock_files would remove, by path, so the registry doctor can name
    them in a report instead of only counting them."""
    return _sweep_lock_files(dest_dir, unlink=False)


def prune_lock_files(dest_dir=None, dry_run=False) -> int:
    """Remove lock files nobody can request again. That is how the live
    library gets from 262 lock files back to one per meeting.

    Only two kinds of stem go: a base-named lock for a meeting that now has an
    id (lock_key asks for the id from then on), and a stem that names no live
    and no archived meeting at all. An archived meeting's lock stays, because
    a restore or a delete still locks under it.

    A lock another process is actually holding is never touched: we take it
    non-blocking first, so a concurrent relabel can never have its lock file
    unlinked out from under it. With the flock held, the stem is checked
    against the live set once more before the unlink.

    The residual window: flock does not stop anyone from OPENING the path. A
    caller that resolves the same dead stem between our re-check and our
    unlink (a stale name that raced a rename, or the pipeline locking a
    brand-new meeting's base before its json exists) opens our inode, waits
    on our flock, and then locks an unlinked file, while the next caller
    creates a fresh one — two holders. The re-check narrows that to a stem
    going from dead to requested within the milliseconds between the re-read
    and the unlink. Closing it outright needs the locker to confirm, after its
    flock, that the path still names the inode it holds, and to retry if not
    (review.lock_meeting).

    Returns how many were removed — or, with dry_run, how many WOULD be
    removed, nothing unlinked."""
    return len(_sweep_lock_files(dest_dir, unlink=not dry_run))


def drop_meeting_locks(stems, dest_dir=None) -> int:
    """Remove the lock files of one deleted meeting: its id and its base.
    The same held test and re-check as prune_lock_files, but over these
    stems only. A panel Delete calls this instead of the whole-library
    sweep, so the sweep's residual race (see prune_lock_files) never runs
    against other meetings while a batch is working. Returns how many went."""
    d = Path(dest_dir or meetings_dir()) / ".locks"
    cands = sorted({d / f"{s}.lock" for s in stems if s and "/" not in s})
    cands = [f for f in cands if f.is_file()]
    return len(_sweep_lock_files(dest_dir, unlink=True, cands=cands)) if cands else 0


def backfill_meeting_ids(dest_dir=None) -> int:
    """Stamp a meeting_id into every meeting json in `dest_dir` that lacks one.

    Idempotent (a meeting with an id is skipped), atomic per meeting, and taken
    under that meeting's own lock so a relabel or a panel edit mid-backfill is
    serialized rather than clobbered. Every other field is preserved with its
    value untouched — generated_at above all: the panel reloads the open
    meeting when that stamp moves, so a migration that re-stamped it would
    yank the page out from under whoever was reading. The id is written as the
    FIRST key so id_of can find it in a bounded read of a megabyte transcript.

    Run over the archive too (dest_dir=config.archive_dir()): a restore has to
    bring the id back with the meeting. Only a live pass touches the index.
    Returns the number of meetings stamped."""
    import json as _json

    from . import review
    d = Path(dest_dir) if dest_dir else meetings_dir()
    n = 0
    for base in meeting_bases(d):
        j = meeting_file(base, ".json", d)
        if id_of(base, d):
            continue
        with review.lock_meeting(base):
            try:                          # re-read UNDER the lock
                data = _json.loads(j.read_text())
            except (OSError, ValueError):
                continue
            if data.get("meeting_id"):
                continue
            data = {"meeting_id": new_meeting_id(), **data}
            tmp = j.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, j)
            n += 1
    if dest_dir is None or Path(dest_dir).resolve() == meetings_dir().resolve():
        rebuild_meetings_index(dest_dir)
        prune_lock_files(dest_dir)
    return n


def _is_flat_meeting(j: Path) -> bool:
    """A flat-layout meeting: a json the pipeline wrote, next to its .txt.
    The pipeline has always written "segments" and "speakers" lists and a
    "source_file" name. A folder the owner picks may hold
    other work, and a stray package.json, notes file or another tool's
    {"segments": [...]} export must never be moved into a folder."""
    import json as _json
    if not j.with_suffix(".txt").is_file():
        return False
    try:
        data = _json.loads(j.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    segs = data.get("segments")
    return (isinstance(segs, list) and all(isinstance(x, dict) for x in segs)
            and isinstance(data.get("speakers"), list)
            and isinstance(data.get("source_file"), str))


def flat_meeting_bases(dest_dir=None) -> list:
    """The flat-layout meetings migrate_flat_meetings would move."""
    d = Path(dest_dir or meetings_dir())
    if not d.is_dir():
        return []
    return sorted(j.stem for j in d.glob("*.json")
                  if not j.name.endswith(_SIDECAR_JSON) and _is_flat_meeting(j))


def adopt_folder_warning(dest_dir) -> str | None:
    """What picking `dest_dir` as the transcripts folder would do to files
    already in it, as one sentence. None when it holds only meeting folders
    (or nothing). The panel asks the owner to confirm before it adopts a
    folder with other files, or one whose flat meetings it would move."""
    d = Path(dest_dir)
    if not d.is_dir():
        return None
    flat = flat_meeting_bases(d)
    live = set(meeting_bases(d))
    other = 0
    for e in d.iterdir():
        if e.name.startswith(".") or e.name in live:
            continue
        if e.is_file() and any(e.name.startswith(b + ".") for b in flat):
            continue
        other += 1
    if not flat and not other:
        return None
    parts = []
    if other:
        parts.append(f"This folder holds {other} item(s) that are not "
                     "meetings. They stay where they are.")
    if flat:
        parts.append(f"{len(flat)} older flat-layout meeting(s) will move "
                     "into their own folders.")
    return " ".join(parts)


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
    for base in flat_meeting_bases(d):
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

#!/bin/bash
# First-run setup: walks a fresh clone to a working install, interactively.
# Safe to re-run any time — every step detects what's already done and skips.
#
#   ./init.sh
#
# What it does, in order:
#   1. Machine checks   — Apple Silicon, memory, free disk
#   2. Tools            — Homebrew, ffmpeg, uv (offers to install the missing)
#   3. Python env       — .venv (3.12) + pipeline dependencies
#   4. Folders          — where recordings arrive, where transcripts land
#   5. Hugging Face     — token for the license-gated diarization model
#   6. Models           — optional pre-download (~3 GB) so night one isn't slow
#   7. Optional extras  — the local model for descriptions and titles (the MLX
#                         fallback and its ~4.5 GB of weights; Ollama is
#                         preferred when present); automation (LaunchAgents)
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVF="$BASE/stt.env"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
# Every answer is read from /dev/tty. With no terminal (ssh without -t, a
# cron or CI run) the read failed, the empty answer read as the default
# Yes, and every install ran unasked. A failed read is a No now, and the
# script stops before step 1 when it has no terminal (has_tty below).
ask()   { local a; read -r -p "  → $1 " a </dev/tty || return 1; echo "$a"; }
yes_no() { local a; a="$(ask "$1 [Y/n]")" || return 1; [[ ! "$a" =~ ^[Nn] ]]; }
has_tty() { { : </dev/tty; } 2>/dev/null; }

# stt.env is read by stt/envfile.py's rules: keys and values trimmed, one
# pair of matching quotes around a value removed, a later line wins.
_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }
_unquote() {
  local v; v="$(_trim "$1")"
  if [ "${#v}" -ge 2 ]; then
    case "$v" in \"*\"|\'*\') v="${v:1:${#v}-2}" ;; esac
  fi
  printf '%s' "$v"
}
_env_key() {  # the key of one stt.env line, or nothing for a comment or a blank
  local line; line="$(_trim "$1")"
  case "$line" in ''|\#*) return 0 ;; esac
  [ "${line#*=}" = "$line" ] && return 0
  _trim "${line%%=*}"
}
env_get() {
  local line out=""
  [ -f "$ENVF" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    [ "$(_env_key "$line")" = "$1" ] && out="$(_unquote "${line#*=}")"
  done < "$ENVF"
  printf '%s' "$out"
}
env_set() {  # env_set KEY VALUE — update-or-append, preserving other lines
  # The value is written as given, byte for byte. A sed substitution read
  # '&' as the matched text, a '|' as the end of its pattern and '\n' as a
  # newline, so a folder named "R & D" corrupted the line.
  local line tmp="$ENVF.tmp.$$" found=0
  touch "$ENVF"; chmod 600 "$ENVF"
  : > "$tmp"; chmod 600 "$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$(_env_key "$line")" = "$1" ]; then
      printf '%s=%s\n' "$1" "$2" >> "$tmp"; found=1
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$ENVF"
  [ "$found" = 1 ] || printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv -f "$tmp" "$ENVF"
}

# The local model for descriptions, title drafts and Ask (stt/summarize.py).
# Ollama with OLLAMA_MODEL is preferred when it answers; the fallback is
# LLM_REPO on MLX in its own .venv-llm. The weights are NOT part of the
# mlx-lm install: without the download below, the first description fetches
# them from Hugging Face, and the old prompt's offline promise was not true.
LLM_REPO="mlx-community/Qwen3-8B-4bit"
OLLAMA_MODEL="qwen3.8:27b-mlx"
LLM_PY="$BASE/.venv-llm/bin/python"
setup_local_llm() {
  echo "  Descriptions, title drafts and Ask use a local model on this Mac."
  echo "  Preferred: Qwen 3.8 (27B) on Ollama, about 29 GB in memory (a 48 GB Mac)."
  echo "  Fallback:  Qwen3-8B on MLX, about 4.5 GB of weights, set up here."
  if command -v ollama >/dev/null 2>&1; then
    ok "Ollama found. If the model is not pulled yet: ollama pull $OLLAMA_MODEL"
  else
    echo "  (For the preferred model, install Ollama from https://ollama.com,"
    echo "   then run: ollama pull $OLLAMA_MODEL)"
  fi
  if [ -x "$LLM_PY" ]; then
    ok "MLX fallback environment already installed"
  elif yes_no "Set up the MLX fallback (a small environment now; the weights next)?"; then
    uv venv --python 3.12 "$BASE/.venv-llm" \
      && uv pip install --python "$LLM_PY" mlx-lm 'transformers<5' \
      && ok "MLX fallback environment installed" \
      || { warn "MLX install failed — everything else still works"; return 0; }
  else
    return 0
  fi
  if "$LLM_PY" -c "from huggingface_hub import snapshot_download as d; d('$LLM_REPO', local_files_only=True)" \
       >/dev/null 2>&1; then
    ok "Qwen3-8B weights already downloaded (descriptions work offline)"
  elif yes_no "Download the Qwen3-8B weights now (~4.5 GB)? If not, the first description downloads them."; then
    "$LLM_PY" -c "from huggingface_hub import snapshot_download as d; d('$LLM_REPO')" \
      && ok "Qwen3-8B weights downloaded (descriptions work offline)" \
      || warn "download failed — the first description downloads the weights instead"
  fi
}

# sourced (the tests load the helpers above): stop before the setup steps
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

if ! has_tty; then
  echo "init.sh asks before each install, so it needs a terminal." >&2
  echo "Run it in Terminal, or over ssh with -t (ssh -t host ./init.sh)." >&2
  exit 1
fi

bold "STT Workflow — first-run setup"
echo "  Everything runs on this Mac; nothing is uploaded unless you later add a"
echo "  cloud transcription key yourself. Re-run this script any time."
echo

# ---------- 1. machine ----------
bold "1/7  Checking this machine"
if [ "$(uname -m)" != "arm64" ]; then
  fail "This is not an Apple Silicon Mac ($(uname -m)). The MLX transcription"
  echo "     models require M-series hardware — this tool will not work here."
  exit 1
fi
ok "Apple Silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo arm64))"

MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if [ "$MEM_GB" -lt 16 ]; then
  warn "${MEM_GB} GB memory — runs, but expect swapping on long recordings (16 GB+ recommended)"
elif [ "$MEM_GB" -lt 24 ]; then
  ok "${MEM_GB} GB memory (fine; 24 GB+ recommended for '--parallel 2')"
else
  ok "${MEM_GB} GB memory"
fi

FREE_GB=$(df -g "$BASE" | awk 'NR==2 {print $4}')
NEED_GB=15   # ~3 GB models + ~5 GB venvs/caches + headroom for audio
if [ "$FREE_GB" -lt "$NEED_GB" ]; then
  fail "${FREE_GB} GB free on this volume — need at least ${NEED_GB} GB (models, environments, audio)"
  yes_no "Continue anyway?" || exit 1
else
  ok "${FREE_GB} GB free disk"
fi

# ---------- 2. tools ----------
bold "2/7  Command-line tools"
if ! command -v brew >/dev/null; then
  warn "Homebrew not found — install it from https://brew.sh first, then re-run."
  exit 1
fi
ok "Homebrew"
for tool in ffmpeg uv; do
  if command -v "$tool" >/dev/null; then
    ok "$tool"
  else
    if yes_no "$tool is missing — install with Homebrew now?"; then
      brew install "$tool" || { fail "brew install $tool failed"; exit 1; }
      ok "$tool installed"
    else
      fail "$tool is required"; exit 1
    fi
  fi
done

# ---------- 3. python env ----------
bold "3/7  Python environment"
if [ -x "$BASE/.venv/bin/python" ]; then
  ok ".venv exists ($("$BASE/.venv/bin/python" -V 2>&1))"
else
  echo "  Creating .venv (Python 3.12) and installing the pipeline…"
  uv venv --python 3.12 "$BASE/.venv" || { fail "uv venv failed"; exit 1; }
fi
if ! "$BASE/.venv/bin/python" -c "import parakeet_mlx, pyannote.audio" 2>/dev/null; then
  echo "  Installing dependencies (a few minutes on first run)…"
  uv pip install --python "$BASE/.venv/bin/python" -r "$BASE/requirements.txt" \
    || { fail "dependency install failed"; exit 1; }
fi
ok "pipeline dependencies installed"

# speakrs: the fast Core ML speaker-separation helper (native/speakrs).
# Without it the pipeline falls back to pyannote on CPU, much slower.
# Rebuilt when its source or pin is newer than the binary (after a pull).
SPK_DIR="$BASE/native/speakrs"
SPK_BIN="$SPK_DIR/target/release/stt-speakrs"
if [ ! -x "$SPK_BIN" ] || [ -n "$(find "$SPK_DIR/src" "$SPK_DIR/Cargo.toml" "$SPK_DIR/Cargo.lock" \
                                   -newer "$SPK_BIN" 2>/dev/null | head -1)" ]; then
  if command -v cargo >/dev/null 2>&1; then
    echo "  Building the speakrs helper (one time, ~1 minute)…"
    (cd "$BASE/native/speakrs" && CARGO_TARGET_DIR="$BASE/native/speakrs/target" \
       cargo build --release --locked -q) || warn "speakrs build failed — pyannote fallback stays in use"
  else
    warn "Rust not installed (brew install rust) — speaker separation uses the slower pyannote path"
  fi
fi
if [ -x "$SPK_BIN" ]; then
  "$BASE/.venv/bin/python" "$BASE/tools/fetch_speakrs_models.py" >/dev/null \
    && ok "speakrs helper and Core ML models ready" \
    || warn "speakrs model download failed — re-run init.sh; pyannote fallback in use"
fi

# ---------- 4. folders ----------
bold "4/7  Your folders"
DEFAULT_SRC="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Voice Recordings"
CUR_SRC="$(env_get STT_ICLOUD_DIR)"; CUR_SRC="${CUR_SRC:-$DEFAULT_SRC}"
echo "  Watched folder — where new recordings arrive (e.g. your iCloud Voice"
echo "  Memos folder). Currently: $CUR_SRC"
NEW_SRC="$(ask "Press Enter to keep, or paste a different path:")"
SRC="${NEW_SRC:-$CUR_SRC}"
mkdir -p "$SRC" 2>/dev/null || true
[ -d "$SRC" ] && ok "watched folder: $SRC" || { fail "cannot create $SRC"; exit 1; }
env_set STT_ICLOUD_DIR "$SRC"

DEFAULT_DST="$HOME/Documents/Meeting Transcripts"
CUR_DST="$(env_get STT_MEETINGS_DIR)"; CUR_DST="${CUR_DST:-$DEFAULT_DST}"
echo "  Transcripts folder — where finished transcripts + audio are stored,"
echo "  one folder per meeting. Currently: $CUR_DST"
NEW_DST="$(ask "Press Enter to keep, or paste a different path:")"
DST="${NEW_DST:-$CUR_DST}"
mkdir -p "$DST" 2>/dev/null || true
[ -d "$DST" ] && ok "transcripts folder: $DST" || { fail "cannot create $DST"; exit 1; }
env_set STT_MEETINGS_DIR "$DST"

# ---------- 5. hugging face ----------
bold "5/7  Hugging Face token (speaker identification)"
if [ -n "$(env_get HF_TOKEN)" ]; then
  ok "HF_TOKEN already set in stt.env"
else
  echo "  The speaker-diarization model is free but license-gated:"
  echo "    1. Sign in at huggingface.co and open:"
  echo "       https://huggingface.co/pyannote/speaker-diarization-community-1"
  echo "       → click 'Agree and access repository' (and any dependency repos listed)"
  echo "    2. Create a READ token: https://hf.co/settings/tokens"
  TOK="$(ask "Paste your hf_… token (or Enter to skip — the batch does not run until it is set):")"
  if [ -n "$TOK" ]; then
    env_set HF_TOKEN "$TOK"
    ok "token saved to stt.env (git-ignored, chmod 600)"
  else
    warn "skipped — the batch stops at its start until HF_TOKEN is set in stt.env"
  fi
fi

# ---------- 6. models ----------
bold "6/7  Models"
echo "  First transcription downloads the models automatically (~3 GB)."
if yes_no "Pre-download them now so the first run is fast?"; then
  "$BASE/.venv/bin/python" - <<'PY' || warn "pre-download hit an error — models will download on first use instead"
from huggingface_hub import snapshot_download
for repo in ("mlx-community/parakeet-tdt-0.6b-v3",):
    print(f"  downloading {repo}…", flush=True)
    snapshot_download(repo)
print("  done — Whisper/diarization models fetch on first use (token-gated).")
PY
fi

# ---------- 7. extras ----------
bold "7/7  Optional extras"
setup_local_llm

if yes_no "Run automatically? (nightly batch + menu-bar app at login)"; then
  "$BASE/setup.sh" install-agent
  "$BASE/setup.sh" gui-install
  echo
  warn "macOS will need Full Disk Access for the Python binary printed above"
  echo "     (System Settings → Privacy & Security → Full Disk Access) so the"
  echo "     nightly run can read your iCloud folder."
fi

echo
bold "Done."
echo "  Try it:            ./run.sh batch --dry-run     (shows what would process)"
echo "  Control panel:     ./run.sh gui                 (menu bar + http://127.0.0.1:8737)"
echo "  Process now:       ./run.sh batch"
echo "  Re-run setup:      ./init.sh"

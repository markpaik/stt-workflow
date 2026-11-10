#!/bin/bash
# Convenience wrapper for interactive use: ./run.sh {batch|enroll|relabel|gui|test|py} [args...]
# The launchd agent calls the venv python directly and does NOT use this script.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
PY="$BASE/.venv/bin/python"
export PYTHONPATH="$BASE"
# Load local secrets/env (HF_TOKEN, STT_* overrides) if present. Parse KEY=VALUE
# the same safe way run_batch.py does rather than sourcing the file as shell:
# the default folder paths contain spaces, and dotting them under set -euo
# pipefail runs the text after the first space as a command and aborts.
# Under STT_HOME (a demo or test home) the settings file is $STT_HOME/stt.env,
# as stt/config.py says: the checkout's file names the REAL library folders.
# The rules are stt/envfile.py's: keys and values trimmed, one pair of
# matching quotes around a value removed, a key that is not a shell name
# skipped (export would abort the script under set -e). A variable already
# set in the shell wins over the file, as in run_batch.py (os.environ.
# setdefault): `STT_X=1 ./run.sh batch` ran with the file's value before,
# while the same run through python used 1. Within the file a later line
# still wins, so the check is against the keys this loop set, not $key.
ENVDIR="${STT_PROJECT_DIR:-${STT_HOME:-$BASE}}"
_from_file=" "
_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }
_unquote() {
  local v; v="$(_trim "$1")"
  if [ "${#v}" -ge 2 ]; then
    case "$v" in \"*\"|\'*\') v="${v:1:${#v}-2}" ;; esac
  fi
  printf '%s' "$v"
}
if [ -f "$ENVDIR/stt.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(_trim "$line")"
    case "$line" in ''|\#*) continue ;; esac
    [ "${line#*=}" = "$line" ] && continue  # no '=' -> not an assignment
    key="$(_trim "${line%%=*}")"
    case "$key" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    case "$_from_file" in
      *" $key "*) ;;                                  # an earlier line set it
      *) printenv "$key" >/dev/null && continue       # the shell set it: keep
         _from_file="$_from_file$key " ;;
    esac
    export "$key=$(_unquote "${line#*=}")"
  done < "$ENVDIR/stt.env"
fi

cmd="${1:-}"; shift || true
case "$cmd" in
  batch|enroll|relabel|gui|test|py) ;;
  *) echo "usage: run.sh {batch|enroll|relabel|gui|test|py} [args...]"; exit 2 ;;
esac
# a fresh clone has no .venv until init.sh builds it: say so, instead of
# the shell's bare ".venv/bin/python: No such file or directory"
if [ ! -x "$PY" ]; then
  echo "run.sh: no Python environment at $PY" >&2
  echo "run.sh: run ./init.sh first; it builds .venv and installs the pipeline." >&2
  exit 1
fi
case "$cmd" in
  batch)  exec "$PY" "$BASE/run_batch.py" "$@" ;;
  enroll) exec "$PY" "$BASE/enroll.py" "$@" ;;
  relabel) exec "$PY" "$BASE/relabel.py" "$@" ;;
  gui)    exec "$PY" "$BASE/gui/menubar.py" "$@" ;;
  test)
    # pytest is a development package (requirements-dev.txt), not a
    # pipeline one, so a plain install does not have it
    if ! "$PY" -c "import pytest" 2>/dev/null; then
      echo "run.sh: pytest is not installed. Install the test packages with:" >&2
      echo "  uv pip install --python .venv/bin/python -r requirements-dev.txt" >&2
      exit 1
    fi
    exec "$PY" -m pytest "$BASE/tests" -q "$@" ;;
  py)     exec "$PY" "$@" ;;
  *) echo "usage: run.sh {batch|enroll|relabel|gui|test|py} [args...]"; exit 2 ;;
esac

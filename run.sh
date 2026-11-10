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
# skipped (export would abort the script under set -e).
ENVDIR="${STT_PROJECT_DIR:-${STT_HOME:-$BASE}}"
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
    export "$key=$(_unquote "${line#*=}")"
  done < "$ENVDIR/stt.env"
fi

cmd="${1:-}"; shift || true
case "$cmd" in
  batch)  exec "$PY" "$BASE/run_batch.py" "$@" ;;
  enroll) exec "$PY" "$BASE/enroll.py" "$@" ;;
  relabel) exec "$PY" "$BASE/relabel.py" "$@" ;;
  gui)    exec "$PY" "$BASE/gui/menubar.py" "$@" ;;
  test)   exec "$PY" -m pytest "$BASE/tests" -q "$@" ;;
  py)     exec "$PY" "$@" ;;
  *) echo "usage: run.sh {batch|enroll|relabel|gui|test|py} [args...]"; exit 2 ;;
esac

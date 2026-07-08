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
if [ -f "$BASE/stt.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    [ "${line#*=}" = "$line" ] && continue  # no '=' -> not an assignment
    export "${line%%=*}=${line#*=}"
  done < "$BASE/stt.env"
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

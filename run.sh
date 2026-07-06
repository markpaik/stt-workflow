#!/bin/bash
# Convenience wrapper for interactive use: ./run.sh {batch|enroll|relabel|gui|test|py} [args...]
# The launchd agent calls the venv python directly and does NOT use this script.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
PY="$BASE/.venv/bin/python"
export PYTHONPATH="$BASE"
# Load local secrets/env (HF_TOKEN, STT_* overrides) if present.
if [ -f "$BASE/stt.env" ]; then set -a; . "$BASE/stt.env"; set +a; fi

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

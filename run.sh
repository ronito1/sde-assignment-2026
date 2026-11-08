#!/usr/bin/env bash
# Starts the controller and runs until the scenario finishes. Contract: ./run.sh <SIM_URL>
# No Docker: plain Python 3.10+ with the repo's requirements (numpy, httpx; sqlite3 is stdlib).
# State lives in $CONTROLLER_DB (default state/controller.db). Re-running against the same simulator
# resumes; a different/restarted simulator world is detected and the store starts fresh.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif ! "$PY" -c "import numpy, httpx" 2>/dev/null; then
  "$PY" -m venv .venv && .venv/bin/pip install -q -r requirements.txt && PY=.venv/bin/python
fi
exec "$PY" -m controller run --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}"

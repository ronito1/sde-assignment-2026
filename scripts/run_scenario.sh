#!/usr/bin/env bash
# Run one scenario end to end: baseline, simulator, controller, grade.
#   scripts/run_scenario.sh scenarios/public_01_dev.yaml [port]
set -euo pipefail
cd "$(dirname "$0")/.."
SC="$1"; PORT="${2:-8700}"
NAME="$(basename "$SC" .yaml)"
PY=.venv/bin/python
mkdir -p reports state runs
[ -d "runs/base_$NAME" ] || $PY -m sim baseline --scenario "$SC" --run-dir "runs/base_$NAME"
rm -f "state/$NAME.db"*
$PY -m sim serve --scenario "$SC" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "runs/sim_$NAME.log" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.5; done
START=$(date +%s)
CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT" 2> "runs/controller_$NAME.log"
echo "controller wall time: $(( $(date +%s) - START ))s"
$PY -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md" > /dev/null
$PY -m controller status --db "state/$NAME.db" > "reports/${NAME}_status.md"
$PY -m controller view --db "state/$NAME.db" > "reports/${NAME}_supervisor_view.md"
sed -n '1,40p' "reports/$NAME.md"

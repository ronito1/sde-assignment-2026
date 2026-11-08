#!/usr/bin/env bash
# Crash-recovery evidence (C13). Kills the controller with SIGKILL at random moments and at injected fault
# points, restarts it each time against the same simulator, lets it finish, then checks invariants.
#   scripts/crash_test.sh scenarios/public_02_incidents.yaml [port] [random_kills]
set -uo pipefail
cd "$(dirname "$0")/.."
SC="$1"; PORT="${2:-8790}"; KILLS="${3:-8}"
NAME="crash_$(basename "$SC" .yaml)"
PY=.venv/bin/python
DB="state/$NAME.db"
LOG="runs/$NAME.log"
mkdir -p runs state reports
rm -f "$DB"*; : > "$LOG"
$PY -m sim serve --scenario "$SC" --run-dir "runs/$NAME" --port "$PORT" --force > "runs/${NAME}_sim.log" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.5; done
URL="http://127.0.0.1:$PORT"
minute() { curl -s "$URL/sim/state" | $PY -c "import json,sys; s=json.load(sys.stdin); print(s['ts'], s['finished'])"; }

echo "== fault injection: crash right after writing an outbox row, before the HTTP call" | tee -a "$LOG"
for i in 1 2 3; do
  CONTROLLER_CRASH_AT=after_prepare:$((i + 1)) CONTROLLER_DB="$DB" ./run.sh "$URL" >> "$LOG" 2>&1
  echo "  run $i exited $? at $(minute)" | tee -a "$LOG"
done
echo "== fault injection: crash just before advancing time" | tee -a "$LOG"
CONTROLLER_CRASH_AT=before_advance:40 CONTROLLER_DB="$DB" ./run.sh "$URL" >> "$LOG" 2>&1
echo "  exited $? at $(minute)" | tee -a "$LOG"
echo "== $KILLS random SIGKILLs" | tee -a "$LOG"
for i in $(seq 1 "$KILLS"); do
  CONTROLLER_DB="$DB" ./run.sh "$URL" >> "$LOG" 2>&1 &
  CTL=$!
  sleep "$($PY -c 'import random; print(round(random.uniform(0.3, 1.0), 2))')"
  kill -9 $CTL 2>/dev/null && echo "  kill -9 #$i at $(minute)" | tee -a "$LOG"
  wait $CTL 2>/dev/null
done
echo "== final run to completion" | tee -a "$LOG"
CONTROLLER_DB="$DB" ./run.sh "$URL" >> "$LOG" 2>&1
echo "  exited $? at $(minute)" | tee -a "$LOG"
$PY scripts/verify_run.py --url "$URL" --db "$DB" | tee -a "$LOG"
RC=${PIPESTATUS[0]}
$PY -m sim grade --run "runs/$NAME" --baseline "runs/base_$(basename "$SC" .yaml)" --out "reports/$NAME.md" > /dev/null
grep -E "applied —|^\| ALL" "reports/$NAME.md" | tee -a "$LOG"
exit $RC

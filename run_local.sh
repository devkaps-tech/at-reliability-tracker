#!/bin/bash
# Run the whole pipeline on this Mac until you press Ctrl+C.
#  - collector: saves the live feed every 2 minutes, restarted automatically if it crashes
#  - scheduler: health check every 5 minutes, nightly scoring job at 04:30
#  - keeps the Mac from going to sleep while it runs (caffeinate)
cd "$(dirname "$0")"
PY=.venv/bin/python

caffeinate -i "$PY" -m tracker.scheduler &
SCHEDULER_PID=$!
trap 'kill $SCHEDULER_PID 2>/dev/null' EXIT

while true; do
  "$PY" -m tracker.collector
  status=$?
  # 0 = you pressed Ctrl+C, 2 = setup problem (e.g. no API key): stop. Anything else = crash: restart.
  if [ $status -eq 0 ] || [ $status -eq 2 ]; then break; fi
  echo "Collector stopped unexpectedly (exit code $status) — restarting in 10 s"
  sleep 10
done

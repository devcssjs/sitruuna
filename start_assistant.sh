#!/usr/bin/env bash
# Start Sitruuna on Mac or Linux:  ./start_assistant.sh
# It restarts itself after /restart (code 3) or a crash; press Ctrl+C to stop.
# On a Mac, "caffeinate -i" keeps the computer awake while Sitruuna runs.
cd "$(dirname "$0")" || exit 1
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi
PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"
KEEP_AWAKE=""
if [ "$(uname)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
  KEEP_AWAKE="caffeinate -i"
fi
CRASHES=0
while true; do
  $KEEP_AWAKE "$PY" main.py
  CODE=$?
  if [ "$CODE" -eq 3 ]; then
    CRASHES=0
    echo "Restarting..."
    continue
  fi
  if [ "$CODE" -eq 0 ] || [ "$CODE" -eq 130 ]; then
    break                                   # normal stop or Ctrl+C
  fi
  CRASHES=$((CRASHES + 1))
  if [ "$CRASHES" -ge 5 ]; then
    echo "Crashed 5 times in a row, stopping."
    "$PY" main.py --notify "Sitruuna crashed 5 times in a row and has stopped. Check data/logs/assistant.log and start it again."
    break
  fi
  echo "Sitruuna stopped unexpectedly. Restarting in 10 seconds..."
  sleep 10
done

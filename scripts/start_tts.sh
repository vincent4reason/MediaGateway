#!/bin/bash
# Start the CosyVoice TTS microservice (127.0.0.1:8001) in its own torch venv.
# Idempotent: no-op if port 8001 is already listening.
# Waits (up to 180s) for /health model_loaded.
set -e
DIR=/Users/vincent/tool/cosyvoice
PORT=8001
LOG="$DIR/tts_server.log"

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "TTS server already listening on $PORT"
else
  cd "$DIR"
  nohup .venv/bin/python tts_server.py > "$LOG" 2>&1 &
  echo "started pid $! (log: $LOG)"
fi

for _ in $(seq 1 180); do
  if curl -s "http://127.0.0.1:$PORT/health" | grep -q '"model_loaded":true'; then
    echo "TTS model loaded"
    exit 0
  fi
  sleep 1
done
echo "TIMEOUT: model not loaded after 180s (check $LOG)" >&2
exit 1

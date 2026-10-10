#!/bin/bash
# Starts the TechnoFunda Desk server (if it is not already running) and opens the site.   macOS: double-click this file in Finder, or run ./start-desk.command
cd "$(dirname "$0")" || exit 1

up() { curl -fsS -m 3 http://localhost:8000/api/status >/dev/null 2>&1; }

if ! up; then
  if [ ! -x .venv/bin/python ]; then
    echo "The Python environment is not set up yet. Run once:  bash scripts/setup_mac.sh"
    read -r -p "Press Return to close." _
    exit 1
  fi
  mkdir -p data
  echo "Starting the TechnoFunda Desk server..."
  nohup .venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 >> data/server.log 2>&1 &
  disown 2>/dev/null || true
  for _ in $(seq 1 60); do        # the first start builds the market tables, which takes a little while
    sleep 1
    up && break
  done
  if ! up; then
    echo "The server did not answer within a minute. The last lines of data/server.log:"
    tail -n 15 data/server.log
    read -r -p "Press Return to close." _
    exit 1
  fi
fi

open "http://localhost:8000/"
ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$ip" ] && echo "On your phone (same Wi-Fi) open: http://$ip:8000"
echo "The server keeps running in the background. Stop it with:  pkill -f 'uvicorn backend.app:app'"

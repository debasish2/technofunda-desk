#!/bin/bash
# First-time setup on a Mac (Apple silicon or Intel).   Run from anywhere:   bash scripts/setup_mac.sh [--full]
#
#   (default)  installs what the app and the nightly jobs need
#   --full     installs every pinned package, including the OCR tools used only by the accuracy programme
set -euo pipefail
cd "$(dirname "$0")/.."

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

# Python 3.11 or newer (pandas 3 needs it). Prefer the newest one that is installed.
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  cat <<'MSG'
Python 3.11 or newer was not found.
Install it, then run this script again:
  - with Homebrew:   brew install python@3.13
  - or download the macOS installer from https://www.python.org/downloads/
(The Python that Apple's Command Line Tools provide is usually too old.)
MSG
  exit 1
fi
echo "Using $($PY --version) at $(command -v $PY)"

if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment (.venv)..."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip

if [ "$FULL" = 1 ]; then
  echo "Installing all pinned packages (a few minutes)..."
  if ! .venv/bin/python -m pip install -r requirements.txt; then
    echo "Some pinned package has no build for this Mac; falling back to the standard set."
    .venv/bin/python -m pip install -r deploy/requirements-server.txt
  fi
else
  echo "Installing the packages the app needs (a minute or two)..."
  .venv/bin/python -m pip install -r deploy/requirements-server.txt tzdata python-dotenv
fi

chmod +x start-desk.command scripts/*.sh 2>/dev/null || true
mkdir -p data

tz="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
cat <<MSG

Setup finished.
  Time zone of this Mac: ${tz:-unknown}   (the market schedule below assumes India time: System Settings > General > Date & Time)

Next:
  1. Bring your data over from the Windows PC (see "On a Mac" in README.md), or let the first nightly run rebuild it.
  2. Put your IndianAPI key into a file named .env in this folder (INDIANAPI_KEY=...), copied privately, not through chat.
  3. Start the app:                     ./start-desk.command      (or double-click it)
  4. Schedule the daily jobs:           .venv/bin/python scripts/mac_services.py install
MSG

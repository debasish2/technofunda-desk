#!/usr/bin/env bash
# Runs one scheduled job. The timers in deploy/systemd call:  run_job.sh nightly | topup | weekly | intraday | backup
set -euo pipefail
cd /opt/technofunda
py=/opt/technofunda/.venv/bin/python
case "${1:-}" in
  nightly)  exec "$py" -m backend.nightly ;;
  topup)    exec "$py" -m backend.nightly --only extras,desk,screens ;;
  weekly)   exec "$py" -m backend.nightly --weekly ;;
  intraday) exec "$py" -m backend.live ;;
  backup)   exec bash deploy/backup.sh ;;
  *) echo "unknown job: ${1:-}" >&2; exit 2 ;;
esac

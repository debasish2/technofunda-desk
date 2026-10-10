#!/bin/bash
# Commits whatever changed in the project and pushes it to GitHub (the repository set as "origin").   macOS / Linux version of backup_github.ps1
# The databases, logs and caches in data/ are ignored by .gitignore, so this only ever sends code and your themes.
# The result of each run is appended to data/backup.log.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
log() { echo "$(date '+%Y-%m-%d %H:%M')  $1" >> data/backup.log; }
git add -A >/dev/null 2>&1
if ! git diff --cached --quiet; then
  git commit -q -m "Nightly backup $(date +%Y-%m-%d)" && log "committed changes"
fi
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
if out="$(git push -q origin main 2>&1)"; then
  log "pushed ($ahead commit(s) sent)"
else
  log "PUSH FAILED - check your internet and your GitHub sign-in: $out"
fi

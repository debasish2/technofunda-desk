#!/usr/bin/env bash
# Daily copy of what cannot be rebuilt from the internet: the logins, each person's settings, saved screens and themes.
# (Prices and fundamentals come back by themselves from the nightly jobs.) Keeps 14 days in /home/desk/backups.
# To keep a copy off the server:   scp desk@YOUR-SERVER:/home/desk/backups/technofunda-$(date +%F).tar.gz .
set -euo pipefail
cd /opt/technofunda
dir=/home/desk/backups
mkdir -p "$dir"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
sqlite3 data/setupdesk.db ".backup '$tmp/setupdesk.db'"
for f in users.json secret.key themes.json; do [ -f "data/$f" ] && cp "data/$f" "$tmp/"; done
[ -d data/userprefs ] && cp -r data/userprefs "$tmp/"
out="$dir/technofunda-$(date +%F).tar.gz"
tar -czf "$out" -C "$tmp" .
chmod 600 "$out"
find "$dir" -name 'technofunda-*.tar.gz' -mtime +14 -delete
echo "backup written: $out ($(du -h "$out" | cut -f1))"

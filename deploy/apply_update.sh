#!/usr/bin/env bash
# Puts new code on the server. Run as root:   sudo bash /opt/technofunda/deploy/apply_update.sh /tmp/app.tar.gz
# The data/ folder is never touched (the archive does not carry it and extraction skips it).
set -euo pipefail
archive="${1:-/tmp/app.tar.gz}"
[ -f "$archive" ] || { echo "no archive at $archive"; exit 1; }
cd /opt/technofunda
tar --exclude='data' -xzf "$archive"
chown -R desk:desk /opt/technofunda
sudo -u desk /opt/technofunda/.venv/bin/pip install -q -r deploy/requirements-server.txt
# unit files may have changed too
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart technofunda
sleep 4
curl -fsS http://127.0.0.1:8000/api/status >/dev/null && echo "updated; the app is answering" || { echo "the app did not come back: journalctl -u technofunda -n 50"; exit 1; }

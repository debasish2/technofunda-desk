#!/usr/bin/env bash
# First-time setup of a fresh Ubuntu 24.04 server for TechnoFunda Desk.   Run as root, from the folder holding app.tar.gz and data.tar.gz:
#
#   sudo bash setup_server.sh --domain desk.example.com     # public https address (you own the domain; its DNS A record already points here)
#   sudo bash setup_server.sh --tailscale                   # private: reachable only through your Tailscale network, no public port
#   sudo bash setup_server.sh                               # neither: the app listens on this machine only (use an ssh tunnel)
#
# It is safe to run again: it will not delete data/.
set -euo pipefail

DOMAIN=""; TS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?give the domain}"; shift 2 ;;
    --tailscale) TS=1; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run this with sudo"; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$here/app.tar.gz" ] || { echo "app.tar.gz is not next to this script ($here)"; exit 1; }
[ -f "$here/data.tar.gz" ] || echo "note: no data.tar.gz here: the server will start empty and the first nightly run has to rebuild everything"

echo "== 1/7 packages and clock"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl ca-certificates tar ufw unattended-upgrades fail2ban >/dev/null
timedatectl set-timezone Asia/Kolkata                     # the timers and the app's market-hours logic are in IST

echo "== 2/7 swap file (the nightly job loads two years of prices; a 1-2 GB server needs the breathing room)"
if [ ! -f /swapfile ] && [ "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" -lt 3500 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 3/7 user and code"
id desk >/dev/null 2>&1 || adduser --system --group --home /home/desk --shell /bin/bash desk
mkdir -p /opt/technofunda /home/desk/backups
tar -xzf "$here/app.tar.gz" -C /opt/technofunda
mkdir -p /opt/technofunda/data
if [ -f "$here/data.tar.gz" ]; then tar -xzf "$here/data.tar.gz" -C /opt/technofunda/data; fi
chmod 600 /opt/technofunda/data/secret.key /opt/technofunda/data/users.json 2>/dev/null || true
sed -i 's/\r$//' /opt/technofunda/deploy/*.sh /opt/technofunda/deploy/systemd/*      # in case Windows line endings came along
chmod +x /opt/technofunda/deploy/*.sh
chown -R desk:desk /opt/technofunda /home/desk

echo "== 4/7 Python packages (a few minutes)"
sudo -u desk python3 -m venv /opt/technofunda/.venv
sudo -u desk /opt/technofunda/.venv/bin/pip install -q --upgrade pip
sudo -u desk /opt/technofunda/.venv/bin/pip install -q -r /opt/technofunda/deploy/requirements-server.txt

echo "== 5/7 services and timers"
bind="127.0.0.1"
[ "$TS" = 1 ] && bind="0.0.0.0"                           # the firewall below lets only the Tailscale interface reach it
echo "BIND=$bind" > /etc/technofunda.env
cp /opt/technofunda/deploy/systemd/*.service /opt/technofunda/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now technofunda.service
systemctl enable --now technofunda-nightly.timer technofunda-topup.timer technofunda-weekly.timer technofunda-intraday.timer technofunda-backup.timer

echo "== 6/7 network"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
if [ -n "$DOMAIN" ]; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gpg >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  printf '%s {\n    encode gzip\n    reverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile
  systemctl restart caddy
fi
if [ "$TS" = 1 ]; then
  curl -fsSL https://tailscale.com/install.sh | sh
  ufw allow in on tailscale0 to any port 8000 proto tcp >/dev/null
fi
ufw --force enable >/dev/null

echo "== 7/7 check"
sleep 6
if curl -fsS http://127.0.0.1:8000/api/status >/dev/null; then echo "the app is running"; else echo "the app did not start: journalctl -u technofunda -n 50"; exit 1; fi
echo
echo "Done."
if [ -n "$DOMAIN" ]; then echo "Open https://$DOMAIN  (the first visit can take a few seconds while the certificate is issued)."; fi
if [ "$TS" = 1 ]; then echo "Now run:  tailscale up   (open the link it prints, sign in), then use  http://<this server's Tailscale name>:8000"; fi
if [ -z "$DOMAIN" ] && [ "$TS" = 0 ]; then echo "From your PC:  ssh -L 8000:127.0.0.1:8000 YOUR-LOGIN@SERVER-IP   then open http://localhost:8000"; fi
echo "Your existing login came across with the data. If you started without data.tar.gz, create the first login with:"
echo "   sudo -u desk /opt/technofunda/.venv/bin/python -m backend.auth add YOURNAME --admin    (run in /opt/technofunda)"

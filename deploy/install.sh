#!/usr/bin/env bash
# Run from the uploaded project on the approved Debian 12 Compute Engine VM.
set -euo pipefail
: "${SUMMITAIR_HOSTNAME:?Set the DNS hostname pointing to this VM}"
[[ -f /etc/summitair.env ]] || { echo 'Install /etc/summitair.env (root-owned, mode 600) first.'; exit 1; }
[[ "$SUMMITAIR_HOSTNAME" =~ ^[a-zA-Z0-9.-]+$ ]] || exit 1
sudo apt-get update -qq
sudo apt-get install -y python3-venv caddy
id summitair >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/summitair --shell /usr/sbin/nologin summitair
sudo mkdir -p /opt/summitair
sudo cp -R app outbound config.py pyproject.toml /opt/summitair/
sudo python3 -m venv /opt/summitair/.venv
sudo /opt/summitair/.venv/bin/pip install /opt/summitair
sudo install -m 644 deploy/summitair.service /etc/systemd/system/summitair.service
sudo mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=SUMMITAIR_HOSTNAME=%s\n' "$SUMMITAIR_HOSTNAME" | sudo tee /etc/systemd/system/caddy.service.d/summitair.conf >/dev/null
sudo install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now summitair
sudo systemctl restart summitair caddy
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health

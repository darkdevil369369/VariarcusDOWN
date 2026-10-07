#!/usr/bin/env bash
# Install / update VariarcusDOWN as a systemd service on an Ubuntu server (e.g. AWS EC2).
#   bash deploy/install_server.sh          # run from inside the cloned repo
# Re-running it updates the code and restarts the service; the token is kept.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
PORT="${PORT:-8787}"
ENV_FILE="$APP_DIR/.env"

cd "$APP_DIR"
git pull --ff-only || true

if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" 2>/dev/null; then
  sudo apt-get update -y && sudo apt-get install -y python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Secret dashboard token (kept across re-installs)
if [ ! -f "$ENV_FILE" ] || ! grep -q '^VARIARCUS_TOKEN=' "$ENV_FILE"; then
  echo "VARIARCUS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
TOKEN="$(grep '^VARIARCUS_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"

# Server overrides: listen on all interfaces, never try to open a browser
if [ ! -f config.local.yaml ]; then
  cat > config.local.yaml <<YAML
dashboard:
  host: 0.0.0.0
  port: $PORT
  open_browser: false
YAML
fi

echo "Measuring RTT to Arcus from this server..."
.venv/bin/python tools/ping_arcus.py --write || echo "(RTT measure failed; keeping config value)"

sudo tee /etc/systemd/system/variarcus.service >/dev/null <<UNIT
[Unit]
Description=VariarcusDOWN lead-lag paper bot (Arcus) + dashboard :$PORT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -u run.py --no-browser
Restart=always
RestartSec=5
MemoryMax=400M

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now variarcus.service
sudo systemctl restart variarcus.service
sleep 4
sudo systemctl --no-pager --lines=8 status variarcus.service || true

IP="$(curl -s --max-time 3 http://checkip.amazonaws.com || hostname -I | awk '{print $1}')"
echo
echo "=================================================================="
echo " Dashboard:  http://$IP:$PORT/?token=$TOKEN"
echo " (AWS: open inbound TCP $PORT in the instance's Security Group,"
echo "  ideally only for your own IP.)"
echo " No port open? Use a tunnel from your laptop instead:"
echo "   ssh -L $PORT:localhost:$PORT $RUN_USER@$IP"
echo "   then open http://localhost:$PORT/?token=$TOKEN"
echo " Logs:  journalctl -u variarcus -f"
echo "=================================================================="

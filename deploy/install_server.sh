#!/usr/bin/env bash
# Install / update VariarcusDOWN on the bots server, the same way polypf15min is set up:
#   * systemd service `variarcus` (User=ubuntu, Restart=always, loads ~/.mirofish.env for Telegram)
#   * dashboard on 127.0.0.1:8798, published as https://variarcus.tryrealo.com
#     through the existing cloudflared tunnel (no AWS port to open)
#   * login = the same user:pass as edge.tryrealo.com (CROSSEDGE_DASH_AUTH in ~/.crossedge_dash.env)
#
#   bash ~/VariarcusDOWN/deploy/install_server.sh
# Re-running it updates the code and restarts the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
PORT="${PORT:-8798}"
HOST_NAME="${HOST_NAME:-variarcus.tryrealo.com}"
CF_CFG="$HOME/.cloudflared/config.yml"
ENV_FILE="$APP_DIR/.env"

cd "$APP_DIR"
git pull --ff-only || true

echo "== python env"
if ! python3 -c "import ensurepip" 2>/dev/null; then
  sudo apt-get update -y && sudo apt-get install -y python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== dashboard login"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
if grep -q '^CROSSEDGE_DASH_AUTH=' "$HOME/.crossedge_dash.env" 2>/dev/null; then
  LOGIN="same user/password as edge.tryrealo.com"
else
  grep -q '^VARIARCUS_TOKEN=' "$ENV_FILE" || \
    echo "VARIARCUS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" >> "$ENV_FILE"
  LOGIN="token link below"
fi
TOKEN="$(grep '^VARIARCUS_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"

if [ ! -f config.local.yaml ]; then
  cat > config.local.yaml <<YAML
dashboard:
  host: 127.0.0.1
  port: $PORT
  open_browser: false
YAML
fi

echo "== measuring RTT to Arcus from this server"
.venv/bin/python tools/ping_arcus.py --write || echo "(RTT measure failed; keeping config value)"

echo "== systemd service"
sudo tee /etc/systemd/system/variarcus.service >/dev/null <<UNIT
[Unit]
Description=VariarcusDOWN lead-lag paper bot (Arcus) + dashboard 127.0.0.1:$PORT ($HOST_NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-/home/ubuntu/.mirofish.env
EnvironmentFile=$ENV_FILE
Environment=MALLOC_ARENA_MAX=2
ExecStart=$APP_DIR/.venv/bin/python -u run.py --no-browser
Restart=always
RestartSec=5
MemoryHigh=300M
MemoryMax=450M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now variarcus.service
sudo systemctl restart variarcus.service

echo "== cloudflared route $HOST_NAME -> 127.0.0.1:$PORT"
URL="http://127.0.0.1:$PORT/"
CF_BIN="$(command -v cloudflared || echo "$HOME/cloudflared")"
if [ -f "$CF_CFG" ] && [ -x "$CF_BIN" ]; then
  if ! grep -q "hostname: $HOST_NAME" "$CF_CFG"; then
    cp "$CF_CFG" "$CF_CFG.bak_variarcus_$(date +%s)"
    python3 - "$CF_CFG" "$HOST_NAME" "$PORT" <<'PY'
import re, sys
cfg, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(cfg).read()
m = re.search(r"^(\s*)- service: http_status:404", s, re.M)
if not m:
    sys.exit("no catch-all '- service: http_status:404' in config.yml; add the route by hand")
ind = m.group(1)
s = s[:m.start()] + f"{ind}- hostname: {host}\n{ind}  service: http://127.0.0.1:{port}\n" + s[m.start():]
open(cfg, "w").write(s)
PY
    TUNNEL="$(awk '/^tunnel:/{print $2; exit}' "$CF_CFG")"
    "$CF_BIN" tunnel route dns "$TUNNEL" "$HOST_NAME" || true
    sudo systemctl restart cloudflared
  fi
  URL="https://$HOST_NAME/"
else
  echo "(no cloudflared config found; dashboard is only on $URL)"
fi

sleep 5
sudo systemctl --no-pager --lines=6 status variarcus.service || true
echo
echo "=================================================================="
if [ -n "$TOKEN" ]; then
  echo " Dashboard:  ${URL}?token=$TOKEN"
else
  echo " Dashboard:  $URL   (login: $LOGIN)"
fi
echo " Logs:   journalctl -u variarcus -f"
echo " Pause:  touch ~/.variarcus_STOP      Resume: rm ~/.variarcus_STOP"
echo "=================================================================="

#!/usr/bin/env bash
# One-shot deploy of the TradeOS AI Pump Reader on an Oracle Cloud Always-Free
# Ubuntu VM. Idempotent: safe to re-run (it pulls + restarts).
#
#   curl -fsSL https://raw.githubusercontent.com/KannieO2/Amatista-Trade-AI/main/deploy/oracle-setup.sh | bash
# or, after cloning:
#   bash deploy/oracle-setup.sh
#
# Override defaults with env vars, e.g.  PORT=8080 REPO_URL=... bash oracle-setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/KannieO2/Amatista-Trade-AI.git}"
APP_DIR="${APP_DIR:-$HOME/tradeos}"
PORT="${PORT:-8000}"
SERVICE="${SERVICE:-pumpreader}"
RUN_USER="$(whoami)"

echo "==> [1/6] System packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git curl

echo "==> [2/6] Clone or update repo at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> [3/6] Python venv + dependencies"
VENV="$APP_DIR/apps/pump-reader/.venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/apps/pump-reader/requirements.txt"

echo "==> [4/6] Environment file"
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$APP_DIR/.env.example" ]; then cp "$APP_DIR/.env.example" "$APP_DIR/.env"; else : > "$APP_DIR/.env"; fi
  echo "    Created $APP_DIR/.env — edit it: SUPABASE_URL, SUPABASE_SERVICE_KEY"
  echo "    (and exchange/Telegram keys only when you choose to go live)."
fi

echo "==> [5/6] Firewall: open TCP $PORT"
sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$PORT" -j ACCEPT || true
if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save || true; fi

echo "==> [6/6] systemd service '$SERVICE'"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=TradeOS AI Pump Reader
After=network.target

[Service]
WorkingDirectory=$APP_DIR/apps/pump-reader
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
User=$RUN_USER

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sleep 3
sudo systemctl --no-pager status "$SERVICE" | head -n 15 || true

IP="$(curl -fsS ifconfig.me 2>/dev/null || echo '<vm-public-ip>')"
echo
echo "================================================================"
echo " Pump Reader is running.  Open:  http://$IP:$PORT"
echo " Logs:     sudo journalctl -u $SERVICE -f"
echo " Restart:  sudo systemctl restart $SERVICE"
echo " Update:   bash $APP_DIR/deploy/update.sh"
echo "================================================================"

#!/usr/bin/env bash
# One-shot deploy of TradeOS AI (Pump Reader + embedded real GRVTBot) on an
# Oracle Cloud Always-Free Ubuntu VM. Idempotent: safe to re-run (pulls +
# rebuilds + restarts).
#
#   curl -fsSL https://raw.githubusercontent.com/KannieO2/Amatista-Trade-AI/main/deploy/oracle-setup.sh | bash
# or, after cloning:
#   bash deploy/oracle-setup.sh
#
# Architecture: two local processes on one host.
#   - uvicorn (FastAPI)  :8000  — the app + dashboard, the ONLY public port.
#   - node (GRVTBot)     :3848  — the real grid bot, localhost only, reached by
#                                 the FastAPI reverse proxy at /grid/*.
#
# Override defaults with env vars, e.g.  PORT=8080 REPO_URL=... bash oracle-setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/KannieO2/Amatista-Trade-AI.git}"
APP_DIR="${APP_DIR:-$HOME/tradeos}"
PORT="${PORT:-8000}"
SERVICE="${SERVICE:-pumpreader}"
GRID_SERVICE="${GRID_SERVICE:-grvtbot}"
# Real GRVTBot, pinned to the commit this app was built and verified against.
GRVT_REPO="${GRVT_REPO:-https://github.com/kmanus88/GRVTBot.git}"
GRVT_REF="${GRVT_REF:-d5587adc88def401d789fce1b1a75db51432003b}"
GRID_OWNER_EMAIL="${GRID_OWNER_EMAIL:-admin@tradeos.local}"
GRID_OWNER_PASSWORD="${GRID_OWNER_PASSWORD:-tradeos2026}"
RUN_USER="$(whoami)"

echo "==> [1/8] System packages (python, git, node 22)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git curl build-essential openssl
if ! command -v node >/dev/null 2>&1 || [ "$(node -v 2>/dev/null | sed 's/v//; s/\..*//')" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
echo "    node $(node -v)  npm $(npm -v)"

echo "==> [2/8] Clone or update app at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> [3/8] Python venv + dependencies"
VENV="$APP_DIR/apps/pump-reader/.venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/apps/pump-reader/requirements.txt"

echo "==> [4/8] App environment file ($APP_DIR/.env)"
ENV_FILE="$APP_DIR/.env"
touch "$ENV_FILE"
ensure_env() { grep -q "^$1=" "$ENV_FILE" || echo "$1=$2" >> "$ENV_FILE"; }
ensure_env APP_USERNAME admin
ensure_env APP_PASSWORD "$GRID_OWNER_PASSWORD"
ensure_env APP_SECRET_KEY "$(openssl rand -hex 32)"
ensure_env GRID_OWNER_EMAIL "$GRID_OWNER_EMAIL"
ensure_env GRID_OWNER_PASSWORD "$GRID_OWNER_PASSWORD"
echo "    (edit $ENV_FILE to add SUPABASE_URL / SUPABASE_SERVICE_KEY and, only"
echo "     when you choose to go live, exchange/Telegram keys.)"

echo "==> [5/8] Build the real GRVTBot (pinned $GRVT_REF)"
GRVT_DIR="$APP_DIR/external/GRVTBot"
if [ -d "$GRVT_DIR/.git" ]; then
  git -C "$GRVT_DIR" fetch origin
else
  git clone "$GRVT_REPO" "$GRVT_DIR"
fi
git -C "$GRVT_DIR" checkout -f "$GRVT_REF"
# Strip the upstream "GRVT referral" card from Settings (not wanted in this app).
perl -0pi -e "s{\s*<Card>\s*<h2[^>]*>\s*\{t\(.settings\.sectionReferral.\)\}.*?</Card>}{}s" \
  "$GRVT_DIR/packages/dashboard/src/pages/settings.tsx" || true
( cd "$GRVT_DIR" \
  && npm install \
  && npm run build --workspace=@grvt-grid/bot \
  && VITE_BASE_PATH=/grid/dashboard/ VITE_API_BASE_URL=/grid npm run build --workspace=@grvt-grid/dashboard )
[ -f "$GRVT_DIR/master.key" ] || node -e "require('fs').writeFileSync('$GRVT_DIR/master.key', require('crypto').randomBytes(32))"

echo "==> [6/8] GRVTBot environment file"
GENV="$GRVT_DIR/.env"
if [ ! -f "$GENV" ]; then
  cat > "$GENV" <<ENV
MOCK_MODE=true
DRY_RUN=true
ALLOW_EMBED=1
DISABLE_RATE_LIMIT=1
DASHBOARD_PORT=3848
LOG_LEVEL=info
JWT_SECRET=$(openssl rand -hex 32)
DASHBOARD_API_KEY=$(openssl rand -hex 24)
OWNER_EMAIL=$GRID_OWNER_EMAIL
OWNER_INITIAL_PASSWORD=$GRID_OWNER_PASSWORD
MASTER_KEY_PATH=$GRVT_DIR/master.key
DASHBOARD_V2_DIST=$GRVT_DIR/packages/dashboard/dist
GRVT_TRADING_ACCOUNT_ID=mock-account
GRVT_ACCOUNT_ID=mock-account
GRVT_API_KEY=mock-key
GRVT_API_SECRET=mock-secret
GRVT_TRADING_ADDRESS=0x0000000000000000000000000000000000000000
ENV
  echo "    Created $GENV in MOCK mode. For real GRVT money: set your GRVT keys"
  echo "    and remove MOCK_MODE / DRY_RUN."
fi

echo "==> [7/8] Firewall: open TCP $PORT (only; 3848 stays localhost)"
sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$PORT" -j ACCEPT || true
if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save || true; fi

echo "==> [8/8] systemd services '$GRID_SERVICE' + '$SERVICE'"
sudo tee "/etc/systemd/system/$GRID_SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=GRVTBot (real grid bot, localhost :3848)
After=network.target

[Service]
WorkingDirectory=$GRVT_DIR
EnvironmentFile=$GENV
ExecStart=/usr/bin/node packages/bot/dist/dashboard/server.js
Restart=always
RestartSec=10
User=$RUN_USER

[Install]
WantedBy=multi-user.target
UNIT

sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=TradeOS AI Pump Reader
After=network.target $GRID_SERVICE.service
Wants=$GRID_SERVICE.service
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
WorkingDirectory=$APP_DIR/apps/pump-reader
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
User=$RUN_USER
SyslogIdentifier=$SERVICE

[Install]
WantedBy=multi-user.target
UNIT

echo "==> [8b/8] Log rotation (journald size caps) + hourly health-check cron"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp "$APP_DIR/deploy/journald-tradeos.conf" /etc/systemd/journald.conf.d/tradeos.conf
sudo systemctl restart systemd-journald || true
# Scripts must be executable + readable by the run user.
chmod +x "$APP_DIR/deploy/"*.sh "$APP_DIR/deploy/health_check.py" 2>/dev/null || true
# Hourly watchdog: restart the service if the API is down AND logs are stale.
sudo touch /var/log/tradeos-health.log
sudo chown "$RUN_USER" /var/log/tradeos-health.log || true
CRON_LINE="0 * * * * /usr/bin/python3 $APP_DIR/deploy/health_check.py >> /var/log/tradeos-health.log 2>&1"
( sudo crontab -l 2>/dev/null | grep -v health_check.py; echo "$CRON_LINE" ) | sudo crontab -

sudo systemctl daemon-reload
sudo systemctl enable --now "$GRID_SERVICE"
sudo systemctl enable --now "$SERVICE"
sleep 4
sudo systemctl --no-pager status "$GRID_SERVICE" | head -n 8 || true
sudo systemctl --no-pager status "$SERVICE" | head -n 12 || true

IP="$(curl -fsS ifconfig.me 2>/dev/null || echo '<vm-public-ip>')"
echo
echo "================================================================"
echo " TradeOS AI is running.  Open:  http://$IP:$PORT"
echo " Login: admin / $GRID_OWNER_PASSWORD   (change APP_PASSWORD in $ENV_FILE)"
echo " Grid bot tab embeds the real GRVTBot (single sign-on, no 2nd login)."
echo " Logs:     sudo journalctl -u $SERVICE -f   |   -u $GRID_SERVICE -f"
echo " Restart:  sudo systemctl restart $SERVICE $GRID_SERVICE"
echo " Update:   bash $APP_DIR/deploy/update.sh"
echo "================================================================"

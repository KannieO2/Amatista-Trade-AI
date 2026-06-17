#!/usr/bin/env bash
# Pull latest code, refresh deps, restart the service. Run on the VM after a push.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/tradeos}"
SERVICE="${SERVICE:-pumpreader}"
git -C "$APP_DIR" pull --ff-only
"$APP_DIR/apps/pump-reader/.venv/bin/pip" install -r "$APP_DIR/apps/pump-reader/requirements.txt"
sudo systemctl restart "$SERVICE"
sudo systemctl --no-pager status "$SERVICE" | head -n 12

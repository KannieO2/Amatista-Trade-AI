#!/usr/bin/env bash
# Pull latest code, refresh deps, rebuild the embedded GRVTBot, restart both
# services. Run on the VM after a push.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/tradeos}"
SERVICE="${SERVICE:-pumpreader}"
GRID_SERVICE="${GRID_SERVICE:-grvtbot}"
GRVT_REF="${GRVT_REF:-d5587adc88def401d789fce1b1a75db51432003b}"
GRVT_DIR="$APP_DIR/external/GRVTBot"

git -C "$APP_DIR" pull --ff-only
"$APP_DIR/apps/pump-reader/.venv/bin/pip" install -r "$APP_DIR/apps/pump-reader/requirements.txt"

# Rebuild GRVTBot only if it's present (it lives outside the repo).
if [ -d "$GRVT_DIR/.git" ]; then
  git -C "$GRVT_DIR" fetch origin && git -C "$GRVT_DIR" checkout -f "$GRVT_REF"
  ( cd "$GRVT_DIR" \
    && npm install \
    && npm run build --workspace=@grvt-grid/bot \
    && VITE_BASE_PATH=/grid/dashboard/ VITE_API_BASE_URL=/grid npm run build --workspace=@grvt-grid/dashboard )
  sudo systemctl restart "$GRID_SERVICE"
fi

sudo systemctl restart "$SERVICE"
sudo systemctl --no-pager status "$SERVICE" | head -n 12

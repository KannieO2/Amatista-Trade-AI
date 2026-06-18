#!/usr/bin/env python3
"""TradeOS AI health-check watchdog (run hourly from cron, as root).

Healthy if EITHER is true:
  - the API answers GET /health with 200, or
  - the systemd journal for the service has a line newer than MAX_LOG_AGE.

Otherwise: `systemctl restart <service>` and (best-effort) ping Telegram.

Stdlib only — no venv required. Reads TELEGRAM_* from the app .env so the alert
goes to the same chat as the bot. Designed to be idempotent and quiet on success.

Install (root crontab, every hour):
  0 * * * * /usr/bin/python3 /home/ubuntu/tradeos/deploy/health_check.py >> /var/log/tradeos-health.log 2>&1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SERVICE = os.getenv("HEALTH_SERVICE", "pumpreader")
HEALTH_URL = os.getenv("HEALTH_URL", "http://127.0.0.1:8000/health")
MAX_LOG_AGE = int(os.getenv("HEALTH_MAX_LOG_AGE", "300"))   # 5 min
ENV_PATH = Path(os.getenv("TRADEOS_ENV", str(Path.home() / "tradeos" / ".env")))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def api_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("status") == "ok"
    except Exception:
        return False


def logs_fresh() -> bool:
    """True if the service journal has a line within MAX_LOG_AGE seconds."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", SERVICE, "-n", "1", "-o", "short-unix", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not out:
            return False
        epoch = float(out.split()[0])  # leading unix timestamp
        return (time.time() - epoch) < MAX_LOG_AGE
    except Exception:
        return False


def restart() -> bool:
    try:
        r = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def telegram(text: str) -> None:
    """Best-effort alert using TELEGRAM_* from the app .env. Errors are ignored."""
    token = chat = None
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_ERROR_CHAT_ID="):
                chat = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_CHAT_ID=") and not chat:
                chat = line.split("=", 1)[1].strip()
    except Exception:
        return
    if not token or not chat:
        return
    try:
        data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def main() -> int:
    if api_ok():
        print(f"[{_now()}] OK · API responde")
        return 0
    if logs_fresh():
        print(f"[{_now()}] OK · logs frescos (<{MAX_LOG_AGE}s), API lenta pero vivo")
        return 0
    print(f"[{_now()}] CAÍDO · API no responde y logs viejos → reiniciando {SERVICE}")
    ok = restart()
    telegram(
        f"🩺 <b>Health-check</b>\nServicio <b>{SERVICE}</b> no respondía.\n"
        f"Reinicio {'✅ exitoso' if ok else '❌ FALLÓ'}."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

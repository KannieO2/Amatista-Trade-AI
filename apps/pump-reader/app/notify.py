"""Telegram alerting. No-op (logs only) when no bot token is configured."""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("pump-reader.notify")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("telegram disabled (no token); message: %s", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the engine
        logger.warning("telegram send failed: %s", exc)
        return False


def format_alert(symbol: str, pump_score: int, classification: str, flags: list[str]) -> str:
    flag_text = ", ".join(flags) if flags else "none"
    return (
        f"🚨 <b>Pump signal</b> {symbol}\n"
        f"score: <b>{pump_score}</b> | type: {classification}\n"
        f"flags: {flag_text}"
    )

"""Telegram alerting + runtime config. No-op (logs only) when unconfigured.

Token/chat can come from env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) or be set
live from the Settings UI (/telegram/config), which also persists them to .env.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("pump-reader.notify")

# repo-root .env (app/ -> pump-reader/ -> apps/ -> repo root)
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

# Runtime overrides set via the Settings UI; fall back to env vars.
_token_override: str | None = None
_chat_override: str | None = None


def _token() -> str:
    return (_token_override or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _chat() -> str:
    return (_chat_override or os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def configure(token: str | None = None, chat_id: str | None = None) -> None:
    global _token_override, _chat_override
    if token is not None:
        _token_override = token.strip()
    if chat_id is not None:
        _chat_override = str(chat_id).strip()


def status() -> dict:
    tok = _token()
    return {
        "configured": bool(tok and _chat()),
        "has_token": bool(tok),
        "chat_id": _chat(),
        "token_hint": (tok[:10] + "…") if tok else "",
    }


def persist_env() -> bool:
    """Best-effort upsert of the two keys into the repo-root .env (survives restart)."""
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.exists() else []
        vals = {"TELEGRAM_BOT_TOKEN": _token(), "TELEGRAM_CHAT_ID": _chat()}
        seen: set[str] = set()
        out: list[str] = []
        for ln in lines:
            key = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else ""
            if key in vals:
                out.append(f"{key}={vals[key]}")
                seen.add(key)
            else:
                out.append(ln)
        for k, v in vals.items():
            if k not in seen:
                out.append(f"{k}={v}")
        _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram persist failed: %s", exc)
        return False


async def send_telegram(text: str) -> bool:
    token, chat = _token(), _chat()
    if not token or not chat:
        logger.info("telegram disabled (no token/chat); message: %s", text)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            )
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the engine
        logger.warning("telegram send failed: %s", exc)
        return False


async def get_updates() -> dict:
    """getUpdates so the user can find their group chat id (after adding the bot
    to the group and sending any message there)."""
    token = _token()
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            data = (await client.get(f"https://api.telegram.org/bot{token}/getUpdates")).json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("description", "telegram error")}
    chats: dict = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None:
            chats[cid] = {
                "id": cid,
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
            }
    return {"ok": True, "chats": list(chats.values())}


async def send_test() -> bool:
    return await send_telegram(
        "✅ <b>TradeOS AI</b> conectado a este grupo. Aquí recibirás las alertas de pump en tiempo real."
    )


def format_alert(symbol: str, pump_score: int, classification: str, flags: list[str]) -> str:
    flag_text = ", ".join(flags) if flags else "none"
    return (
        f"🚨 <b>Pump signal</b> {symbol}\n"
        f"score: <b>{pump_score}</b> | type: {classification}\n"
        f"flags: {flag_text}"
    )

"""Telegram alerting + runtime config. No-op (logs only) when unconfigured.

Token/chat can come from env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) or be set
live from the Settings UI (/telegram/config), which also persists them to .env.
"""

from __future__ import annotations

import html
import logging
import os
import time
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


def _error_chat() -> str:
    """Optional separate chat/group for error + system alerts (same bot, distinct
    group). Falls back to the main chat when TELEGRAM_ERROR_CHAT_ID is unset."""
    return (os.getenv("TELEGRAM_ERROR_CHAT_ID") or "").strip() or _chat()


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


async def send_telegram(text: str, parse_mode: str | None = "HTML",
                        chat_id: str | None = None) -> bool:
    token = _token()
    chat = (chat_id or _chat()).strip()
    if not token or not chat:
        logger.info("telegram disabled (no token/chat); message: %s", text)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=payload)
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


# --- pump-alert QUALITY GATE (cuts noise) -------------------------------------
# Solo notifica señales que cruzan TODOS los pisos, clasificadas ALTA/MEDIA, y
# como máximo 1 vez por token cada cooldown (anti-spam / anti-repetido).
ALERT_MIN_SCORE = float(os.getenv("PUMP_ALERT_MIN_SCORE", "65"))
ALERT_MIN_CONFIDENCE = float(os.getenv("PUMP_ALERT_MIN_CONFIDENCE", "50"))
ALERT_MIN_VOL_SPIKE = float(os.getenv("PUMP_ALERT_MIN_VOL_SPIKE", "3.0"))
ALERT_MIN_LIQUIDITY = float(os.getenv("PUMP_ALERT_MIN_LIQUIDITY", "50000"))
ALERT_COOLDOWN_S = int(os.getenv("PUMP_ALERT_COOLDOWN_SECONDS", "1800"))  # 30 min


def alert_importance(score: float, confidence: float, vol_spike: float,
                     liquidity: float) -> str:
    """ALTA / MEDIA / BAJA. Solo ALTA y MEDIA se notifican."""
    if (score >= ALERT_MIN_SCORE and confidence >= ALERT_MIN_CONFIDENCE
            and vol_spike >= ALERT_MIN_VOL_SPIKE and liquidity >= ALERT_MIN_LIQUIDITY):
        if score >= 80 and confidence >= 65 and vol_spike >= 6 and liquidity >= 100_000:
            return "ALTA"
        return "MEDIA"
    return "BAJA"


class AlertGate:
    """Decide si una señal de pump merece notificación de Telegram. Corta calidad
    baja (no cruza pisos), repetidos y tokens ya avisados dentro del cooldown."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def evaluate(self, key: str, *, score: float, confidence: float,
                 vol_spike: float, liquidity: float) -> tuple[bool, str, str]:
        """Devuelve (enviar, importancia, motivo)."""
        imp = alert_importance(score, confidence, vol_spike, liquidity)
        if imp == "BAJA":
            return False, imp, "calidad baja (no cruza pisos)"
        now = time.time()
        last = self._last.get(key)
        if last is not None and (now - last) < ALERT_COOLDOWN_S:
            return False, imp, f"cooldown {int((now - last) / 60)}min<{ALERT_COOLDOWN_S // 60}min"
        self._last[key] = now           # primera vez / cooldown vencido -> notificar
        return True, imp, "ok"


alert_gate = AlertGate()


# --- GRID summaries + dedup ----------------------------------------------------
GRID_PNL_DELTA_USD = float(os.getenv("PUMP_GRID_PNL_DELTA_USD", "5"))
_last_grid_equity: float | None = None


def grid_summary_changed(equity: float) -> bool:
    """True si vale la pena reenviar el resumen (equity cambió >= umbral). Silencia
    resúmenes idénticos cuando el grid no se movió."""
    global _last_grid_equity
    if _last_grid_equity is None or abs(equity - _last_grid_equity) >= GRID_PNL_DELTA_USD:
        _last_grid_equity = equity
        return True
    return False


def format_grid_summary(s: dict) -> str:
    state = "🟢 Activo" if s.get("running") else "⚪ Inactivo"
    rp = s.get("realized_pnl", 0.0) or 0.0
    up = s.get("unrealized_pnl", 0.0) or 0.0
    tot = rp + up
    eq = s.get("equity", 0.0) or 0.0
    cap = s.get("capital", 0.0) or 0.0
    slots = s.get("active_slots", 0)
    levels = s.get("grid_levels", 0)
    emoji = "📈" if tot >= 0 else "📉"
    return (
        f"<b>[GRID]</b> 📊 <b>Resumen del Grid Bot</b>\n"
        f"\n"
        f"🔹 <b>Par:</b> {s.get('pair', '—')}  ·  {state}\n"
        f"🟩 <b>Slots activos:</b> {slots} / {max(levels - 1, 0)}\n"
        f"💵 <b>PnL realizado:</b> {rp:+.2f} USD\n"
        f"📐 <b>PnL no realizado:</b> {up:+.2f} USD\n"
        f"{emoji} <b>PnL total:</b> {tot:+.2f} USD\n"
        f"🏦 <b>Equity:</b> ${eq:,.2f}  (capital ${cap:,.0f})"
    )


def format_grid_fill(pair: str, side: str, price: float, qty: float, pnl: float) -> str:
    arrow = "🟢 COMPRA" if side == "buy" else "🔴 VENTA"
    pnl_txt = f"  ·  PnL {pnl:+.2f} USD" if side == "sell" else ""
    return f"<b>[GRID]</b> {arrow}  ·  {pair} @ {price:g}  ·  {qty:g}{pnl_txt}"


# --- system / error / grid alerts (distinct headers so they stand out) --------

ERROR_THROTTLE_S = int(os.getenv("PUMP_ERROR_THROTTLE_S", "600"))  # same error max 1×/10min
_last_error_at: dict[str, float] = {}


async def send_system(text: str) -> bool:
    # System notices go to the error/system group when one is configured.
    return await send_telegram(
        f"⚙️ <b>TradeOS AI · Sistema</b>\n{text}", chat_id=_error_chat()
    )


async def send_error(where: str, detail: str) -> bool:
    """Big, unmistakable error alert in the error/system group. Throttled per
    (where+detail) so a looping failure doesn't flood the chat."""
    key = f"{where}:{detail[:80]}"
    now = time.time()
    if now - _last_error_at.get(key, 0.0) < ERROR_THROTTLE_S:
        return False
    _last_error_at[key] = now
    body = html.escape(detail[:500])
    text = (
        "🟥🟥🟥  <b>ERROR · TradeOS AI</b>  🟥🟥🟥\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 <b>{html.escape(where)}</b>\n"
        f"<code>{body}</code>"
    )
    return await send_telegram(text, chat_id=_error_chat())


async def send_grid(text: str) -> bool:
    return await send_telegram(f"📊 <b>Grid Bot</b>\n{text}")


def send_error_sync(where: str, detail: str) -> bool:
    """Blocking Telegram send for a FATAL crash — the async event loop is already
    dead at that point, so this uses a synchronous client. Best-effort, never
    raises (we're already crashing)."""
    token, chat = _token(), _error_chat()
    if not token or not chat:
        return False
    body = html.escape(detail[-1500:])  # tail of the traceback (most relevant)
    text = (
        "🟥🟥🟥  <b>CRASH FATAL · TradeOS AI</b>  🟥🟥🟥\n"
        f"📍 <b>{html.escape(where)}</b>\n"
        "El proceso murió. systemd lo reiniciará en ~10s.\n"
        f"<pre>{body}</pre>"
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=8,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# --- trade cards (Spanish, HTML formatted: bold labels, clean spacing) --------

def format_alert(symbol: str, pump_score: int, classification: str, flags: list[str],
                 cluster: str = "", exchange: str = "", liquidity_usd: float = 0.0,
                 importance: str = "") -> str:
    """A detected pump SIGNAL (not yet an entry)."""
    flag_text = ", ".join(flags) if flags else "ninguna"
    crit = cluster.upper().replace("_", " ") if cluster else "—"
    liq = f"${liquidity_usd:,.0f}" if liquidity_usd else "—"
    venue = exchange.title() if exchange else "—"
    imp = f"  ·  <b>{importance}</b>" if importance else ""
    return (
        f"<b>[PUMP]</b> 🔔 <b>SEÑAL DE PUMP</b>  ·  {symbol}{imp}\n"
        f"\n"
        f"🎯 <b>Score:</b> {pump_score}\n"
        f"📊 <b>Tipo:</b> {classification.upper()}\n"
        f"🧭 <b>Criterio:</b> {crit}\n"
        f"💧 <b>Liquidez:</b> {liq}\n"
        f"🏦 <b>Exchange:</b> {venue}\n"
        f"🚩 <b>Flags:</b> {flag_text}"
    )


def format_entry(symbol: str, exchange: str, price: float, accel: float, score: int,
                 classification: str, flags: list[str], dump_pct: float,
                 timeout_min: float, be_pct: float) -> str:
    """New position opened — with the active protections spelled out."""
    flag_text = ", ".join(flags) if flags else "ninguna"
    return (
        f"<b>[PUMP]</b> 🚀 <b>NUEVA OPERACIÓN</b>  ·  {symbol}\n"
        f"\n"
        f"🔹 <b>Par:</b> {symbol} ({exchange.title()})\n"
        f"💰 <b>Entrada:</b> {price:g}\n"
        f"⚡ <b>Aceleración:</b> {accel:.1f}x\n"
        f"🎯 <b>Score:</b> {score}\n"
        f"📊 <b>Tipo:</b> {classification.upper()}\n"
        f"🚩 <b>Flags:</b> {flag_text}\n"
        f"\n"
        f"🛡️ <b>Protecciones activas</b>\n"
        f"💥 Panic sell  ·  dump {dump_pct:g}x\n"
        f"⏳ Time-out  ·  {timeout_min:g}m lateral\n"
        f"📈 Break-even  ·  +{be_pct:g}%"
    )


_CAUSA_ES = {
    "hard_stop": "HARD_STOP",
    "dump": "PANIC_SELL",
    "trailing": "TRAILING_STOP",
    "timeout": "TIME_OUT",
    "break_even": "BREAK_EVEN",
    "tp1": "TAKE_PROFIT",
}


def format_exit(symbol: str, exchange: str, price: float, pnl_pct: float,
                cause: str, note: str = "") -> str:
    """Full position close card."""
    causa = _CAUSA_ES.get(cause, cause.upper())
    emoji = "📈" if pnl_pct >= 0 else "📉"
    nota = f"\n📋 <b>Nota:</b> {note}" if note else ""
    return (
        f"<b>[PUMP]</b> 🛑 <b>CIERRE DE POSICIÓN</b>  ·  {symbol}\n"
        f"\n"
        f"🔹 <b>Par:</b> {symbol} ({exchange.title()})\n"
        f"💸 <b>Salida:</b> {price:g}\n"
        f"{emoji} <b>PnL:</b> {pnl_pct:+.2f}%\n"
        f"⚠️ <b>Causa:</b> {causa}"
        f"{nota}"
    )


def format_partial(symbol: str, exchange: str, price: float, pct: int,
                   pnl_usd: float, cause: str) -> str:
    """Partial take-profit (keeps the rest running)."""
    causa = _CAUSA_ES.get(cause, cause.upper())
    return (
        f"<b>[PUMP]</b> 💰 <b>VENTA PARCIAL</b>  ·  {symbol} ({exchange.title()})\n"
        f"{causa}  ·  {pct}% @ {price:g}  ·  PnL {pnl_usd:+.2f} USD"
    )


def format_arbitrage(symbol: str, lo_ex: str, lo_px: float, hi_ex: str,
                     hi_px: float, spread_pct: float) -> str:
    return (
        f"🔀 <b>ARBITRAJE DETECTADO</b>  ·  {symbol}\n"
        f"\n"
        f"🟢 <b>Compra:</b> {lo_ex.title()} @ {lo_px:g}\n"
        f"🔴 <b>Venta:</b> {hi_ex.title()} @ {hi_px:g}\n"
        f"📏 <b>Spread:</b> {spread_pct:.2f}%\n"
        f"\n"
        f"ℹ️ <i>Solo detección (paper) — ejecutar requiere fondos en ambos exchanges</i>"
    )


# Convenience senders (HTML formatting).

async def send_entry(text: str) -> bool:
    return await send_telegram(text)


async def send_exit(text: str) -> bool:
    return await send_telegram(text)


async def send_alert(text: str) -> bool:
    return await send_telegram(text)


async def send_arbitrage(symbol: str, lo_ex: str, lo_px: float, hi_ex: str,
                         hi_px: float, spread_pct: float) -> bool:
    return await send_telegram(
        format_arbitrage(symbol, lo_ex, lo_px, hi_ex, hi_px, spread_pct)
    )

"""Unified market-data access: WebSocket-first price, persistent CCXT pool.

The exit engine used to read prices via grid.fetch_price, which (a) ignored the
live WebSocket feed and (b) created a brand-new ccxt client — full TLS handshake —
on EVERY call. This module fixes both without touching any strategy logic:

  get_price()      -> WebSocket cache first (sub-second, fresh), REST only as
                      fallback. Returns (price, source) so callers can log latency.
  get_1m_volume()  -> last closed 1m base volume (volume-aware time-stop fuel).

Both use a PERSISTENT per-exchange ccxt client (markets + HTTP session reused,
closed only on shutdown). Fully fail-safe: any error returns 0.0 / None and never
propagates into a trading loop. Pure execution-quality layer — it changes the
DATA SOURCE, never a trading decision.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import ccxt.async_support as ccxt

from .websocket_manager import get_manager

logger = logging.getLogger("pump-reader.marketdata")

# A WS price older than this is considered stale -> fall back to REST. Keeps the
# exit engine from ever acting on a frozen feed (dead socket).
WS_MAX_AGE_S = float(os.getenv("PUMP_WS_PRICE_MAX_AGE_SECONDS", "12"))

# --- persistent ccxt client pool --------------------------------------------
_clients: dict[str, ccxt.Exchange] = {}
_pool_lock = asyncio.Lock()

# --- lightweight telemetry (data-source hit rates) --------------------------
_stats = {"ws_hits": 0, "rest_hits": 0, "rest_calls": 0, "errors": 0}


async def _get_client(exchange_id: str) -> ccxt.Exchange | None:
    """Return a reused ccxt client for the exchange (created + load_markets once).
    Single event loop, so the only race is first-creation -> guarded by a lock."""
    eid = (exchange_id or "binance").lower()
    cli = _clients.get(eid)
    if cli is not None:
        return cli
    async with _pool_lock:
        cli = _clients.get(eid)
        if cli is not None:
            return cli
        if not hasattr(ccxt, eid):
            return None
        cli = getattr(ccxt, eid)({"enableRateLimit": True})
        try:
            await cli.load_markets()
        except Exception:
            logger.debug("load_markets %s failed (continuing without cache)", eid)
        _clients[eid] = cli
        return cli


def ws_price(symbol: str, exchange: str) -> float | None:
    """Fresh WebSocket price (None if absent or stale)."""
    try:
        return get_manager().get_price_fresh(exchange, symbol, WS_MAX_AGE_S)
    except Exception:
        return None


def ws_age_ms(symbol: str, exchange: str) -> float | None:
    """Age of the last WS price for this symbol, ms (None if never seen)."""
    try:
        mgr = get_manager()
        ts = mgr.price_ts.get(f"{exchange}:{symbol}")
        return round((time.monotonic() - ts) * 1000, 1) if ts else None
    except Exception:
        return None


async def get_price(symbol: str, exchange: str) -> tuple[float, str]:
    """Live price, WebSocket-first. Returns (price, source) where source is
    'ws' | 'rest' | 'none'. 0.0 price on failure (caller treats as no-signal)."""
    p = ws_price(symbol, exchange)
    if p and p > 0:
        _stats["ws_hits"] += 1
        return p, "ws"
    cli = await _get_client(exchange)
    if cli is None:
        return 0.0, "none"
    try:
        _stats["rest_calls"] += 1
        ticker = await cli.fetch_ticker(symbol.upper())
        px = float(ticker.get("last") or 0.0)
        if px > 0:
            _stats["rest_hits"] += 1
        return px, "rest"
    except Exception:
        _stats["errors"] += 1
        return 0.0, "rest"


async def get_1m_volume(symbol: str, exchange: str) -> float:
    """Base volume of the last CLOSED 1m candle (the previous, fully-formed one)."""
    cli = await _get_client(exchange)
    if cli is None:
        return 0.0
    try:
        candles = await cli.fetch_ohlcv(symbol.upper(), timeframe="1m", limit=3)
        if not candles or len(candles) < 2:
            return 0.0
        return float(candles[-2][5] or 0.0)
    except Exception:
        return 0.0


async def closes(symbol: str, exchange: str, timeframe: str = "1d", limit: int = 8) -> list[float]:
    """Closing prices for the last `limit` candles (regime detection fuel).
    Uses the pooled client; returns [] on any failure (fail-safe)."""
    cli = await _get_client(exchange)
    if cli is None:
        return []
    try:
        candles = await cli.fetch_ohlcv(symbol.upper(), timeframe=timeframe, limit=limit)
        return [float(c[4]) for c in candles if c and c[4]]
    except Exception:
        return []


def stats() -> dict:
    total = _stats["ws_hits"] + _stats["rest_calls"]
    return {
        **_stats,
        "pooled_clients": sorted(_clients.keys()),
        "ws_hit_rate": round(_stats["ws_hits"] / total, 3) if total else None,
    }


async def close_all() -> None:
    """Close every pooled client. Called once on shutdown."""
    for eid, cli in list(_clients.items()):
        try:
            await cli.close()
        except Exception:
            pass
    _clients.clear()

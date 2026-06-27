"""Cross-exchange lead-lag — free pump lead via leader venues (no key, CCXT public).

Criminal pumps often ignite FIRST on a thin leader venue (Gate, LBank, MEXC) before
the token moves on the main book. We pull the SAME token's last price on a few leader
exchanges and measure how far the best leader has run ABOVE our venue's price = a still-
open lead-lag divergence (arbitrage hasn't closed it yet). A leader trading meaningfully
above our venue = the pump started elsewhere → the laggard is the entry.

Cost control: fetch_tickers once per leader per refresh (cached), then look up each
symbol in memory — not one HTTP call per token. Best-effort: {} / None on any failure,
never fabricates.
"""

from __future__ import annotations

import os
import time

_LEADERS = [e.strip().lower() for e in
            os.getenv("PUMP_LEADLAG_EXCHANGES", "gate,lbank,mexc,bitget").split(",") if e.strip()]
_MIN_DIVERGENCE_PCT = float(os.getenv("PUMP_LEADLAG_MIN_PCT", "1.5"))   # leader ahead by >=this%
_TICKERS_TTL = int(os.getenv("PUMP_LEADLAG_TICKERS_TTL", "90"))

_clients: dict[str, object] = {}
_tickers: dict[str, tuple[float, dict]] = {}   # exchange -> (ts, {symbol: last_price})


async def _client(exchange_id: str):
    c = _clients.get(exchange_id)
    if c is not None:
        return c
    try:
        import ccxt.async_support as ccxt
        if not hasattr(ccxt, exchange_id):
            return None
        c = getattr(ccxt, exchange_id)({"enableRateLimit": True,
                                        "options": {"defaultType": "spot"}})
        _clients[exchange_id] = c
        return c
    except Exception:
        return None


async def _leader_tickers(exchange_id: str) -> dict:
    """{ 'BASE/USDT': last_price } for one leader, cached. {} on failure."""
    cached = _tickers.get(exchange_id)
    if cached and time.time() - cached[0] < _TICKERS_TTL:
        return cached[1]
    c = await _client(exchange_id)
    if c is None:
        return {}
    out: dict[str, float] = {}
    try:
        raw = await c.fetch_tickers()
        for sym, t in (raw or {}).items():
            last = (t or {}).get("last")
            if last:
                out[sym] = float(last)
    except Exception:
        return _tickers.get(exchange_id, (0, {}))[1]
    if out:
        _tickers[exchange_id] = (time.time(), out)
    return out


async def lead_divergence(base_symbol: str, our_exchange: str, our_price: float) -> dict | None:
    """Best leader-venue divergence ABOVE our price, or None.

    Returns {leader, leader_price, divergence_pct} when some leader (other than our own
    venue) trades >= _MIN_DIVERGENCE_PCT above our_price — a fresh, still-open lead."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base or not our_price or our_price <= 0:
        return None
    sym = f"{base}/USDT"
    best = None
    for ex in _LEADERS:
        if ex == (our_exchange or "").lower():
            continue
        last = (await _leader_tickers(ex)).get(sym)
        if not last or last <= 0:
            continue
        div = (last - our_price) / our_price * 100.0
        if div >= _MIN_DIVERGENCE_PCT and (best is None or div > best["divergence_pct"]):
            best = {"leader": ex, "leader_price": round(last, 10), "divergence_pct": round(div, 2)}
    return best


def lead_heat(div: dict | None) -> float:
    """0-30 heat from a lead-lag divergence (capped). None -> 0."""
    if not div:
        return 0.0
    # +10 at the threshold, scaling to +30 at ~5% ahead.
    return float(min(30.0, 10.0 + (div["divergence_pct"] - _MIN_DIVERGENCE_PCT) * 6.0))

"""Derivatives lead — funding-rate + open-interest surge (free, no key, CCXT public).

For a token that HAS a perp market, aggressive long leverage piling in (funding rate
spiking positive + open interest surging vs its recent level) often precedes or confirms
a spot pump. Most criminal-pump microcaps have NO perp -> None (the signal simply does
not apply and never blocks). Uses CCXT public swap endpoints.

Best-effort: None on any failure. Never fabricates. OI delta needs a baseline, kept in a
small in-memory cache per symbol.
"""

from __future__ import annotations

import os
import time

_PERP_EXCHANGES = [e.strip().lower() for e in
                   os.getenv("PUMP_DERIV_EXCHANGES", "binance,bybit").split(",") if e.strip()]
_FUNDING_HOT = float(os.getenv("PUMP_DERIV_FUNDING_HOT", "0.0005"))   # >=0.05%/8h = longs paying up
_OI_SURGE_PCT = float(os.getenv("PUMP_DERIV_OI_SURGE_PCT", "8"))      # OI up >=this% vs last sample

_clients: dict[str, object] = {}
_oi_hist: dict[str, tuple[float, float]] = {}   # 'ex:SYM' -> (ts, open_interest)
_OI_TTL = 1800


async def _client(exchange_id: str):
    c = _clients.get(exchange_id)
    if c is not None:
        return c
    try:
        import ccxt.async_support as ccxt
        if not hasattr(ccxt, exchange_id):
            return None
        c = getattr(ccxt, exchange_id)({"enableRateLimit": True,
                                        "options": {"defaultType": "swap"}})
        _clients[exchange_id] = c
        return c
    except Exception:
        return None


async def deriv_signal(base_symbol: str) -> dict | None:
    """{exchange, funding_rate, oi_change_pct} for the first perp venue that has this
    token, or None if no perp exists / all fail."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base:
        return None
    sym = f"{base}/USDT:USDT"   # CCXT linear-perp symbol
    for ex in _PERP_EXCHANGES:
        c = await _client(ex)
        if c is None:
            continue
        funding = None
        oi_change = None
        try:
            fr = await c.fetch_funding_rate(sym)
            funding = float((fr or {}).get("fundingRate") or 0.0)
        except Exception:
            continue   # no perp for this token on this venue -> try next
        try:
            oi = await c.fetch_open_interest(sym)
            oi_val = float((oi or {}).get("openInterestValue")
                           or (oi or {}).get("openInterestAmount") or 0.0)
            key = f"{ex}:{sym}"
            prev = _oi_hist.get(key)
            if prev and prev[1] > 0 and time.time() - prev[0] < _OI_TTL:
                oi_change = (oi_val - prev[1]) / prev[1] * 100.0
            if oi_val > 0:
                _oi_hist[key] = (time.time(), oi_val)
        except Exception:
            oi_change = None
        return {"exchange": ex, "funding_rate": round(funding, 6),
                "oi_change_pct": round(oi_change, 2) if oi_change is not None else None}
    return None


def deriv_heat(sig: dict | None) -> float:
    """0-20 heat from funding + OI surge (capped). None -> 0."""
    if not sig:
        return 0.0
    h = 0.0
    fr = sig.get("funding_rate") or 0.0
    if fr >= _FUNDING_HOT:
        h += min(12.0, (fr / _FUNDING_HOT) * 6.0)   # longs paying = bullish lean
    oic = sig.get("oi_change_pct")
    if oic is not None and oic >= _OI_SURGE_PCT:
        h += 8.0                                     # leverage piling in
    return float(min(20.0, h))

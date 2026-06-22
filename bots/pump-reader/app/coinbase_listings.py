"""Coinbase listing watcher — the 'Coinbase effect' pump catalyst (FREE, no key).

A token newly listed (or moving toward live trading) on Coinbase pumps hard across
EVERY exchange — one of the very few genuinely PREDICTIVE, free pump signals (a
public catalyst, not a guess about order-book accumulation). This polls Coinbase's
PUBLIC products API, diffs against the last-seen set, and surfaces:
  - new_listing : a base currency that wasn't on Coinbase before
  - went_live   : a known base that just flipped to live trading (the listing day)
Buy the token on the CEX where it ALREADY trades (MEXC/Binance USDT) the moment
Coinbase moves. Best-effort: returns {} on any failure, never fabricates.
"""

from __future__ import annotations

import os

import httpx

PRODUCTS_URL = os.getenv("COINBASE_PRODUCTS_URL", "https://api.exchange.coinbase.com/products")
_QUOTES = ("USD", "USDC")


async def fetch_products() -> dict[str, dict]:
    """{ base_currency: {status, auction_mode, trading_disabled} } for USD/USDC pairs."""
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "pump-reader"}) as c:
            r = await c.get(PRODUCTS_URL)
            data = r.json()
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for p in data:
        if not isinstance(p, dict) or p.get("quote_currency") not in _QUOTES:
            continue
        base = p.get("base_currency")
        if not base:
            continue
        info = {"status": p.get("status"),
                "auction_mode": bool(p.get("auction_mode")),
                "trading_disabled": bool(p.get("trading_disabled"))}
        # If a base has several quote pairs, prefer the "most live" record.
        prev = out.get(base)
        if prev is None or (prev.get("trading_disabled") and not info["trading_disabled"]):
            out[base] = info
    return out


def detect_events(prev: dict, curr: dict) -> list[dict]:
    """New bases, or a known base transitioning toward live trading = listing catalyst.
    Pure/testable. prev/curr are the maps from fetch_products()."""
    events: list[dict] = []
    for base, info in curr.items():
        old = prev.get(base)
        if old is None:
            events.append({"base": base, "kind": "new_listing", **info})
            continue
        went_live = ((old.get("trading_disabled") and not info.get("trading_disabled"))
                     or (old.get("status") != "online" and info.get("status") == "online")
                     or (old.get("auction_mode") and not info.get("auction_mode")))
        if went_live:
            events.append({"base": base, "kind": "went_live", **info})
    return events

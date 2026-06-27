"""Binance + Upbit listing watchers — more 'listing effect' catalysts (FREE, no key).

Same thesis as the Coinbase watcher: a token newly listed on a MAJOR, hard-to-list
venue pumps across every exchange. Binance and Upbit (the 'Korean premium' effect)
are the two strongest after Coinbase. We poll each venue's PUBLIC market list, diff
against the last-seen set, and surface new_listing events — then buy the token on the
CEX where it ALREADY trades (MEXC/Binance/Bitget USDT).

Reuses coinbase_listings.detect_events (pure base-set diff). Best-effort: every fetch
returns {} on ANY failure and never fabricates. The first poll only seeds state (no
events) because prev is empty — only listings that appear AFTER boot ever fire.
"""

from __future__ import annotations

import os

import httpx

BINANCE_URL = os.getenv("BINANCE_EXCHANGEINFO_URL", "https://api.binance.com/api/v3/exchangeInfo")
UPBIT_URL = os.getenv("UPBIT_MARKETS_URL", "https://api.upbit.com/v1/market/all")
_BINANCE_QUOTES = ("USDT", "FDUSD", "USDC")


async def fetch_binance() -> dict[str, dict]:
    """{ base_asset: {status} } for Binance spot pairs quoted in USDT/FDUSD/USDC.
    A base that wasn't here before = a fresh Binance listing (the catalyst)."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "pump-reader"}) as c:
            r = await c.get(BINANCE_URL)
            data = r.json()
    except Exception:
        return {}
    syms = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(syms, list):
        return {}
    out: dict[str, dict] = {}
    for s in syms:
        if not isinstance(s, dict) or s.get("quoteAsset") not in _BINANCE_QUOTES:
            continue
        base = s.get("baseAsset")
        if not base:
            continue
        info = {"status": s.get("status")}
        prev = out.get(base)
        # Prefer the "most live" record (TRADING beats anything else).
        if prev is None or (prev.get("status") != "TRADING" and info["status"] == "TRADING"):
            out[base] = info
    return out


async def fetch_upbit() -> dict[str, dict]:
    """{ base: {market} } for Upbit KRW markets — the 'Korean premium' listing effect.
    base = the token symbol of a KRW-quoted market (KRW-XXX → XXX)."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "pump-reader"}) as c:
            r = await c.get(UPBIT_URL)
            data = r.json()
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for m in data:
        if not isinstance(m, dict):
            continue
        market = m.get("market") or ""
        # Only KRW listings carry the Korean-premium catalyst.
        if not market.startswith("KRW-"):
            continue
        base = market.split("-", 1)[1].strip()
        if base:
            out[base] = {"market": market}
    return out

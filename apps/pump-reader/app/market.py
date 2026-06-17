"""CoinGecko market data (FDV / market cap / supply) — real, no API key.

Fills the FDV / MCap fields the orderbook can't give us. CoinGecko's free
`/coins/markets?symbols=` endpoint returns market cap, fully-diluted valuation
and supply by ticker. Tickers are not unique across CoinGecko, so when several
coins share a symbol we pick the largest by market cap and tag it `approx` —
never fabricated, and `null`/n-a when there is no match.

Cached in-memory (TTL) to respect CoinGecko's free rate limits.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("pump-reader.market")

CG_BASE = "https://api.coingecko.com/api/v3"
TTL_SECONDS = 1800  # 30 min cache per symbol

_cache: dict[str, tuple[float, dict | None]] = {}


async def market_for_symbol(base_symbol: str) -> dict | None:
    """Return market data for a base asset ticker (e.g. 'ON', 'BR'), or None."""
    key = base_symbol.upper()
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL_SECONDS:
        return hit[1]

    data: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{CG_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "symbols": key.lower(),
                    "per_page": 50,
                    "page": 1,
                },
            )
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    # Several coins can share a ticker -> pick the largest mcap.
                    rows = [r for r in rows if isinstance(r, dict)]
                    rows.sort(key=lambda r: (r.get("market_cap") or 0), reverse=True)
                    best = rows[0]
                    data = {
                        "symbol": key,
                        "coingecko_id": best.get("id"),
                        "name": best.get("name"),
                        "market_cap_usd": best.get("market_cap"),
                        "fdv_usd": best.get("fully_diluted_valuation"),
                        "circulating_supply": best.get("circulating_supply"),
                        "total_supply": best.get("total_supply"),
                        "price_usd": best.get("current_price"),
                        "approx": len(rows) > 1,
                    }
    except Exception:
        logger.exception("coingecko lookup failed: %s", key)
        data = None

    _cache[key] = (now, data)
    return data

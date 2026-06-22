"""Real DEX activity via DexScreener (FREE, no API key, no charge, no card).

This does NOT give exchange inflows (DexScreener has no on-chain transfer/deposit
data — that's Etherscan's job). What it DOES give, for free, instantly: real DEX
liquidity, volume, and BUY vs SELL transaction counts = on-chain buy pressure, a
genuine accumulation tell. Tokens are matched by contract address (chain-agnostic;
DexScreener returns pairs across all chains for an address).

Best-effort: None on any failure / unresolvable token. Never fabricates.
"""

from __future__ import annotations

import os
import time

import httpx

from . import onchain

_BASE = os.getenv("DEXSCREENER_API_BASE", "https://api.dexscreener.com/latest/dex/tokens")
_SEARCH = os.getenv("DEXSCREENER_SEARCH_BASE", "https://api.dexscreener.com/latest/dex/search")
_TTL = int(os.getenv("PUMP_DEX_CACHE_SECONDS", "180"))
# Sane quote tokens for the symbol-search fallback (avoid matching exotic pairs).
_QUOTE_OK = ("USDT", "USDC", "USD", "WETH", "ETH", "WBNB", "BNB", "SOL", "DAI", "BUSD")

_cache: dict[str, tuple[float, dict | None]] = {}


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _summarize(pairs: list) -> dict | None:
    """Pick the deepest-liquidity pair and pull its buy/sell pressure. Pure/testable."""
    pairs = [p for p in (pairs or []) if isinstance(p, dict)]
    if not pairs:
        return None
    p = max(pairs, key=lambda x: _f((x.get("liquidity") or {}).get("usd")))
    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}

    def bs(w: str) -> tuple[int, int]:
        t = txns.get(w) or {}
        return _i(t.get("buys")), _i(t.get("sells"))

    b1, s1 = bs("h1")
    b24, s24 = bs("h24")
    return {
        "source": "dexscreener",
        "liquidity_usd": round(_f((p.get("liquidity") or {}).get("usd")), 0),
        "vol_h1": round(_f(vol.get("h1")), 0),
        "vol_h24": round(_f(vol.get("h24")), 0),
        "buys_h1": b1, "sells_h1": s1,
        "buys_h24": b24, "sells_h24": s24,
        "buy_ratio_h1": round(b1 / (b1 + s1), 2) if (b1 + s1) else None,
        "buy_ratio_h24": round(b24 / (b24 + s24), 2) if (b24 + s24) else None,
        "price_change_h1": round(_f(chg.get("h1")), 2),
        "dex": p.get("dexId") or "",
        "chain": p.get("chainId") or "",
    }


async def _by_address(address: str) -> dict | None:
    ck = address.lower()
    cached = _cache.get(ck)
    if cached:
        ttl = _TTL if cached[1] is not None else 90
        if time.time() - cached[0] < ttl:
            return cached[1]
    summary: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "pump-reader"}) as client:
            r = await client.get(f"{_BASE}/{address}")
            data = r.json()
        summary = _summarize(data.get("pairs") or [])
    except Exception:
        summary = None
    _cache[ck] = (time.time(), summary)
    return summary


async def _by_symbol(base_symbol: str) -> dict | None:
    """Fallback: resolve a token's DEX activity by SEARCHING its ticker, for CEX
    tokens whose exchange doesn't expose a contract address in CCXT metadata
    (binance/mexc → 0% via the address path). Picks the deepest-liquidity pair whose
    baseToken symbol matches exactly and trades against a sane quote. Less precise
    than an address (ticker collisions possible) — fine for a heat *signal*, and the
    forensic override still needs real buy-pressure (heat>=70) before it acts."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base:
        return None
    ck = f"sym:{base}"
    cached = _cache.get(ck)
    if cached:
        ttl = _TTL if cached[1] is not None else 90
        if time.time() - cached[0] < ttl:
            return cached[1]
    summary: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "pump-reader"}) as client:
            r = await client.get(_SEARCH, params={"q": base})
            data = r.json()
        pairs = [p for p in (data.get("pairs") or []) if isinstance(p, dict)]

        def alive(p: dict) -> bool:
            # Reject the fake clones: ticker search surfaces copycat pools with a
            # huge FAKE liquidity number but ZERO real trading. A genuine token has
            # actual 24h volume AND transactions. Liquidity alone is forgeable; flow
            # is not — so we gate on flow, not on the liquidity figure.
            vol = p.get("volume") or {}
            tx = (p.get("txns") or {}).get("h24") or {}
            return _f(vol.get("h24")) > 0 and (_i(tx.get("buys")) + _i(tx.get("sells"))) > 0

        match = [p for p in pairs
                 if str((p.get("baseToken") or {}).get("symbol", "")).upper() == base
                 and str((p.get("quoteToken") or {}).get("symbol", "")).upper() in _QUOTE_OK
                 and alive(p)]
        # Rank by REAL 24h volume (flow), not by the spoofable liquidity number.
        match.sort(key=lambda p: _f((p.get("volume") or {}).get("h24")), reverse=True)
        summary = _summarize(match[:1]) if match else None
    except Exception:
        summary = None
    _cache[ck] = (time.time(), summary)
    return summary


# DexScreener chainId -> GoPlus token_security chain id. Solo cadenas que GoPlus
# soporta; el resto (solana, etc.) -> None y holders queda no disponible (honesto).
_DEX_CHAIN_TO_GOPLUS = {
    "ethereum": "1", "bsc": "56", "polygon": "137", "base": "8453",
    "arbitrum": "42161", "avalanche": "43114", "optimism": "10",
    "fantom": "250", "cronos": "25", "linea": "59144", "scroll": "534352",
    "tron": "tron",
}


async def resolve_onchain(base_symbol: str) -> tuple[str, str] | None:
    """(goplus_chain_id, token_address) del par con MÁS volumen real para este
    ticker, o None. Resuelve por flujo (no por mcap) → coincide con el token que
    la gente realmente tradea = el listado en el CEX. Alimenta los holders GoPlus
    cuando el exchange no expone el contrato en CCXT (binance/mexc/okx)."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base:
        return None
    ck = f"oc:{base}"
    cached = _cache.get(ck)
    if cached and time.time() - cached[0] < (_TTL if cached[1] is not None else 90):
        return cached[1]
    out: tuple[str, str] | None = None
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "pump-reader"}) as client:
            r = await client.get(_SEARCH, params={"q": base})
            data = r.json()
        pairs = [p for p in (data.get("pairs") or []) if isinstance(p, dict)]

        def real(p: dict) -> bool:
            vol = p.get("volume") or {}
            tx = (p.get("txns") or {}).get("h24") or {}
            return _f(vol.get("h24")) > 0 and (_i(tx.get("buys")) + _i(tx.get("sells"))) > 0

        match = [p for p in pairs
                 if str((p.get("baseToken") or {}).get("symbol", "")).upper() == base
                 and _DEX_CHAIN_TO_GOPLUS.get(str(p.get("chainId") or "").lower())
                 and real(p)]
        match.sort(key=lambda p: _f((p.get("volume") or {}).get("h24")), reverse=True)
        if match:
            best = match[0]
            chain = _DEX_CHAIN_TO_GOPLUS[str(best.get("chainId")).lower()]
            addr = str((best.get("baseToken") or {}).get("address") or "").strip()
            if addr:
                out = (chain, addr)
    except Exception:
        out = None
    _cache[ck] = (time.time(), out)
    return out


async def dex_activity(exchange_id: str, base_symbol: str) -> dict | None:
    """Real DEX liquidity + buy/sell pressure for a CEX token, or None.

    Two-tier resolution: (1) exact contract from the exchange's CCXT deposit
    metadata (precise, but binance/mexc expose none); (2) fallback search by ticker
    so those venues get coverage too."""
    resolved = await onchain.resolve_contract(exchange_id, base_symbol)
    if resolved:
        summary = await _by_address(resolved[2])
        if summary is not None:
            return {**summary, "resolved_by": "address"}      # precise (CEX contract)
    summary = await _by_symbol(base_symbol)
    return {**summary, "resolved_by": "symbol"} if summary else None  # approximate (ticker)

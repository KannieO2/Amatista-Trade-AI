"""CoinMarketCap Pro API — datos de mercado + señales scam para microcaps.

Complementa a market.py (CoinGecko): CoinGecko pierde muchos microcaps, CMC los
cubre. Aporta lo que el libro CEX no da:
  - market cap / FDV / supply  -> confirmar microcap real (lo que pumpea).
  - ratio FDV÷MarketCap         -> supply overhang: el equipo puede dumpear lo
                                   bloqueado = tell de scam/criminal-pump.
  - tags / categoría            -> 'memes', 'pump-fun'... arquetipo criminal-pump.
  - contrato on-chain (info)    -> resolución precisa para el veto anti-rug.

Best-effort: None en cualquier fallo. Nunca fabrica. Cacheado (TTL) para respetar
el rate-limit del plan gratis CMC. Requiere CMC_API_KEY en el entorno.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger("pump-reader.cmc")

_BASE = os.getenv("CMC_API_BASE", "https://pro-api.coinmarketcap.com")
TTL_SECONDS = int(os.getenv("CMC_CACHE_SECONDS", "1800"))  # 30 min

_quote_cache: dict[str, tuple[float, dict | None]] = {}
_info_cache: dict[str, tuple[float, dict | None]] = {}


def enabled() -> bool:
    return bool(os.getenv("CMC_API_KEY"))


def _headers() -> dict:
    return {"X-CMC_PRO_API_KEY": os.getenv("CMC_API_KEY", ""), "Accept": "application/json"}


async def quotes(base_symbol: str) -> dict | None:
    """Market data CMC para un ticker base (ej 'COLLECT'), o None.

    Normaliza al MISMO shape que market.market_for_symbol para ser drop-in fallback.
    """
    if not enabled():
        return None
    key = base_symbol.split("/")[0].upper()
    now = time.time()
    hit = _quote_cache.get(key)
    if hit and now - hit[0] < TTL_SECONDS:
        return hit[1]

    data: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers()) as client:
            resp = await client.get(
                f"{_BASE}/v1/cryptocurrency/quotes/latest",
                params={"symbol": key, "convert": "USD"},
            )
        if resp.status_code == 200:
            payload = resp.json() or {}
            entries = (payload.get("data") or {}).get(key)
            # CMC devuelve lista cuando varios tokens comparten ticker -> el de mayor cap.
            if isinstance(entries, list):
                entries = sorted(
                    [e for e in entries if isinstance(e, dict)],
                    key=lambda e: ((e.get("quote") or {}).get("USD") or {}).get("market_cap") or 0,
                    reverse=True,
                )
                entries = entries[0] if entries else None
            if isinstance(entries, dict):
                q = (entries.get("quote") or {}).get("USD") or {}
                mc = q.get("market_cap")
                fdv = q.get("fully_diluted_market_cap")
                data = {
                    "symbol": key,
                    "cmc_id": entries.get("id"),
                    "name": entries.get("name"),
                    "market_cap_usd": mc,
                    "fdv_usd": fdv,
                    "circulating_supply": entries.get("circulating_supply"),
                    "total_supply": entries.get("total_supply"),
                    "max_supply": entries.get("max_supply"),
                    "price_usd": q.get("price"),
                    "volume_24h_usd": q.get("volume_24h"),
                    "pct_change_24h": q.get("percent_change_24h"),
                    "fdv_mcap_ratio": round(fdv / mc, 2) if (mc and fdv and mc > 0) else None,
                    "tags": entries.get("tags") or [],
                    "source": "coinmarketcap",
                    "approx": False,
                }
    except Exception:
        logger.debug("cmc quotes failed: %s", key, exc_info=True)
        data = None

    _quote_cache[key] = (now, data)
    return data


async def contract(base_symbol: str) -> tuple[str, str] | None:
    """(platform_slug, contract_address) on-chain del token vía CMC info, o None.
    Resuelve el contrato preciso para alimentar el veto anti-rug (mejor que adivinar
    por ticker en DexScreener)."""
    if not enabled():
        return None
    key = base_symbol.split("/")[0].upper()
    now = time.time()
    hit = _info_cache.get(key)
    if hit and now - hit[0] < TTL_SECONDS:
        return hit[1]

    out: tuple[str, str] | None = None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers()) as client:
            resp = await client.get(f"{_BASE}/v1/cryptocurrency/info", params={"symbol": key})
        if resp.status_code == 200:
            entries = ((resp.json() or {}).get("data") or {}).get(key)
            if isinstance(entries, list):
                entries = entries[0] if entries else None
            if isinstance(entries, dict):
                addr = entries.get("contract_address") or []
                if isinstance(addr, list) and addr:
                    c0 = addr[0] or {}
                    plat = ((c0.get("platform") or {}).get("name") or "").lower()
                    a = c0.get("contract_address") or ""
                    if a:
                        out = (plat, a)
                plat2 = entries.get("platform") or {}
                if out is None and plat2.get("token_address"):
                    out = ((plat2.get("name") or "").lower(), plat2.get("token_address"))
    except Exception:
        logger.debug("cmc info failed: %s", key, exc_info=True)
        out = None

    _info_cache[key] = (now, out)
    return out


# Tags CMC que marcan el arquetipo criminal-pump / alto riesgo de dump.
_SCAM_TAGS = ("memes", "pump-fun", "pump.fun", "doggone-doggerel", "celebrity")
# FDV mucho mayor que el market cap = supply bloqueado que el equipo puede soltar.
SCAM_FDV_RATIO = float(os.getenv("PUMP_CMC_SCAM_FDV_RATIO", "5.0"))


def scam_flags(quote: dict | None) -> list[str]:
    """Tells de scam derivados de los datos CMC (vacío si no hay nada). Solo señal —
    NO bloquea solo; el caller decide qué hacer con esto."""
    if not quote:
        return []
    flags: list[str] = []
    ratio = quote.get("fdv_mcap_ratio")
    if ratio is not None and ratio >= SCAM_FDV_RATIO:
        flags.append(f"fdv_overhang_{ratio:g}x")
    tags = [str(t).lower() for t in (quote.get("tags") or [])]
    hit = [t for t in tags if any(s in t for s in _SCAM_TAGS)]
    if hit:
        flags.append("tag_" + hit[0])
    return flags

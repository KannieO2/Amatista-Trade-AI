"""Real account balance — READ ONLY.

Reads the user's real spot balance via CCXT `fetch_balance` using the SAME keys
as the executor (KEY_ENV). This only ever READS. It never places, cancels, or
withdraws. If no keys are set for an exchange, that exchange is skipped — the bot
falls back to the paper balance, so nothing breaks without keys.

Two robustness rules so the equity curve tracks the REAL balance smoothly:
  - All wallets per exchange are summed (spot + funding + swap), de-duplicated by
    fingerprint so an exchange that echoes the spot wallet for an unsupported type
    is never double-counted. → "otras carteras" are included in the balance.
  - Asset valuation uses a last-known-price cache: if a ticker fetch transiently
    fails, the previous price is reused instead of valuing the asset at $0. This
    stops the total (and therefore the equity curve) from collapsing to
    stablecoins-only and snapping back — the square-wave the dashboard showed.

Keys must be created spot-only, WITHOUT withdrawal permission.
"""

from __future__ import annotations

import logging

import ccxt.async_support as ccxt

from .executor import KEY_ENV, LiveBroker

logger = logging.getLogger("pump-reader.account")

# Stablecoins counted at $1 toward the USDT-equivalent total.
_STABLE_1TO1 = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USD1"}

# Extra (non-spot) wallet types to also read per exchange. Each is valued and
# SUMMED on top of spot. Unsupported types either raise (skipped) or echo spot
# (skipped by fingerprint de-dup), so this never double-counts.
_EXTRA_WALLETS = {
    "binance": ["funding"],
    "binanceus": [],
    "mexc": ["swap"],
    "bitget": ["funding", "swap"],
}

# Last good USDT price per "exchange:ASSET" so a transient ticker miss reuses the
# previous price instead of valuing the holding at $0.
_px_cache: dict[str, float] = {}


async def _value_holdings(client, exchange_id: str, totals: dict) -> tuple[float, dict]:
    """USDT-equivalent of a wallet's totals + the per-asset valued breakdown.
    Stablecoins 1:1; everything else from live tickers, falling back to the last
    known price (cache) when a ticker is momentarily unavailable."""
    holdings = {a: float(v) for a, v in (totals or {}).items() if v and float(v) > 0}
    if not holdings:
        return 0.0, {}
    non_stable = [a for a in holdings if a.upper() not in _STABLE_1TO1]
    tickers: dict = {}
    if non_stable:
        symbols = [f"{a}/USDT" for a in non_stable]
        try:
            tickers = await client.fetch_tickers(symbols)
        except Exception:
            try:
                tickers = await client.fetch_tickers()
            except Exception:
                tickers = {}
    valued: dict[str, float] = {}
    for asset, amount in holdings.items():
        if asset.upper() in _STABLE_1TO1:
            valued[asset] = amount
            continue
        ck = f"{exchange_id}:{asset.upper()}"
        tk = tickers.get(f"{asset}/USDT") or {}
        px = float(tk.get("last") or tk.get("close") or 0.0)
        if px > 0:
            _px_cache[ck] = px                 # remember a good price
        else:
            px = _px_cache.get(ck, 0.0)        # transient miss -> last known
        valued[asset] = amount * px
    return sum(valued.values()), valued


async def _balance_for(exchange_id: str) -> dict | None:
    cfg = LiveBroker._credentials(exchange_id)  # reuse same gated key lookup
    if cfg is None or not hasattr(ccxt, exchange_id):
        return None
    client = getattr(ccxt, exchange_id)(cfg)
    wallets: dict[str, float] = {}        # wallet type -> usdt
    amounts: dict[str, float] = {}        # asset -> total amount (across wallets)
    valued_all: dict[str, float] = {}     # asset -> usdt (across wallets)
    seen_fps: set = set()
    try:
        for wtype in [None] + _EXTRA_WALLETS.get(exchange_id, []):
            try:
                bal = (await client.fetch_balance({"type": wtype}) if wtype
                       else await client.fetch_balance())
            except Exception as exc:
                logger.debug("balance %s type=%s failed: %s", exchange_id, wtype, exc)
                continue
            totals = bal.get("total") or {}
            fp = frozenset((a, round(float(v), 8)) for a, v in totals.items()
                           if v and float(v) > 0)
            if not fp or fp in seen_fps:
                continue                  # empty or echoed wallet -> no double count
            seen_fps.add(fp)
            usdt, valued = await _value_holdings(client, exchange_id, totals)
            wallets[wtype or "spot"] = round(usdt, 2)
            for a, amt in totals.items():
                if amt and float(amt) > 0:
                    amounts[a] = amounts.get(a, 0.0) + float(amt)
            for a, v in valued.items():
                valued_all[a] = valued_all.get(a, 0.0) + v
    except Exception as exc:
        logger.warning("balance fetch failed for %s: %s", exchange_id, exc)
    finally:
        await client.close()

    if not wallets:
        return None
    usdt_equiv = sum(wallets.values())
    top = sorted(valued_all.items(), key=lambda x: -x[1])[:25]
    return {
        "exchange": exchange_id,
        "total_usdt": round(usdt_equiv, 2),
        "wallets": {k: v for k, v in wallets.items() if v > 0},  # spot/funding/swap
        "balances": {a: round(amounts[a], 8) for a, _ in top},
        "values_usdt": {a: round(v, 2) for a, v in top if v > 0},
    }


async def real_balances() -> dict:
    """Return real read-only balances for every exchange that has keys set.
    `total_usdt` is the sum across ALL exchanges AND all their wallets."""
    snapshots = []
    for exchange_id in KEY_ENV:
        snap = await _balance_for(exchange_id)
        if snap is not None:
            snapshots.append(snap)
    connected = [s["exchange"] for s in snapshots]
    total = round(sum(s["total_usdt"] for s in snapshots), 2)
    return {
        "connected": connected,
        "has_keys": bool(connected),
        "total_usdt": total,
        "snapshots": snapshots,
    }

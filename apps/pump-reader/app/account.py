"""Real account balance — READ ONLY.

Reads the user's real spot balance via CCXT `fetch_balance` using the SAME keys
as the executor (KEY_ENV). This only ever READS. It never places, cancels, or
withdraws. If no keys are set for an exchange, that exchange is skipped — the bot
falls back to the paper balance, so nothing breaks without keys.

Keys must be created spot-only, WITHOUT withdrawal permission.
"""

from __future__ import annotations

import logging

import ccxt.async_support as ccxt

from .executor import KEY_ENV, LiveBroker

logger = logging.getLogger("pump-reader.account")

# Stablecoins counted at $1 toward the USDT-equivalent total.
_STABLE_1TO1 = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USD1"}


async def _balance_for(exchange_id: str) -> dict | None:
    cfg = LiveBroker._credentials(exchange_id)  # reuse same gated key lookup
    if cfg is None or not hasattr(ccxt, exchange_id):
        return None
    client = getattr(ccxt, exchange_id)(cfg)
    try:
        bal = await client.fetch_balance()
    except Exception as exc:
        logger.warning("balance fetch failed for %s: %s", exchange_id, exc)
        return None
    finally:
        await client.close()

    totals = bal.get("total") or {}
    holdings = {a: float(v) for a, v in totals.items() if v and float(v) > 0}

    # Value every asset in USDT: stablecoins 1:1, the rest priced from live
    # tickers (BASE/USDT). Falls back gracefully if a market is missing.
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
        else:
            tk = tickers.get(f"{asset}/USDT") or {}
            px = float(tk.get("last") or tk.get("close") or 0.0)
            valued[asset] = amount * px
    usdt_equiv = sum(valued.values())
    top = sorted(valued.items(), key=lambda x: -x[1])[:25]
    return {
        "exchange": exchange_id,
        "total_usdt": round(usdt_equiv, 2),
        "balances": {a: round(holdings[a], 8) for a, _ in top},
        "values_usdt": {a: round(v, 2) for a, v in top if v > 0},
    }


async def real_balances() -> dict:
    """Return real read-only balances for every exchange that has keys set."""
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

"""Real exchange INFLOWS via the Etherscan V2 unified API (free key, NO credit card).

Closes the "Inflows (pre-pump)" gap without Arkham's $1,500/mo trap. Etherscan V2
uses ONE API key across 50+ EVM chains via a `chainid` param, so a single free key
covers Ethereum / BSC / Base / Arbitrum / Polygon / etc.

Signal: token transfers whose destination is a KNOWN exchange hot/deposit wallet =
supply arriving to a CEX to be sold/traded = a pre-move tell. We list recent
transfers of the token's contract and keep the ones going TO an exchange wallet
inside the window, valuing each at the live price.

ACTIVATION (user does this — free, instant, no card):
  1. etherscan.io → create account → API Keys → copy.
  2. Paste into .env as  ETHERSCAN_API_KEY=...
Without a key this returns None and the UI shows the CEX-volume proxy.

HONESTY (CLAUDE.md): the exchange-wallet list below is PUBLIC but PARTIAL and
exchanges rotate addresses, so this UNDERCOUNTS (never invents). Tron / non-EVM
chains are skipped (Etherscan is EVM-only). Best-effort: any failure → None → proxy.
"""

from __future__ import annotations

import os
import time

import httpx

from . import onchain

_BASE = os.getenv("ETHERSCAN_API_BASE", "https://api.etherscan.io/v2/api")
_WINDOW_H = float(os.getenv("PUMP_INFLOW_WINDOW_HOURS", "24"))
_LARGE_USD = float(os.getenv("PUMP_INFLOW_LARGE_USD", "100000"))
_TTL = int(os.getenv("PUMP_INFLOW_CACHE_SECONDS", "600"))

# Known exchange hot/deposit wallets (public Etherscan labels), lowercased. Partial
# on purpose — covers the biggest venues on ETH + BSC where CEX altcoins mostly
# live. Add more via PUMP_EXCHANGE_WALLETS (comma-separated) without code changes.
_EXCHANGE_WALLETS: set[str] = {
    # Binance
    "0x28c6c06298d514db089934071355e5743bf21d60", "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d", "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976", "0x5a52e96bacdabb82fd05763e25335261b270efcb",
    "0xf977814e90da44bfa03b6295a0616a897441acec", "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
    "0xe2fc31f816a9b94326492132018c3aecc4a93ae1",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b", "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
    "0xa7efae728d2936e78bda97dc267687568dd593f3",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3", "0x503828976d22510aad0201ac7ec88293211d23da",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740", "0x3cd751e6b0078be393132286c442345e5dc49699",
    "0xeb2629a2734e272bcc07bda959863f316f4bd4cf",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2", "0xae2d4617c862309a3d75a0ffb358c7a5009c673f",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0",
    # KuCoin
    "0x2b5634c42055806a59e9107ed44d43c426e58258", "0x689c56aef474df92d44a1b70850f808488f9769c",
    # Gate.io
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe", "0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c",
    # MEXC
    "0x0211f3cedbef3143223d3acf0e589747933e8527", "0x3cc936b795a188f0e246cbb2d74c5bd190aecf18",
    # Bybit
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40", "0xee5b5b923ffce93a870b3104b7ca09c3db80047a",
    # Bitget / Bitfinex / Huobi
    "0x0639556f03714a74a5feeaf5736a4a64ff70d206",
    "0x1151314c646ce4e0efd76d1af4760ae66a9fe30f", "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa",
    "0xab5c66752a9e8167967685f1450532fb96d5d24f", "0xe93381fb4c4f14bda253907b18fad305d799241a",
}
_EXTRA = [a.strip().lower() for a in os.getenv("PUMP_EXCHANGE_WALLETS", "").split(",") if a.strip()]
_EXCHANGE_WALLETS |= set(_EXTRA)

_cache: dict[str, tuple[float, dict | None]] = {}


def enabled() -> bool:
    return bool(os.getenv("ETHERSCAN_API_KEY", "").strip())


def _i(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _tally(result: list, price: float, cutoff: float) -> tuple[float, int, int]:
    """Sum USD of transfers TO an exchange wallet inside the window. Pure (testable):
    result = Etherscan tokentx rows (sorted desc by time)."""
    total = 0.0
    deposits = 0
    large = 0
    for tx in result:
        if _i(tx.get("timeStamp")) < cutoff:
            break                          # sorted desc → everything after is older
        if (tx.get("to") or "").lower() not in _EXCHANGE_WALLETS:
            continue
        dec = _i(tx.get("tokenDecimal")) or 18
        qty = _i(tx.get("value")) / (10 ** dec)
        usd = qty * (price or 0.0)
        total += usd
        deposits += 1
        if usd >= _LARGE_USD:
            large += 1
    return total, deposits, large


async def exchange_inflows(exchange_id: str, base_symbol: str,
                           price: float | None = None) -> dict | None:
    """Real exchange inflows for a CEX token over the window, or None. price = live
    USD price (token has USDT quote, so the CCXT last price ≈ USD)."""
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if not key:
        return None
    resolved = await onchain.resolve_contract(exchange_id, base_symbol)
    if resolved:
        chain_id, chain_name, address = resolved
    else:
        # Fallback: el exchange no expone el contrato en CCXT (binance/mexc/okx) →
        # resuelve por DexScreener (par de mayor volumen real = el token tradeado),
        # igual que holders. resolve_onchain ya da (chain_id_numérico, address).
        dex_res = None
        try:
            from . import dexscreener as _dx
            dex_res = await _dx.resolve_onchain(base_symbol)
        except Exception:
            dex_res = None
        if not dex_res:
            return None
        chain_id, address = dex_res
        chain_name = onchain.CHAIN_NAMES.get(chain_id, chain_id)
    if not chain_id.isdigit():       # tron / non-EVM → Etherscan can't serve it
        return None

    ck = f"{chain_id}:{address.lower()}"
    cached = _cache.get(ck)
    if cached:
        ttl = _TTL if cached[1] is not None else 120
        if time.time() - cached[0] < ttl:
            return cached[1]

    params = {
        "chainid": chain_id, "module": "account", "action": "tokentx",
        "contractaddress": address, "page": 1, "offset": 300, "sort": "desc",
        "apikey": key,
    }
    summary: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(_BASE, params=params)
            data = r.json()
        result = data.get("result")
        if not isinstance(result, list):       # error string / rate-limited
            raise ValueError(str(result)[:80])
        total, deposits, large = _tally(result, price or 0.0, time.time() - _WINDOW_H * 3600)
        summary = {
            "source": "etherscan",
            "window": f"{_WINDOW_H:g}h",
            "inflow_usd": round(total, 0),
            "deposits": deposits,
            "large_deposits": large,
            "chain": chain_name,
        }
    except Exception:
        summary = None
    _cache[ck] = (time.time(), summary)
    return summary

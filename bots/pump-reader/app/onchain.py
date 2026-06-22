"""Real on-chain holder concentration for CEX-listed tokens (free, no API key).

Closes the "Timeline holders (concentración)" gap: until now the Holders tab only
had an ORDERBOOK proxy. Here we resolve the token's real contract address from the
exchange's own deposit metadata (CCXT fetch_currencies exposes contractAddress per
network) and query the free GoPlus token-security API for the real holder list.

HONESTY (CLAUDE.md: never fabricate business data):
  - Returns None on ANY failure / unresolvable token. Never invents numbers.
  - Only chains GoPlus supports are queried; Solana / unsupported chains → None.
  - Exchange INFLOWS (token deposited to a CEX = pre-pump tell) are NOT here:
    real netflow needs a paid provider. The Inflows tab stays an honest proxy.

Everything is cached (currencies ~24h, holder data ~2h) and best-effort.
"""

from __future__ import annotations

import time

import httpx

# CCXT deposit-network name (upper-cased) -> GoPlus chain id. GoPlus token_security
# only covers EVM chains + tron; Solana uses a different endpoint (skipped).
_CHAIN_BY_NETWORK: dict[str, str] = {
    "ERC20": "1", "ETH": "1", "ETHEREUM": "1",
    "BEP20": "56", "BSC": "56", "BNB": "56", "BEP-20": "56", "BNBSMARTCHAIN": "56",
    "BASE": "8453",
    "ARBITRUM": "42161", "ARB": "42161", "ARBITRUMONE": "42161",
    "OPTIMISM": "10", "OP": "10",
    "POLYGON": "137", "MATIC": "137",
    "AVAXC": "43114", "AVALANCHE": "43114", "AVAX": "43114",
    "FANTOM": "250", "FTM": "250",
    "LINEA": "59144", "ZKSYNC": "324", "ZKSYNCERA": "324", "SCROLL": "534352",
    "OPBNB": "204", "MANTLE": "5000", "CRONOS": "25",
    "TRC20": "tron", "TRON": "tron", "TRX": "tron",
}
# When a token lives on several chains, query the most canonical one first.
_CHAIN_PREFERENCE = ["1", "56", "8453", "42161", "137", "10", "43114", "250", "tron"]
# GoPlus chain id -> Arkham chain name (for the inflows module). Only the chains
# both sides support are mapped; others resolve to None and inflows are skipped.
CHAIN_NAMES: dict[str, str] = {
    "1": "ethereum", "56": "bsc", "8453": "base", "42161": "arbitrum_one",
    "137": "polygon", "10": "optimism", "43114": "avalanche", "250": "fantom",
    "tron": "tron",
}

import os

_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
# Mínimo de holders para confiar en una resolución por ticker (dex). Por debajo =
# clon bridged/dust, no el token real listado en el CEX.
_DEX_MIN_HOLDERS = int(os.getenv("PUMP_HOLDERS_DEX_MIN", "50"))
_CONC_TOP1 = 25.0    # a single non-LP holder above this % = concentrated (rug-prone)
_CONC_TOP10 = 70.0   # top-10 holders above this combined % = concentrated

# caches
_currencies: dict[str, tuple[float, dict[str, list[tuple[str, str]]]]] = {}  # exch -> (ts, {SYM:[(chain,addr)]})
_CURR_TTL = 24 * 3600
_holders: dict[str, tuple[float, dict | None]] = {}  # "chain:addr" -> (ts, result)
_HOLD_TTL = 2 * 3600      # cache a real result this long
_HOLD_NEG_TTL = 120       # a failed/None lookup recovers fast (don't poison for 2h)

# tags GoPlus uses for non-whale holders (LP pools, burn, the contract itself)
_NON_WHALE_TAGS = ("lp", "pool", "null", "burn", "dead", "lock")


def _norm_net(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())


async def _load_currencies(exchange_id: str) -> dict[str, list[tuple[str, str]]]:
    """{BASE_SYMBOL: [(goplus_chain_id, contract_address), ...]} from CCXT deposit
    metadata. Cached per exchange. Best-effort: returns {} on failure."""
    cached = _currencies.get(exchange_id)
    if cached and time.time() - cached[0] < _CURR_TTL:
        return cached[1]
    import ccxt.async_support as ccxt
    if not hasattr(ccxt, exchange_id):
        return {}
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        cur = await ex.fetch_currencies() or {}
        for code, c in cur.items():
            nets = (c or {}).get("networks") or {}
            pairs: list[tuple[str, str]] = []
            for nname, n in nets.items():
                info = (n or {}).get("info") or {}
                addr = (info.get("contractAddress") or info.get("contractAddr")
                        or info.get("contract") or info.get("contractaddress"))
                if not addr or len(str(addr)) < 30:
                    continue
                chain = _CHAIN_BY_NETWORK.get(_norm_net(str(nname))) \
                    or _CHAIN_BY_NETWORK.get(_norm_net(str(n.get("network") or n.get("id") or "")))
                if chain:
                    pairs.append((chain, str(addr).strip()))
            if pairs:
                out[code.upper()] = pairs
    except Exception:
        return _currencies.get(exchange_id, (0, {}))[1]
    finally:
        try:
            await ex.close()
        except Exception:
            pass
    if out:                       # only cache a real map; retry next time if empty
        _currencies[exchange_id] = (time.time(), out)
    return out


def _resolve(pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pick the most canonical (chain, address) for a multi-chain token."""
    by_chain = {chain: addr for chain, addr in pairs}
    for chain in _CHAIN_PREFERENCE:
        if chain in by_chain:
            return chain, by_chain[chain]
    return pairs[0] if pairs else None


def _summarize(res: dict) -> dict | None:
    """Turn a raw GoPlus token_security result into a holder-concentration summary."""
    if not res:
        return None
    holders = res.get("holders") or []
    try:
        holder_count = int(res.get("holder_count") or 0)
    except (TypeError, ValueError):
        holder_count = 0
    if not holders and holder_count == 0:
        return None

    def pct(h: dict) -> float:
        try:
            return float(h.get("percent") or 0.0) * 100.0
        except (TypeError, ValueError):
            return 0.0

    def is_whale(h: dict) -> bool:
        tag = (h.get("tag") or "").lower()
        return not any(t in tag for t in _NON_WHALE_TAGS) and int(h.get("is_contract") or 0) == 0

    top10 = round(sum(pct(h) for h in holders[:10]), 2)
    whales = [h for h in holders if is_whale(h)]
    top1_whale = round(max((pct(h) for h in whales), default=0.0), 2)
    top = [{"pct": round(pct(h), 2), "tag": (h.get("tag") or ""),
            "is_contract": int(h.get("is_contract") or 0)} for h in holders[:5]]
    concentrated = top1_whale >= _CONC_TOP1 or top10 >= _CONC_TOP10
    # Contract-security flags (free, same GoPlus response). On a CEX you sell against
    # the venue book, so honeypot/cannot-sell can't trap your EXIT — these are read as
    # a SCAM-INTENT signal feeding Dangerous_Signals, NOT a can't-sell execution block.
    honeypot = _flag(res, "is_honeypot")
    cannot_sell = _flag(res, "cannot_sell_all")
    blacklist = _flag(res, "is_blacklisted")
    pausable = _flag(res, "transfer_pausable")
    buy_tax = round(_f(res.get("buy_tax")) * 100, 2)
    sell_tax = round(_f(res.get("sell_tax")) * 100, 2)
    dangerous_contract = bool(honeypot or cannot_sell or blacklist or sell_tax >= 10.0)
    return {
        "source": "goplus",
        "holder_count": holder_count,
        "top1_whale_pct": top1_whale,
        "top10_pct": top10,
        "top": top,
        "creator_pct": round(_f(res.get("creator_percent")) * 100, 2),
        "owner_pct": round(_f(res.get("owner_percent")) * 100, 2),
        "concentrated": concentrated,
        "honeypot": honeypot,
        "cannot_sell": cannot_sell,
        "blacklist": blacklist,
        "transfer_pausable": pausable,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "dangerous_contract": dangerous_contract,
    }


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _flag(res: dict, *keys: str) -> int | None:
    """GoPlus boolean-ish field ('1'/'0'/1/0/None) → int 0/1, or None if absent."""
    for k in keys:
        v = res.get(k)
        if v is None:
            continue
        try:
            return 1 if int(v) != 0 else 0
        except (TypeError, ValueError):
            return 1 if str(v).strip().lower() in ("1", "true", "yes") else 0
    return None


async def _goplus(chain: str, address: str) -> dict | None:
    key = f"{chain}:{address.lower()}"
    cached = _holders.get(key)
    if cached:
        ttl = _HOLD_TTL if cached[1] is not None else _HOLD_NEG_TTL
        if time.time() - cached[0] < ttl:
            return cached[1]
    url = f"{_GOPLUS}/{chain}?contract_addresses={address}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            data = r.json()
        res = (data.get("result") or {}).get(address.lower()) or {}
        summary = _summarize(res)
    except Exception:
        summary = None
    _holders[key] = (time.time(), summary)
    return summary


async def holder_concentration(exchange_id: str, base_symbol: str) -> dict | None:
    """Real on-chain holder concentration for a CEX token, or None if unresolvable.
    base_symbol is the BASE (e.g. 'PEPE' from 'PEPE/USDT')."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base:
        return None
    curr = await _load_currencies(exchange_id)
    pairs = curr.get(base)
    resolved = _resolve(pairs) if pairs else None
    src = "cex"
    # Fallback: el exchange no expone el contrato en CCXT (binance/mexc/okx) →
    # resuelve por DexScreener (par de mayor volumen real = el token que se tradea).
    if not resolved:
        try:
            from . import dexscreener as _dx
            resolved = await _dx.resolve_onchain(base)
            src = "dex"
        except Exception:
            resolved = None
    if not resolved:
        return None
    chain, address = resolved
    summary = await _goplus(chain, address)
    if summary is None:
        return None
    # Guard anti-basura: una resolución por ticker (dex) puede matchear un clon
    # bridged/dust (ej STEEM nativo → token STEEM en Avalanche con 1 holder). Un
    # token real listado en CEX tiene miles de holders → si la dex-resuelta trae
    # implausiblemente pocos, NO es el token tradeado: devuelve None (honesto).
    if src == "dex" and int(summary.get("holder_count") or 0) < _DEX_MIN_HOLDERS:
        return None
    return {**summary, "chain": chain, "address": address, "resolved_by": src}


async def resolve_contract(exchange_id: str, base_symbol: str) -> tuple[str, str, str] | None:
    """(goplus_chain_id, arkham_chain_name, contract_address) for a CEX token, or
    None. Shared by the inflows module so contract resolution lives in one place."""
    base = (base_symbol or "").split("/")[0].upper()
    if not base:
        return None
    curr = await _load_currencies(exchange_id)
    resolved = _resolve(curr.get(base) or [])
    if not resolved:
        return None
    chain, address = resolved
    name = CHAIN_NAMES.get(chain)
    if not name:
        return None
    return chain, name, address

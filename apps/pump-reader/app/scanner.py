"""Real Binance Spot USDT pump scanner using public CCXT (no API keys).

Detects criminal/scam pump patterns with explicit, auditable rule heuristics:
manufactured volume spikes on thin liquidity, orderbook imbalance, and price
already running. No machine learning, no invented data. Any signal that cannot
be sourced from the public exchange (e.g. on-chain holder concentration) is
omitted, not faked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from statistics import mean

import ccxt.async_support as ccxt

# Bases excluded from the altcoin universe: majors are too liquid to "pump"
# in the criminal sense, stables/fiat are not targets, leveraged tokens are
# derivatives not spot pumps.
MAJORS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "DOT",
    "AVAX", "LINK", "MATIC", "LTC", "BCH", "ATOM", "ETC", "XLM",
}
STABLES = {
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USD1", "AEUR",
    "EUR", "TRY", "BRL", "ARS", "GBP", "JPY", "EURI", "XUSD",
}
LEVERAGED_MARKERS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

# Shortlist bounds: ignore dust (untradeable) and ignore mega-volume blue chips.
MIN_QUOTE_VOLUME_USD = 100_000
MAX_QUOTE_VOLUME_USD = 60_000_000
SHORTLIST_SIZE = 20
DEEP_FETCH_CONCURRENCY = 5

# Liquidity is measured as resting notional within this band around mid price.
DEPTH_BAND_PCT = 0.02
LOW_LIQUIDITY_USD = 75_000

# Score at/above which a candidate needs human confirmation in the UI.
WAITING_CONFIRMATION_THRESHOLD = 75


@dataclass
class ScannedCandidate:
    symbol: str
    exchange: str
    last_price: float
    quote_volume_24h: float
    price_change_pct_24h: float
    volume_spike: float
    orderbook_imbalance: float
    liquidity_usd: float
    pump_score: int
    confidence_score: int
    classification: str
    cluster: str = "long_pump"
    flags: list[str] = field(default_factory=list)
    spark: list[float] = field(default_factory=list)


def _cluster(price_change_pct: float, volume_spike: float, imbalance: float) -> str:
    """Two clusters like the source tool: 'classic' (short-squeeze grind) vs
    'long_pump' (buyer impulse). Spot-only proxy until futures OI/funding wire in:
    a stacked book grinding up without a volume explosion looks 'classic'.
    """
    if imbalance >= 0.70 and price_change_pct < 25 and volume_spike < 3:
        return "classic"
    return "long_pump"


def _is_altcoin(market: dict) -> bool:
    if not market.get("spot") or not market.get("active"):
        return False
    if market.get("quote") != "USDT":
        return False
    base = market.get("base", "")
    if base in MAJORS or base in STABLES:
        return False
    if any(marker in base for marker in LEVERAGED_MARKERS):
        return False
    return True


def _orderbook_metrics(order_book: dict) -> tuple[float, float]:
    """Return (bid_imbalance 0..1, liquidity_usd within DEPTH_BAND_PCT of mid)."""
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return 0.5, 0.0

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return 0.5, 0.0

    low = mid * (1 - DEPTH_BAND_PCT)
    high = mid * (1 + DEPTH_BAND_PCT)

    bid_notional = sum(price * amount for price, amount in bids if price >= low)
    ask_notional = sum(price * amount for price, amount in asks if price <= high)
    total = bid_notional + ask_notional
    imbalance = bid_notional / total if total > 0 else 0.5
    return imbalance, total


def _volume_spike(ohlcv: list[list[float]]) -> float:
    """Last *closed* candle volume vs. mean of the prior window. 1.0 = no spike.

    CCXT returns the in-progress (partial) candle as the final element, so it is
    dropped here; using it would understate volume on every symbol.
    """
    volumes = [row[5] for row in ohlcv if row and row[5] is not None]
    if len(volumes) < 4:
        return 1.0
    recent = volumes[-2]  # last fully closed candle
    base = mean(volumes[:-2])
    if base <= 0:
        return 1.0
    return recent / base


def score_candidate(
    *,
    price_change_pct: float,
    volume_spike: float,
    imbalance: float,
    liquidity_usd: float,
) -> tuple[int, int, str, list[str]]:
    """Explicit rule heuristic. Every point is traceable to a condition."""
    score = 0.0
    flags: list[str] = []

    if volume_spike >= 10:
        score += 45
        flags.append("extreme_volume_spike")
    elif volume_spike >= 6:
        score += 35
        flags.append("high_volume_spike")
    elif volume_spike >= 3:
        score += 25
        flags.append("volume_spike")

    if price_change_pct >= 50:
        score += 35
        flags.append("price_parabolic")
    elif price_change_pct >= 25:
        score += 25
        flags.append("price_running")
    elif price_change_pct >= 10:
        score += 15

    if imbalance >= 0.80:
        score += 20
        flags.append("bids_stacked")
    elif imbalance >= 0.65:
        score += 10

    low_liquidity = liquidity_usd < LOW_LIQUIDITY_USD
    if low_liquidity and volume_spike >= 3:
        # Thin book + manufactured volume is the classic scam/criminal pump tell.
        score += 15
        flags.append("low_liquidity_trap")

    pump_score = int(max(0, min(100, round(score))))

    # Confidence rises with real liquidity (harder to fake) and a clean,
    # not-yet-exhausted move. Pure thin-book spikes stay low-confidence.
    confidence = 35.0
    confidence += min(liquidity_usd / 10_000, 35)  # up to +35 for deep book
    if 10 <= price_change_pct <= 60:
        confidence += 15  # live move, not already dumped
    if volume_spike >= 3:
        confidence += 10
    confidence_score = int(max(0, min(100, round(confidence))))

    if low_liquidity and volume_spike >= 6 and price_change_pct >= 10:
        classification = "criminal_pump_suspect"
    elif volume_spike >= 6 and price_change_pct >= 25:
        classification = "active_pump"
    elif imbalance >= 0.8 and volume_spike >= 3:
        classification = "accumulation_imbalance"
    elif pump_score > 0:
        classification = "volume_anomaly"
    else:
        classification = "no_signal"

    return pump_score, confidence_score, classification, flags


async def _deep_scan_symbol(
    exchange,
    exchange_id: str,
    symbol: str,
    ticker: dict,
    semaphore: asyncio.Semaphore,
) -> ScannedCandidate | None:
    async with semaphore:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=24)
            order_book = await exchange.fetch_order_book(symbol, limit=50)
        except Exception:
            return None

    spike = _volume_spike(ohlcv)
    imbalance, liquidity = _orderbook_metrics(order_book)
    price_change = float(ticker.get("percentage") or 0.0)

    pump_score, confidence, classification, flags = score_candidate(
        price_change_pct=price_change,
        volume_spike=spike,
        imbalance=imbalance,
        liquidity_usd=liquidity,
    )

    # Last 12 closed-candle closes for the UI sparkline.
    closes = [row[4] for row in ohlcv[:-1] if row and row[4] is not None]
    spark = [round(c, 8) for c in closes[-12:]]

    return ScannedCandidate(
        symbol=symbol,
        exchange=exchange_id,
        last_price=float(ticker.get("last") or 0.0),
        quote_volume_24h=float(ticker.get("quoteVolume") or 0.0),
        price_change_pct_24h=round(price_change, 2),
        volume_spike=round(spike, 2),
        orderbook_imbalance=round(imbalance, 3),
        liquidity_usd=round(liquidity, 2),
        pump_score=pump_score,
        confidence_score=confidence,
        classification=classification,
        cluster=_cluster(price_change, spike, imbalance),
        flags=flags,
        spark=spark,
    )


# Exchanges allowed for public scanning (no API keys needed). These are where
# the cheap microcap "scam pump" tokens actually trade — Binance lists few of
# them, which is why the source tool leans on MEXC/Bitget.
SUPPORTED_EXCHANGES = ("binance", "mexc", "bitget")


async def scan_exchange(exchange_id: str, min_pump_score: int = 1) -> list[ScannedCandidate]:
    """Scan one exchange: rank gainers, deep-fetch the shortlist, score by rules."""
    if not hasattr(ccxt, exchange_id):
        return []
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()

        shortlist: list[tuple[str, dict]] = []
        for symbol, ticker in tickers.items():
            market = exchange.markets.get(symbol)
            if market is None or not _is_altcoin(market):
                continue
            quote_volume = float(ticker.get("quoteVolume") or 0.0)
            if not (MIN_QUOTE_VOLUME_USD <= quote_volume <= MAX_QUOTE_VOLUME_USD):
                continue
            change = float(ticker.get("percentage") or 0.0)
            if change <= 0:
                continue
            shortlist.append((symbol, ticker))

        shortlist.sort(key=lambda item: float(item[1].get("percentage") or 0.0), reverse=True)
        shortlist = shortlist[:SHORTLIST_SIZE]

        semaphore = asyncio.Semaphore(DEEP_FETCH_CONCURRENCY)
        results = await asyncio.gather(
            *(_deep_scan_symbol(exchange, exchange_id, symbol, ticker, semaphore) for symbol, ticker in shortlist)
        )
    except Exception:
        return []
    finally:
        await exchange.close()

    return [c for c in results if c is not None and c.pump_score >= min_pump_score]


async def fetch_token_detail(
    exchange_id: str, symbol: str, timeframe: str = "1h", limit: int = 24
) -> dict | None:
    """On-demand deep data for the candidate modal: live OHLCV + orderbook +
    ticker. Powers the Timeline / Holders(depth) / Inflows tabs with real CCXT
    data (no fabrication). On-chain holder/inflow series need a separate
    provider — here we surface the CEX orderbook + volume as honest proxies.
    """
    if not hasattr(ccxt, exchange_id):
        return None
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        order_book = await exchange.fetch_order_book(symbol, limit=20)
        ticker = await exchange.fetch_ticker(symbol)
    except Exception:
        return None
    finally:
        await exchange.close()

    rows = [r for r in ohlcv if r and r[5] is not None]
    closed = rows[:-1] if len(rows) > 1 else rows  # drop in-progress candle
    candles = [
        {"t": int(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
        for r in closed
    ]
    imbalance, liquidity = _orderbook_metrics(order_book)
    spike = _volume_spike(ohlcv)
    bids = [[float(p), float(a)] for p, a in (order_book.get("bids") or [])[:15]]
    asks = [[float(p), float(a)] for p, a in (order_book.get("asks") or [])[:15]]
    return {
        "symbol": symbol,
        "exchange": exchange_id,
        "timeframe": timeframe,
        "candles": candles,
        "depth": {
            "bids": bids,
            "asks": asks,
            "imbalance": round(imbalance, 3),
            "liquidity_usd": round(liquidity, 2),
        },
        "stats": {
            "last": float(ticker.get("last") or 0.0),
            "vol_spike": round(spike, 2),
            "quote_volume_24h": float(ticker.get("quoteVolume") or 0.0),
            "price_change_pct_24h": round(float(ticker.get("percentage") or 0.0), 2),
        },
    }


async def scan_markets(
    exchange_ids: list[str] | None = None,
    min_pump_score: int = 1,
) -> list[ScannedCandidate]:
    """Scan several exchanges in parallel and merge. Tokens are tagged by venue."""
    exchange_ids = exchange_ids or list(SUPPORTED_EXCHANGES)
    per_exchange = await asyncio.gather(
        *(scan_exchange(eid, min_pump_score) for eid in exchange_ids)
    )
    candidates = [c for batch in per_exchange for c in batch]
    candidates.sort(key=lambda c: c.pump_score, reverse=True)
    return candidates

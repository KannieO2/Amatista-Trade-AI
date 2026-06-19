"""Real Binance Spot USDT pump scanner using public CCXT (no API keys).

Detects criminal/scam pump patterns with explicit, auditable rule heuristics:
manufactured volume spikes on thin liquidity, orderbook imbalance, and price
already running. No machine learning, no invented data. Any signal that cannot
be sourced from the public exchange (e.g. on-chain holder concentration) is
omitted, not faked.
"""

from __future__ import annotations

import asyncio
import os
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

# Pre-pump (accumulation) feed. The momentum shortlist above is GAINERS-ONLY
# (sorted by 24h % up), so the accumulation detector (scores.py / FSM) never sees
# a token BEFORE it runs — it only ever scores tokens already pumping = late by
# construction. This SECOND shortlist admits FLAT-but-liquid tokens (small 24h
# move, real turnover) so the microstructure recorder + FSM can watch them
# absorb/accumulate and fire BEFORE the breakout. Ranked by 24h quote volume
# (real interest while the price is still quiet). This is what makes "detect
# antes" possible; without it the bot is structurally a momentum chaser.
ACCUM_MIN_CHG_PCT = float(os.getenv("PUMP_ACCUM_MIN_CHG_PCT", "-3"))
ACCUM_MAX_CHG_PCT = float(os.getenv("PUMP_ACCUM_MAX_CHG_PCT", "12"))
ACCUM_SHORTLIST_SIZE = int(os.getenv("PUMP_ACCUM_SHORTLIST_SIZE", "20"))

# Liquidity is measured as resting notional within this band around mid price.
DEPTH_BAND_PCT = 0.02
LOW_LIQUIDITY_USD = 75_000

# Score at/above which a candidate needs human confirmation in the UI.
WAITING_CONFIRMATION_THRESHOLD = 75

# --- ForensicFilter (CEX-applicable pre-trade safety) -------------------------
# IMPORTANT honesty note: on a centralized exchange you trade against the venue's
# orderbook, NOT a token contract. So DEX-only checks (honeypot / liquidity
# burned / buy-sell tax / holder list) have no source here and are NOT faked.
# What IS measurable from public CEX data is enforced below:
#   - spread (ask-bid)/ask too wide  → illiquid / unsafe fill
#   - resting liquidity below a floor → can't exit without slippage
#   - book concentrated in <=3 levels → spoof / single-actor manipulation proxy
# (For true on-chain DEX tokens, wire GoPlus/DexScreener via a contract-address
#  map; left as an explicit optional hook rather than guessed.)
# Stricter entry gate: thin books bleed the spread immediately (the "4 entries
# all close -0.5% on timeout" symptom = entering low-liquidity zones). Require a
# tighter spread and a deeper book before any auto-entry.
FORENSIC_MAX_SPREAD_PCT = float(os.getenv("PUMP_FORENSIC_MAX_SPREAD_PCT", "1.0"))
# Liquidity floor 120k (was 50k): the Monte Carlo showed the entire negative
# expectancy is the rug / gap-down fat tail, and rugs are thin-book events — a
# deeper book is the single biggest protection. Higher floor = far fewer trades
# but materially better EV. Tune down for more (riskier) entries.
FORENSIC_MIN_LIQUIDITY_USD = float(os.getenv("PUMP_FORENSIC_MIN_LIQUIDITY_USD", "120000"))
FORENSIC_MAX_TOP_SHARE = float(os.getenv("PUMP_FORENSIC_MAX_TOP_SHARE", "0.80"))


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
    score_long_pump: int = 0
    score_classic: int = 0
    spread_pct: float = 0.0           # (ask-bid)/ask, forensic spread filter
    top_book_share: float = 0.0       # share of bid book in top-3 levels (concentration)
    manipulation_suspect: bool = False
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


def _forensic_metrics(order_book: dict) -> tuple[float, float]:
    """Return (spread_pct, top3_bid_share) from the public CEX orderbook.

    spread_pct   = (best_ask - best_bid) / best_ask * 100
    top3_bid_share = notional in the best 3 bid levels / total bid notional.
    A high share means a thin book held up by a few orders — the CEX-visible
    proxy for single-actor manipulation (we cannot see wallets on a CEX).
    """
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return 100.0, 1.0  # no book = treat as maximally unsafe
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    spread_pct = (best_ask - best_bid) / best_ask * 100 if best_ask > 0 else 100.0
    bid_notional = [p * a for p, a in bids]
    total_bid = sum(bid_notional)
    top3 = sum(sorted(bid_notional, reverse=True)[:3])
    top_share = top3 / total_bid if total_bid > 0 else 1.0
    return round(spread_pct, 4), round(top_share, 4)


def forensic_check(*, spread_pct: float, liquidity_usd: float,
                   top_book_share: float,
                   min_liquidity_usd: float | None = None) -> tuple[bool, list[str]]:
    """Pre-trade gate. Returns (ok_to_enter, reasons_if_blocked). Real, auditable,
    CEX-sourced — no fabricated on-chain data. min_liquidity_usd overrides the
    default floor (pre-pump path uses a lower one — thinner accumulation books)."""
    floor = FORENSIC_MIN_LIQUIDITY_USD if min_liquidity_usd is None else min_liquidity_usd
    reasons: list[str] = []
    if spread_pct > FORENSIC_MAX_SPREAD_PCT:
        reasons.append(f"spread {spread_pct:.2f}% > {FORENSIC_MAX_SPREAD_PCT}%")
    if liquidity_usd < floor:
        reasons.append(f"liquidity ${liquidity_usd:,.0f} < ${floor:,.0f}")
    if top_book_share > FORENSIC_MAX_TOP_SHARE:
        reasons.append(f"book {top_book_share*100:.0f}% in top-3 levels (MANIPULATION_SUSPECT)")
    return (not reasons), reasons


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
) -> tuple[int, int, str, list[str], int, int, str]:
    """Explicit rule heuristic. Scores TWO competing criteria and keeps the max.

    Returns (pump_score, confidence, classification, flags, score_long_pump,
    score_classic, cluster). The cluster is whichever criterion scores higher, so
    the UI can say which one is "sounding" louder.
    """
    flags: list[str] = []
    low_liquidity = liquidity_usd < LOW_LIQUIDITY_USD

    # --- long_pump (buyer impulse): volume explosion + price run + stacked bids
    lp = 0.0
    if volume_spike >= 10:
        lp += 45
        flags.append("extreme_volume_spike")
    elif volume_spike >= 6:
        lp += 35
        flags.append("high_volume_spike")
    elif volume_spike >= 3:
        lp += 25
        flags.append("volume_spike")
    if price_change_pct >= 50:
        lp += 35
        flags.append("price_parabolic")
    elif price_change_pct >= 25:
        lp += 25
        flags.append("price_running")
    elif price_change_pct >= 10:
        lp += 15
    if imbalance >= 0.80:
        lp += 20
        flags.append("bids_stacked")
    elif imbalance >= 0.65:
        lp += 10
    if low_liquidity and volume_spike >= 3:
        # Thin book + manufactured volume is the classic scam/criminal pump tell.
        lp += 15
        flags.append("low_liquidity_trap")

    # --- classic (short-squeeze grind): stacked book grinding up, modest volume
    cl = 0.0
    if imbalance >= 0.80:
        cl += 40
    elif imbalance >= 0.70:
        cl += 30
    elif imbalance >= 0.60:
        cl += 18
    elif imbalance >= 0.55:
        cl += 8
    if 5 <= price_change_pct < 25:
        cl += 25
    elif 25 <= price_change_pct < 50:
        cl += 12
    if volume_spike < 3:
        cl += 15
    elif volume_spike < 6:
        cl += 8
    if low_liquidity and imbalance >= 0.65:
        cl += 10

    score_long_pump = int(max(0, min(100, round(lp))))
    score_classic = int(max(0, min(100, round(cl))))
    pump_score = max(score_long_pump, score_classic)
    cluster = "classic" if score_classic > score_long_pump else "long_pump"
    if cluster == "classic" and "squeeze_grind" not in flags:
        flags.append("squeeze_grind")

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

    return pump_score, confidence_score, classification, flags, score_long_pump, score_classic, cluster


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
    spread_pct, top_share = _forensic_metrics(order_book)
    price_change = float(ticker.get("percentage") or 0.0)

    pump_score, confidence, classification, flags, score_long_pump, score_classic, cluster = score_candidate(
        price_change_pct=price_change,
        volume_spike=spike,
        imbalance=imbalance,
        liquidity_usd=liquidity,
    )

    # ForensicFilter: flag (don't fake) manipulation tells from real book data.
    ok, reasons = forensic_check(
        spread_pct=spread_pct, liquidity_usd=liquidity, top_book_share=top_share
    )
    manipulation_suspect = top_share > FORENSIC_MAX_TOP_SHARE
    if not ok:
        flags.append("forensic_block")
    if manipulation_suspect and "manipulation_suspect" not in flags:
        flags.append("manipulation_suspect")
        if classification not in ("no_signal",):
            classification = "manipulation_suspect"
    if spread_pct > FORENSIC_MAX_SPREAD_PCT:
        flags.append("wide_spread")

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
        cluster=cluster,
        score_long_pump=score_long_pump,
        score_classic=score_classic,
        spread_pct=spread_pct,
        top_book_share=top_share,
        manipulation_suspect=manipulation_suspect,
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

        gainers: list[tuple[str, dict]] = []
        accumulation: list[tuple[str, dict]] = []
        for symbol, ticker in tickers.items():
            market = exchange.markets.get(symbol)
            if market is None or not _is_altcoin(market):
                continue
            quote_volume = float(ticker.get("quoteVolume") or 0.0)
            if not (MIN_QUOTE_VOLUME_USD <= quote_volume <= MAX_QUOTE_VOLUME_USD):
                continue
            change = float(ticker.get("percentage") or 0.0)
            # Momentum path: already running (late by nature, but kept).
            if change > 0:
                gainers.append((symbol, ticker))
            # Pre-pump path: still FLAT but with real turnover = accumulation
            # candidate. The FSM decides over time if it's truly accumulating.
            if ACCUM_MIN_CHG_PCT <= change <= ACCUM_MAX_CHG_PCT:
                accumulation.append((symbol, ticker))

        gainers.sort(key=lambda item: float(item[1].get("percentage") or 0.0), reverse=True)
        accumulation.sort(key=lambda item: float(item[1].get("quoteVolume") or 0.0), reverse=True)

        # Merge both shortlists, dedup (a small gainer can be in both).
        merged: dict[str, dict] = {}
        for symbol, ticker in gainers[:SHORTLIST_SIZE] + accumulation[:ACCUM_SHORTLIST_SIZE]:
            merged.setdefault(symbol, ticker)
        shortlist = list(merged.items())

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

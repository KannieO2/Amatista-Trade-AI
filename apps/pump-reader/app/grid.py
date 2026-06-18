"""GRVTBot-style grid engine (paper).

Models the core of github.com/kmanus88/GRVTBot: a price range split into N
levels, a buy at each level filled as price falls, sold one level up as price
rises, fills auto-replaced. Virtual grid (state in-engine, like GRVTBot working
around GRVT's 80-order cap). Paper by default — live GRVT execution needs the
user's GRVT keys and is a separate step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import ccxt.async_support as ccxt

GRID_PRICE_EXCHANGE = os.getenv("GRVT_PRICE_EXCHANGE", "binance")


@dataclass
class Fill:
    side: str
    price: float
    qty: float
    pnl: float
    at: str


@dataclass
class GridBot:
    running: bool = False
    mode: str = "paper"
    pair: str = "BTC/USDT"
    lower: float = 0.0
    upper: float = 0.0
    levels: int = 0
    capital: float = 1000.0
    cash: float = 1000.0
    position: float = 0.0
    realized: float = 0.0
    last_price: float = 0.0
    grid: list[float] = field(default_factory=list)
    held: list[bool] = field(default_factory=list)
    qty: list[float] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    note: str = "Paper grid engine modeled on GRVTBot. Live GRVT needs your keys."

    def configure(self, pair: str, lower: float, upper: float, levels: int, capital: float) -> None:
        if upper <= lower or lower <= 0:
            raise ValueError("upper must be > lower > 0")
        if levels < 2:
            raise ValueError("levels must be >= 2")
        if capital <= 0:
            raise ValueError("capital must be > 0")
        self.pair = pair.upper()
        self.lower = float(lower)
        self.upper = float(upper)
        self.levels = int(levels)
        self.capital = float(capital)
        step = (self.upper - self.lower) / (self.levels - 1)
        self.grid = [round(self.lower + i * step, 8) for i in range(self.levels)]
        self.held = [False] * (self.levels - 1)
        self.qty = [0.0] * (self.levels - 1)
        self.cash = self.capital
        self.position = 0.0
        self.realized = 0.0
        self.fills = []
        self.equity_curve = []
        self.running = False

    def start(self) -> None:
        if not self.grid:
            raise ValueError("configure the grid first")
        self.running = True

    def stop(self) -> None:
        self.running = False

    def step(self, price: float) -> None:
        """Advance the grid against a new price. Classic grid: buy a slot when
        price reaches its lower level, sell when price reaches the upper level."""
        if not self.running or not self.grid or price <= 0:
            return
        notional = self.capital / max(self.levels - 1, 1)
        for i in range(self.levels - 1):
            low, high = self.grid[i], self.grid[i + 1]
            if not self.held[i] and price <= low:
                q = notional / low
                self.cash -= notional
                self.position += q
                self.held[i] = True
                self.qty[i] = q
                self._fill("buy", low, q, 0.0)
            elif self.held[i] and price >= high:
                q = self.qty[i]
                pnl = (high - low) * q
                self.cash += q * high
                self.position -= q
                self.realized += pnl
                self.held[i] = False
                self.qty[i] = 0.0
                self._fill("sell", high, q, pnl)
        self.last_price = price
        self.equity_curve.append({"t": datetime.now(UTC).isoformat(), "v": round(self.equity(price), 2)})
        del self.equity_curve[:-200]

    def _fill(self, side: str, price: float, qty: float, pnl: float) -> None:
        self.fills.append(
            Fill(side=side, price=round(price, 8), qty=round(qty, 8), pnl=round(pnl, 4), at=datetime.now(UTC).isoformat())
        )
        del self.fills[:-50]

    def equity(self, price: float | None = None) -> float:
        px = price if price is not None else self.last_price
        return self.cash + self.position * (px or 0.0)

    def stats(self) -> dict:
        px = self.last_price
        active_slots = sum(1 for h in self.held if h)
        return {
            "running": self.running,
            "mode": self.mode,
            "connected": False,
            "pair": self.pair,
            "grid_lower": self.lower,
            "grid_upper": self.upper,
            "grid_levels": self.levels,
            "virtual_grids": True,
            "reinvest": True,
            "capital": round(self.capital, 2),
            "cash": round(self.cash, 2),
            "position": round(self.position, 8),
            "last_price": round(px, 8),
            "realized_pnl": round(self.realized, 4),
            "unrealized_pnl": round(self.equity(px) - self.capital - self.realized, 4) if px else 0.0,
            "equity": round(self.equity(px), 2) if px else round(self.capital, 2),
            "active_slots": active_slots,
            "grid": self.grid,
            "held": self.held,
            "fills": [f.__dict__ for f in reversed(self.fills[-12:])],
            "equity_curve": self.equity_curve,
            "note": self.note,
        }


async def fetch_ohlcv_for(pair: str, timeframe: str = "1h", limit: int = 168,
                          exchange_id: str | None = None) -> list[list[float]]:
    """Historical candles for the grid backtester (public, no keys)."""
    eid = exchange_id or GRID_PRICE_EXCHANGE
    if not hasattr(ccxt, eid):
        return []
    exchange = getattr(ccxt, eid)({"enableRateLimit": True})
    try:
        return await exchange.fetch_ohlcv(pair.upper(), timeframe=timeframe, limit=limit)
    except Exception:
        return []
    finally:
        await exchange.close()


def backtest(lower: float, upper: float, levels: int, capital: float,
             candles: list[list[float]], fee_pct: float = 0.05) -> dict:
    """Stateless grid backtest over historical candles. Ported from GRVTBot's
    runBacktest: walk each candle, fill levels within its range, realise the
    grid spread on each round trip (fee on both legs), track equity + drawdown.
    Spot long-only (leverage 1). Candle = [ts_ms, o, h, l, c, v]."""
    if not candles or upper <= lower or levels < 2 or capital <= 0:
        return {"net_profit": 0, "gross_profit": 0, "fees": 0, "roi_pct": 0,
                "round_trips": 0, "avg_per_trip": 0, "max_drawdown_pct": 0,
                "profit_factor": 0, "days": 0, "candles": 0, "equity_curve": []}
    n = levels
    spacing = (upper - lower) / n
    mid = (lower + upper) / 2
    qty = (capital * 0.75) / n / mid if mid > 0 else 0.0
    fee_rate = fee_pct / 100
    first_close = candles[0][4]
    lv = []
    for i in range(n + 1):
        price = lower + i * spacing
        lv.append({"i": i, "price": price, "side": "buy" if price < first_close else "sell",
                   "filled": False, "qty": qty})

    equity = capital
    hwm = equity
    maxdd = gp = gl = fees = 0.0
    trips = 0
    pos = cost = 0.0
    curve: list[dict] = []
    for c in candles:
        ts, hi, lo, close = c[0], c[2], c[3], c[4]
        for level in lv:
            if level["filled"]:
                continue
            hit = lo <= level["price"] if level["side"] == "buy" else hi >= level["price"]
            if not hit:
                continue
            level["filled"] = True
            if level["side"] == "buy":
                pos += level["qty"]
                cost += level["price"] * level["qty"]
            else:
                ci = level["i"] - 1
                if 0 <= ci < len(lv):
                    cl = lv[ci]
                    gross = (level["price"] - cl["price"]) * level["qty"]
                    fee = (cl["price"] + level["price"]) * level["qty"] * fee_rate
                    gp += gross if gross > 0 else 0
                    gl += abs(gross) if gross < 0 else 0
                    fees += fee
                    trips += 1
                    equity += gross - fee
                new_pos = max(0.0, pos - level["qty"])
                cost = cost * (new_pos / pos) if pos > 0 else 0.0
                pos = new_pos
            ci = level["i"] + 1 if level["side"] == "buy" else level["i"] - 1
            if 0 <= ci < len(lv):
                lv[ci]["filled"] = False
        unreal = pos * (close - (cost / pos)) if pos > 0 else 0.0
        cur = equity + unreal
        hwm = max(hwm, cur)
        dd = (hwm - cur) / hwm * 100 if hwm > 0 else 0
        maxdd = max(maxdd, dd)
        curve.append({"t": int(ts), "v": round(cur, 2)})

    net = gp - gl - fees
    days = max(1.0, (candles[-1][0] - candles[0][0]) / 86_400_000)
    return {
        "net_profit": round(net, 2),
        "gross_profit": round(gp, 2),
        "fees": round(fees, 2),
        "roi_pct": round(net / capital * 100, 2) if capital else 0,
        "round_trips": trips,
        "avg_per_trip": round(net / trips, 4) if trips else 0,
        "max_drawdown_pct": round(maxdd, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (999.0 if gp > 0 else 0),
        "days": round(days, 1),
        "candles": len(candles),
        "equity_curve": curve[-200:],
    }


async def fetch_price(pair: str, exchange_id: str | None = None) -> float:
    """Live reference price (paper). Uses the given public exchange, else default."""
    eid = exchange_id or GRID_PRICE_EXCHANGE
    if not hasattr(ccxt, eid):
        return 0.0
    exchange = getattr(ccxt, eid)({"enableRateLimit": True})
    try:
        ticker = await exchange.fetch_ticker(pair.upper())
        return float(ticker.get("last") or 0.0)
    except Exception:
        return 0.0
    finally:
        await exchange.close()


async def fetch_1m_volume(pair: str, exchange_id: str | None = None) -> float:
    """Base volume of the last CLOSED 1-minute candle. Used by the exit monitor's
    volume-aware time-stop: a flat move is only "dead" once its volume fades.
    0.0 on failure (caller treats unknown volume as no-signal). The last candle
    is still forming (partial volume), so we read the previous, fully-closed one."""
    eid = exchange_id or GRID_PRICE_EXCHANGE
    if not hasattr(ccxt, eid):
        return 0.0
    exchange = getattr(ccxt, eid)({"enableRateLimit": True})
    try:
        candles = await exchange.fetch_ohlcv(pair.upper(), timeframe="1m", limit=3)
        if not candles or len(candles) < 2:
            return 0.0
        return float(candles[-2][5] or 0.0)  # [ts,o,h,l,c,v] of the last closed 1m
    except Exception:
        return 0.0
    finally:
        await exchange.close()

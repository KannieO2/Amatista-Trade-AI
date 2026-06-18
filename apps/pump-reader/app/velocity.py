"""Real-time volume-acceleration entry trigger.

The 5-minute scan finds *candidates*; this watches the hottest ones on a fast
cadence (default 20s) and fires the entry the instant volume accelerates,
instead of waiting for the next slow scan. This is the source tool's "fire on
inflow" behaviour — don't wait for slow third-party confirmations.

Pre-fetching: each watched symbol keeps a primed baseline volume in memory, so a
tick is one cheap 1m-OHLCV call, not a full market sweep. Persistent CCXT
clients (markets loaded once) keep the loop fast.

Trigger rule (auditable, no ML):
  accel = last_closed_1m_volume / mean(prior_1m_volumes)
  fire when accel >= ACCEL_FACTOR AND price is rising AND symbol not on cooldown.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from statistics import mean

import ccxt.async_support as ccxt

VELOCITY_TIMEFRAME = os.getenv("PUMP_VELOCITY_TIMEFRAME", "1m")
VELOCITY_OHLCV_LIMIT = int(os.getenv("PUMP_VELOCITY_OHLCV_LIMIT", "8"))
ACCEL_FACTOR = float(os.getenv("PUMP_VELOCITY_ACCEL_FACTOR", "4"))   # x baseline volume
WATCH_TOP_N = int(os.getenv("PUMP_VELOCITY_WATCH_TOP_N", "8"))       # hottest N watched
WATCH_MIN_SCORE = int(os.getenv("PUMP_VELOCITY_WATCH_MIN_SCORE", "40"))
TRIGGER_COOLDOWN_SECONDS = int(os.getenv("PUMP_VELOCITY_COOLDOWN_SECONDS", "600"))


@dataclass
class WatchEntry:
    exchange: str
    symbol: str
    baseline_vol: float = 0.0      # mean prior 1m volume, primed (pre-fetched)
    last_bar_ts: float = 0.0       # ts of last closed bar we evaluated
    last_close: float = 0.0
    last_accel: float = 0.0
    primed: bool = False
    last_trigger_ts: float = 0.0


@dataclass
class TriggerEvent:
    exchange: str
    symbol: str
    accel: float
    price: float
    bar_volume: float
    at: float = field(default_factory=time.time)


class VelocityWatcher:
    """Watches a small hot-list of symbols and fires on volume acceleration."""

    def __init__(self) -> None:
        self.watch: dict[str, WatchEntry] = {}            # "exchange:symbol" -> entry
        self._clients: dict[str, ccxt.Exchange] = {}      # persistent per-exchange

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    async def _client(self, exchange_id: str):
        client = self._clients.get(exchange_id)
        if client is None:
            if not hasattr(ccxt, exchange_id):
                return None
            client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
            self._clients[exchange_id] = client
        return client

    async def _accel_metrics(self, exchange_id: str, symbol: str):
        """Return (baseline_mean, recent_vol, last_close, prev_close, bar_ts) or None."""
        client = await self._client(exchange_id)
        if client is None:
            return None
        try:
            ohlcv = await client.fetch_ohlcv(symbol, timeframe=VELOCITY_TIMEFRAME, limit=VELOCITY_OHLCV_LIMIT)
        except Exception:
            return None
        # Drop the in-progress (partial) final candle.
        rows = [r for r in ohlcv if r and r[5] is not None]
        if len(rows) < 4:
            return None
        closed = rows[:-1]
        recent = closed[-1]
        prior = closed[:-1]
        base = mean(v[5] for v in prior) if prior else 0.0
        return base, recent[5], recent[4], prior[-1][4], recent[0]

    async def sync(self, watch_list: list[tuple[str, str]]) -> None:
        """Refresh the hot-list from the latest scan. Adds + primes new entries,
        drops symbols no longer hot. watch_list = [(exchange, symbol), ...]."""
        wanted = {self._key(e, s): (e, s) for e, s in watch_list[:WATCH_TOP_N]}
        # Drop stale.
        for key in list(self.watch.keys()):
            if key not in wanted:
                del self.watch[key]
        # Add + prime new.
        for key, (exchange, symbol) in wanted.items():
            if key in self.watch:
                continue
            entry = WatchEntry(exchange=exchange, symbol=symbol)
            metrics = await self._accel_metrics(exchange, symbol)
            if metrics:
                base, _recent, last_close, _prev, bar_ts = metrics
                entry.baseline_vol = base
                entry.last_close = last_close
                entry.last_bar_ts = bar_ts
                entry.primed = True
            self.watch[key] = entry

    async def poll_once(self) -> list[TriggerEvent]:
        """One fast tick over the hot-list. Returns symbols that just accelerated."""
        triggers: list[TriggerEvent] = []
        now = time.time()
        # Snapshot the dict: sync() runs in a different task and may add/remove
        # symbols mid-poll → "dictionary changed size during iteration".
        for entry in list(self.watch.values()):
            metrics = await self._accel_metrics(entry.exchange, entry.symbol)
            if not metrics:
                continue
            base, recent_vol, last_close, prev_close, bar_ts = metrics
            # Re-prime baseline if we had none yet.
            if entry.baseline_vol <= 0:
                entry.baseline_vol = base
            ref_base = entry.baseline_vol if entry.baseline_vol > 0 else base
            accel = recent_vol / ref_base if ref_base > 0 else 0.0
            entry.last_accel = round(accel, 2)
            rising = last_close >= prev_close
            new_bar = bar_ts != entry.last_bar_ts
            cooled = now - entry.last_trigger_ts >= TRIGGER_COOLDOWN_SECONDS
            if accel >= ACCEL_FACTOR and rising and new_bar and cooled:
                entry.last_trigger_ts = now
                triggers.append(
                    TriggerEvent(
                        exchange=entry.exchange, symbol=entry.symbol,
                        accel=round(accel, 2), price=last_close, bar_volume=recent_vol,
                    )
                )
            entry.last_bar_ts = bar_ts
            entry.last_close = last_close
            # Slow-EMA the baseline so it tracks regime, but stays mostly the prime.
            if base > 0:
                entry.baseline_vol = entry.baseline_vol * 0.8 + base * 0.2
        return triggers

    def status(self) -> dict:
        return {
            "timeframe": VELOCITY_TIMEFRAME,
            "accel_factor": ACCEL_FACTOR,
            "watch_top_n": WATCH_TOP_N,
            "watching": [
                {
                    "exchange": e.exchange,
                    "symbol": e.symbol,
                    "baseline_vol": round(e.baseline_vol, 4),
                    "last_accel": e.last_accel,
                    "primed": e.primed,
                }
                for e in list(self.watch.values())
            ],
        }

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()


def watch_list_from_scores(items: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """Pick the hottest watchable symbols: score >= WATCH_MIN_SCORE, by score desc.
    items = [(exchange, symbol, pump_score), ...]."""
    hot = [(e, s) for e, s, score in sorted(items, key=lambda x: x[2], reverse=True) if score >= WATCH_MIN_SCORE]
    return hot[:WATCH_TOP_N]

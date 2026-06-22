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

from .websocket_manager import get_manager

VELOCITY_TIMEFRAME = os.getenv("PUMP_VELOCITY_TIMEFRAME", "1m")
VELOCITY_OHLCV_LIMIT = int(os.getenv("PUMP_VELOCITY_OHLCV_LIMIT", "8"))
ACCEL_FACTOR = float(os.getenv("PUMP_VELOCITY_ACCEL_FACTOR", "4"))   # x baseline volume
# Anti-late guard: only fire when price is within this % of the recent window high
# (= the FRONT of the move). Past it the move already topped/faded — buying there is
# the "entró tarde" dead-cat (e.g. W bought at 0.01029 after spiking to 0.011).
MAX_BELOW_HIGH_PCT = float(os.getenv("PUMP_VELOCITY_MAX_BELOW_HIGH", "3"))
# --- COIL / SQUEEZE pre-breakout PREDICTOR (gainers anticipation) ---------------
# Predict the arranque instead of chasing it: a token whose candle RANGE has
# CONTRACTED (a coiled spring) while VOLUME quietly BUILDS is loading for a pop.
# Firing on the FIRST acceleration of a coiled token catches the launch EARLY,
# where waiting for the full ACCEL_FACTOR (4x) only catches it mid-move (= late).
COIL_CONTRACTION = float(os.getenv("PUMP_COIL_CONTRACTION", "0.7"))  # last range <= 70% of prior mean = coiled
COIL_VOL_BUILD = float(os.getenv("PUMP_COIL_VOL_BUILD", "1.3"))      # vol >= 1.3x base = quietly building
COIL_ACCEL = float(os.getenv("PUMP_COIL_ACCEL", "2.0"))             # lower accel suffices WHEN coiled
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
    kind: str = "momentum"   # "coil" = predicted pre-breakout | "momentum" = move already going
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
        """Return (baseline_mean, recent_vol, last_close, prev_close, bar_ts, win_high,
        coiled) or None. coiled = volatility contracted + volume building = a spring
        loading for a breakout (the gainers PREDICTOR)."""
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
        win_high = max(r[2] for r in closed)   # highest high of the closed window
        # COIL detection: candle range (high-low) of the last bar vs the prior mean.
        # A contraction (range shrinks) WHILE volume builds = a coiled spring.
        ranges = [(r[2] - r[3]) for r in closed]   # high - low per closed bar
        recent_range = ranges[-1]
        prior_range = mean(ranges[:-1]) if len(ranges) > 1 else recent_range
        contracting = prior_range > 0 and recent_range <= prior_range * COIL_CONTRACTION
        vol_building = base > 0 and (base * COIL_VOL_BUILD) <= recent[5] < (base * ACCEL_FACTOR)
        coiled = bool(contracting and vol_building)
        return base, recent[5], recent[4], prior[-1][4], recent[0], win_high, coiled

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
                base, _recent, last_close, _prev, bar_ts, _high, _coiled = metrics
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
            base, recent_vol, last_close, prev_close, bar_ts, win_high, coiled = metrics
            # Prefer the live WebSocket price for the rising/trigger check (sub-second);
            # volume acceleration stays on the OHLCV (WS tickers don't give 1m volume).
            # Falls back to the OHLCV close when the WS cache is empty.
            try:
                ws_px = get_manager().get_price(entry.exchange, entry.symbol)
            except Exception:
                ws_px = None
            if ws_px and ws_px > 0:
                last_close = ws_px
            # Re-prime baseline if we had none yet.
            if entry.baseline_vol <= 0:
                entry.baseline_vol = base
            ref_base = entry.baseline_vol if entry.baseline_vol > 0 else base
            accel = recent_vol / ref_base if ref_base > 0 else 0.0
            entry.last_accel = round(accel, 2)
            rising = last_close >= prev_close
            new_bar = bar_ts != entry.last_bar_ts
            cooled = now - entry.last_trigger_ts >= TRIGGER_COOLDOWN_SECONDS
            # Anti-late: price must be near the recent high (front of the move), not
            # bought on the fade after the spike already topped.
            near_high = win_high <= 0 or (win_high - last_close) / win_high * 100 <= MAX_BELOW_HIGH_PCT
            # PREDICTOR: a coiled spring needs only COIL_ACCEL (the FIRST push) to fire
            # — that catches the arranque early. Otherwise wait for full ACCEL_FACTOR.
            pre_breakout = coiled and accel >= COIL_ACCEL
            fired = accel >= ACCEL_FACTOR or pre_breakout
            if fired and rising and new_bar and cooled and near_high:
                entry.last_trigger_ts = now
                triggers.append(
                    TriggerEvent(
                        exchange=entry.exchange, symbol=entry.symbol,
                        accel=round(accel, 2), price=last_close, bar_volume=recent_vol,
                        kind="coil" if (pre_breakout and accel < ACCEL_FACTOR) else "momentum",
                    )
                )
            entry.last_bar_ts = bar_ts
            entry.last_close = last_close
            # Slow-EMA the baseline toward the QUIET regime ONLY. During a build-up
            # `base` (mean of the prior window) creeps up, which would raise the very
            # bar the accel must clear → it suppresses the signal it exists to catch.
            # Adapt only when volume is near baseline (accel < 1.5 = calm); FREEZE the
            # primed baseline while a build is underway so the trigger stays reachable.
            if base > 0 and accel < 1.5:
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

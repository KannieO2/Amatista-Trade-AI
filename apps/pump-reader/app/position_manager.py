"""Two-phase exit + dump detector for paper pump positions.

Addresses the #1 gap: the bot used to only buy. This manages each open position
so it is never the exit liquidity:

  Phase 1 (secure capital): at +TP1% sell TP1_FRAC (default 60%).
  Phase 2 (let it run): remainder rides a trailing stop off the peak.
  Dump detector: an abrupt one-tick drop panic-sells the remainder at market.
  Hard stop: a loss past -HARD_STOP% sells everything.

Entry quality is graded (early/perfect/late) by comparing entry time/price to
the peak — feeds the learning loop so the bot gets more sensitive after late
entries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

# TP1 15 (was 30): the bot enters mid-move, so the residual upside is usually
# modest — a +30% first target rarely triggered, leaving winners to be scratched
# at break-even. Banking 60% at +15% turns moderate pumps into real wins (the
# Monte Carlo lifted expectancy ~+0.7%/trade). TRAIL 10 (was 12) tightens give-back.
TP1_PCT = float(os.getenv("PUMP_TP1_PCT", "15"))          # phase-1 take-profit trigger
TP1_FRAC = float(os.getenv("PUMP_TP1_FRAC", "0.6"))        # fraction sold in phase 1
TRAIL_PCT = float(os.getenv("PUMP_TRAIL_PCT", "10"))       # phase-2 trailing stop off peak
HARD_STOP_PCT = float(os.getenv("PUMP_STOP_LOSS_PCT", "8"))  # hard stop loss
DUMP_TICK_PCT = float(os.getenv("PUMP_DUMP_TICK_PCT", "10"))  # abrupt one-tick drop = dump

# Dynamic risk management.
TIMEOUT_MINUTES = float(os.getenv("PUMP_TIMEOUT_MINUTES", "8"))    # earliest a faded flat move is cut
TIMEOUT_BAND_PCT = float(os.getenv("PUMP_TIMEOUT_BAND_PCT", "3"))  # lateral = |gain| <= band
BREAKEVEN_PCT = float(os.getenv("PUMP_BREAKEVEN_PCT", "4"))        # gain that arms break-even
BREAKEVEN_MARGIN_PCT = float(os.getenv("PUMP_BREAKEVEN_MARGIN_PCT", "0.5"))  # SL above entry
# Volume-aware (dynamic) time-stop: a sideways pump with LIVE volume keeps its
# capital; only a flat move whose volume has FADED is cut. "Alive" = the latest
# 1m volume is still >= VOLUME_ALIVE_FRAC of the peak 1m volume seen in the trade.
VOLUME_ALIVE_FRAC = float(os.getenv("PUMP_VOLUME_ALIVE_FRAC", "0.5"))
# When no volume signal is available, fall back to a (longer) plain time-stop.
TIMEOUT_NO_VOL_MINUTES = float(os.getenv("PUMP_TIMEOUT_NO_VOL_MINUTES", "20"))
# Hard backstop: cap the hold even if volume persists but price goes nowhere.
MAX_HOLD_MINUTES = float(os.getenv("PUMP_MAX_HOLD_MINUTES", "45"))

# --- Cluster-aware exit profiles --------------------------------------------
# long_pump and classic are DIFFERENT setups → different trade management:
#   long_pump (buyer impulse / parabolic): run the spike, TIGHT trail, FAST cut,
#             sensitive dump detector — the move is violent and round-trips fast.
#   classic   (short-squeeze grind): modest TP banked early, LOOSE trail so the
#             grind isn't shaken out, PATIENT time-stop, tighter hard stop.
# DEFAULT_PROFILE = the base env constants (used by accumulation / n.a. entries,
# whose breakout character isn't known yet). Per-cluster keys override it.
DEFAULT_PROFILE = {
    "tp1_pct": TP1_PCT, "tp1_frac": TP1_FRAC, "trail_pct": TRAIL_PCT,
    "hard_stop_pct": HARD_STOP_PCT, "dump_tick_pct": DUMP_TICK_PCT,
    "timeout_min": TIMEOUT_MINUTES, "max_hold_min": MAX_HOLD_MINUTES,
}
CLUSTER_PROFILES = {
    "long_pump": {"tp1_pct": 25, "tp1_frac": 0.5, "trail_pct": 8, "hard_stop_pct": 8,
                  "dump_tick_pct": 9, "timeout_min": 6, "max_hold_min": 30},
    "classic":   {"tp1_pct": 10, "tp1_frac": 0.7, "trail_pct": 14, "hard_stop_pct": 6,
                  "dump_tick_pct": 12, "timeout_min": 12, "max_hold_min": 60},
}


def exit_profile(cluster: str) -> dict:
    """Lee variables de entorno en tiempo real para TP/SL/Timeout (así el
    auto-optimizer de 24h puede ajustarlas mutando os.environ sin reiniciar)."""
    base = {
        "tp1_pct": float(os.getenv("PUMP_TAKE_PROFIT_PCT", "35")),
        "tp1_frac": float(os.getenv("PUMP_TP1_FRAC", "0.6")),
        "trail_pct": float(os.getenv("PUMP_TRAIL_PCT", "5")),
        "hard_stop_pct": float(os.getenv("PUMP_STOP_LOSS_PCT", "2.5")),
        "dump_tick_pct": float(os.getenv("PUMP_DUMP_TICK_PCT", "8")),
        "timeout_min": float(os.getenv("PUMP_TIMEOUT_MINUTES", "15")),
        "max_hold_min": float(os.getenv("PUMP_MAX_HOLD_MINUTES", "60")),
    }
    return {**base}


@dataclass
class ManagedPosition:
    symbol: str
    exchange: str
    entry_price: float
    qty: float
    initial_qty: float
    entry_at: datetime
    peak_price: float
    peak_at: datetime
    phase: int = 1
    realized_pnl: float = 0.0
    last_price: float = 0.0
    closed: bool = False
    pump_score: int = 0
    classification: str = "n/a"
    cluster: str = "n/a"          # long_pump | classic | accumulation → exit profile
    be_armed: bool = False        # break-even stop activated (gain crossed BREAKEVEN_PCT)
    be_stop: float = 0.0          # break-even stop price (entry + margin)
    peak_volume: float = 0.0      # max 1m volume seen during the trade (fuel gauge)
    last_volume: float = 0.0      # latest 1m volume (vs peak → alive / faded)


@dataclass
class ExitEvent:
    symbol: str
    exchange: str
    reason: str
    sold_qty: float
    price: float
    pnl: float
    fraction: float
    closed: bool
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class PositionManager:
    def __init__(self) -> None:
        self.positions: dict[str, ManagedPosition] = {}
        self.history: list[ExitEvent] = []

    def key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def has(self, exchange: str, symbol: str) -> bool:
        pos = self.positions.get(self.key(exchange, symbol))
        return pos is not None and not pos.closed

    def open(self, *, symbol: str, exchange: str, entry_price: float, qty: float,
             pump_score: int = 0, classification: str = "n/a", cluster: str = "n/a",
             now: datetime | None = None) -> None:
        if entry_price <= 0 or qty <= 0:
            return
        now = now or datetime.now(UTC)
        self.positions[self.key(exchange, symbol)] = ManagedPosition(
            symbol=symbol, exchange=exchange, entry_price=entry_price, qty=qty,
            initial_qty=qty, entry_at=now, peak_price=entry_price, peak_at=now,
            last_price=entry_price, pump_score=pump_score, classification=classification,
            cluster=cluster,
        )

    def step(self, key: str, price: float, volume: float | None = None,
             now: datetime | None = None) -> list[ExitEvent]:
        pos = self.positions.get(key)
        if not pos or pos.closed or price <= 0:
            return []
        now = now or datetime.now(UTC)
        prev = pos.last_price or pos.entry_price
        pos.last_price = price
        if price > pos.peak_price:
            pos.peak_price = price
            pos.peak_at = now
        if volume is not None and volume > 0:
            pos.last_volume = volume
            if volume > pos.peak_volume:
                pos.peak_volume = volume

        gain = (price - pos.entry_price) / pos.entry_price * 100
        drop_from_peak = (pos.peak_price - price) / pos.peak_price * 100 if pos.peak_price > 0 else 0
        tick_drop = (prev - price) / prev * 100 if prev > 0 else 0
        elapsed_min = (now - pos.entry_at).total_seconds() / 60

        # Cluster-aware management: long_pump rides tight/fast, classic grinds
        # patient/loose (see CLUSTER_PROFILES). Falls back to base env constants.
        p = exit_profile(pos.cluster)

        events: list[ExitEvent] = []
        # Hard stop first (capital protection priority).
        if gain <= -p["hard_stop_pct"]:
            events.append(self._sell(pos, price, 1.0, "hard_stop"))
            return events
        # Dump detector: abrupt one-tick collapse -> panic sell the rest.
        if tick_drop >= p["dump_tick_pct"]:
            events.append(self._sell(pos, price, 1.0, "dump"))
            return events
        # Break-even: once gain crossed +BREAKEVEN_PCT, the stop moves to entry +
        # margin. Falling back to it locks the trade at ~breakeven (no give-back).
        if not pos.be_armed and gain >= BREAKEVEN_PCT:
            pos.be_armed = True
            pos.be_stop = pos.entry_price * (1 + BREAKEVEN_MARGIN_PCT / 100)
        if pos.be_armed and price <= pos.be_stop:
            events.append(self._sell(pos, price, 1.0, "break_even"))
            return events
        # Dynamic time-stop. A flat move (|gain| <= band) is NOT cut just for being
        # slow — only when its FUEL is gone. While 1m volume stays alive (>= frac of
        # peak), a sideways pump keeps running; once volume fades, free the capital.
        if self._time_stop_fires(pos, gain, elapsed_min, p):
            events.append(self._sell(pos, price, 1.0, "timeout"))
            return events
        # Phase 1: secure capital with a partial take-profit.
        if pos.phase == 1 and gain >= p["tp1_pct"]:
            events.append(self._sell(pos, price, p["tp1_frac"], "tp1"))
            pos.phase = 2
        # Phase 2: trailing stop on the remainder.
        if pos.phase == 2 and not pos.closed and drop_from_peak >= p["trail_pct"]:
            events.append(self._sell(pos, price, 1.0, "trailing"))
        return events

    def _time_stop_fires(self, pos: ManagedPosition, gain: float, elapsed_min: float,
                         p: dict | None = None) -> bool:
        """Volume-aware time-stop. Returns True only for a flat move that should be
        cut. Logic:
          - not lateral (|gain| > band)            -> never (let TP/trail/stop run)
          - volume FADED + past TIMEOUT_MINUTES     -> cut (dead move)
          - no volume data + past NO_VOL_MINUTES    -> cut (longer fallback grace)
          - volume ALIVE                            -> hold, until MAX_HOLD backstop
        """
        p = p or exit_profile(pos.cluster)
        if abs(gain) > TIMEOUT_BAND_PCT:
            return False
        have_vol = pos.peak_volume > 0 and pos.last_volume > 0
        if have_vol:
            faded = pos.last_volume < VOLUME_ALIVE_FRAC * pos.peak_volume
            if faded and elapsed_min >= p["timeout_min"]:
                return True            # flat + fuel gone = dead
            if elapsed_min >= p["max_hold_min"]:
                return True            # backstop: capped even if volume persists
            return False               # alive volume -> keep the sideways pump
        # No volume signal: fall back to a plain (longer) time-stop.
        return elapsed_min >= TIMEOUT_NO_VOL_MINUTES

    def _sell(self, pos: ManagedPosition, price: float, fraction: float, reason: str) -> ExitEvent:
        sell_qty = pos.qty if fraction >= 1.0 else pos.qty * fraction
        pnl = (price - pos.entry_price) * sell_qty
        pos.qty -= sell_qty
        pos.realized_pnl += pnl
        closed = pos.qty <= 1e-12
        pos.closed = pos.closed or closed
        event = ExitEvent(
            symbol=pos.symbol, exchange=pos.exchange, reason=reason,
            sold_qty=round(sell_qty, 8), price=round(price, 8), pnl=round(pnl, 4),
            fraction=round(fraction, 3), closed=closed,
        )
        self.history.append(event)
        del self.history[:-100]
        return event

    def entry_quality(self, pos: ManagedPosition) -> str:
        """Grade the entry vs the peak to feed the learning loop."""
        secs_to_peak = (pos.peak_at - pos.entry_at).total_seconds()
        peak_gain = (pos.peak_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
        if peak_gain < 5 or secs_to_peak < 60:
            return "late_entry"        # bought near the top — barely ran after entry
        if peak_gain >= 30:
            return "early_entry"
        return "perfect_entry"

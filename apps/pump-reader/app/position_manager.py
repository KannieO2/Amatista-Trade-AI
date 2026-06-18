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

TP1_PCT = float(os.getenv("PUMP_TP1_PCT", "30"))          # phase-1 take-profit trigger
TP1_FRAC = float(os.getenv("PUMP_TP1_FRAC", "0.6"))        # fraction sold in phase 1
TRAIL_PCT = float(os.getenv("PUMP_TRAIL_PCT", "12"))       # phase-2 trailing stop off peak
HARD_STOP_PCT = float(os.getenv("PUMP_STOP_LOSS_PCT", "8"))  # hard stop loss
DUMP_TICK_PCT = float(os.getenv("PUMP_DUMP_TICK_PCT", "10"))  # abrupt one-tick drop = dump

# Dynamic risk management.
TIMEOUT_MINUTES = float(os.getenv("PUMP_TIMEOUT_MINUTES", "8"))    # time-stop window
TIMEOUT_BAND_PCT = float(os.getenv("PUMP_TIMEOUT_BAND_PCT", "3"))  # lateral = |gain| <= band
BREAKEVEN_PCT = float(os.getenv("PUMP_BREAKEVEN_PCT", "4"))        # gain that arms break-even
BREAKEVEN_MARGIN_PCT = float(os.getenv("PUMP_BREAKEVEN_MARGIN_PCT", "0.5"))  # SL above entry


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
    be_armed: bool = False        # break-even stop activated (gain crossed BREAKEVEN_PCT)
    be_stop: float = 0.0          # break-even stop price (entry + margin)


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
             pump_score: int = 0, classification: str = "n/a") -> None:
        if entry_price <= 0 or qty <= 0:
            return
        now = datetime.now(UTC)
        self.positions[self.key(exchange, symbol)] = ManagedPosition(
            symbol=symbol, exchange=exchange, entry_price=entry_price, qty=qty,
            initial_qty=qty, entry_at=now, peak_price=entry_price, peak_at=now,
            last_price=entry_price, pump_score=pump_score, classification=classification,
        )

    def step(self, key: str, price: float) -> list[ExitEvent]:
        pos = self.positions.get(key)
        if not pos or pos.closed or price <= 0:
            return []
        prev = pos.last_price or pos.entry_price
        pos.last_price = price
        if price > pos.peak_price:
            pos.peak_price = price
            pos.peak_at = datetime.now(UTC)

        gain = (price - pos.entry_price) / pos.entry_price * 100
        drop_from_peak = (pos.peak_price - price) / pos.peak_price * 100 if pos.peak_price > 0 else 0
        tick_drop = (prev - price) / prev * 100 if prev > 0 else 0
        elapsed_min = (datetime.now(UTC) - pos.entry_at).total_seconds() / 60

        events: list[ExitEvent] = []
        # Hard stop first (capital protection priority).
        if gain <= -HARD_STOP_PCT:
            events.append(self._sell(pos, price, 1.0, "hard_stop"))
            return events
        # Dump detector: abrupt one-tick collapse -> panic sell the rest.
        if tick_drop >= DUMP_TICK_PCT:
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
        # Time-stop: after TIMEOUT_MINUTES going nowhere (|gain| <= band), free the
        # capital for a better setup instead of bag-holding a dead move.
        if elapsed_min >= TIMEOUT_MINUTES and abs(gain) <= TIMEOUT_BAND_PCT:
            events.append(self._sell(pos, price, 1.0, "timeout"))
            return events
        # Phase 1: secure capital with a partial take-profit.
        if pos.phase == 1 and gain >= TP1_PCT:
            events.append(self._sell(pos, price, TP1_FRAC, "tp1"))
            pos.phase = 2
        # Phase 2: trailing stop on the remainder.
        if pos.phase == 2 and not pos.closed and drop_from_peak >= TRAIL_PCT:
            events.append(self._sell(pos, price, 1.0, "trailing"))
        return events

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

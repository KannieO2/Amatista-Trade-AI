"""Capital allocation: total bot capital + per-exchange split.

Position size for a trade = the venue's cap = bot_total * (split% / 100).
Mirrors the source tool's "Capital allocation" dialog. Paper by default.
"""

from __future__ import annotations

import os


def _exec_exchanges() -> list[str]:
    raw = os.getenv("PUMP_EXEC_EXCHANGES", "binance,mexc,bitget")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


class Allocation:
    def __init__(self) -> None:
        self.bot_total_usdt = float(os.getenv("PUMP_PAPER_BALANCE", "1000"))
        exchanges = _exec_exchanges()
        even = round(100.0 / len(exchanges), 2)
        self.splits: dict[str, float] = {e: even for e in exchanges}
        # absorb rounding drift into the first venue so the sum is exactly 100
        self.splits[exchanges[0]] += round(100.0 - sum(self.splits.values()), 2)

    def cap_for(self, exchange: str) -> float:
        return self.bot_total_usdt * self.splits.get(exchange.lower(), 0.0) / 100.0

    def sum_pct(self) -> float:
        return round(sum(self.splits.values()), 2)

    def is_valid(self) -> bool:
        return abs(self.sum_pct() - 100.0) < 0.5

    def update(self, bot_total_usdt: float, splits: dict[str, float]) -> None:
        if bot_total_usdt > 0:
            self.bot_total_usdt = float(bot_total_usdt)
        if splits:
            self.splits = {k.lower(): float(v) for k, v in splits.items()}


_state = Allocation()


def get_state() -> Allocation:
    return _state

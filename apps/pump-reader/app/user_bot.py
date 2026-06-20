"""Per-user bot state — the registry that makes each account its OWN independent
bot (own balance, positions, risk, equity, P&L) while the market scan and the
scam-pump learning stay GLOBAL (the shared brain).

main.py keeps the global pieces (scanner, velocity watcher, learning lab,
candidates) at module level and fans the loops out over every user's bot here;
the dashboard endpoints scope to the logged-in user via get_bot(request).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from .executor import ExecutionEngine
from .position_manager import PositionManager
from .risk import RiskGuard

PAPER_BALANCE = float(os.getenv("PUMP_PAPER_BALANCE", "1000"))
AUTO_ENTRY_DEFAULT = os.getenv("PUMP_AUTO_ENTRY", "true").lower() == "true"
AUTO_ENTRY_USD_DEFAULT = float(os.getenv("PUMP_AUTO_ENTRY_USD", "100"))


def default_allocation() -> dict:
    return {"bot_total_usdt": PAPER_BALANCE,
            "splits": {"binance": 25.0, "bitget": 25.0, "mexc": 25.0, "okx": 25.0}}


class UserBot:
    """One account's independent trading state. The market data + learning that
    feed it are shared (passed in by the caller); everything here is private."""

    def __init__(self, uid: str):
        self.uid = uid
        self.guard = RiskGuard()
        self.engine = ExecutionEngine(self.guard)
        self.pm = PositionManager()
        self.equity_history: list[dict] = []
        self.allocation: dict = default_allocation()
        # Per-user trading preferences (each account controls its OWN bot). The
        # confirmation threshold stays global (shared brain) — see main.py.
        self.auto_entry: bool = AUTO_ENTRY_DEFAULT
        self.auto_entry_usd: float = AUTO_ENTRY_USD_DEFAULT
        # Real read-only balance — only populated for an account whose exchange
        # keys are set (today: the owner). Others stay paper (has_keys=False).
        self.real_account: dict = {"has_keys": False, "total_usdt": 0.0, "connected": [], "snapshots": []}
        # Realized P&L from PREVIOUS sessions (loaded at startup from exit_events),
        # so the paper balance + equity curve don't reset to the base capital on a
        # restart. pm.history holds only THIS session's exits → no double count.
        self.realized_carry: float = 0.0
        self.carry_exits: list[dict] = []   # recent {"at", "pnl"} for the 7d figure

    def open_count(self) -> int:
        """Live OPEN managed positions (drives the max-open-trades risk cap)."""
        return sum(1 for p in list(self.pm.positions.values()) if not p.closed)

    def paper_equity(self) -> float:
        """Paper balance = allocated capital + realized (carried across restarts +
        this session) + unrealized demo gains."""
        base = float(self.allocation.get("bot_total_usdt") or PAPER_BALANCE)
        realized = self.realized_carry + sum(e.pnl for e in self.pm.history)
        unrealized = sum(
            (p.last_price - p.entry_price) * p.qty
            for p in self.pm.positions.values()
            if not p.closed and p.last_price > 0
        )
        return round(base + realized + unrealized, 2)

    def pnl_7d(self) -> float:
        """Realized exits over the last 7 days + open unrealized."""
        cutoff = datetime.now(UTC).timestamp() - 7 * 86400
        realized = 0.0
        for e in self.pm.history:
            try:
                ts = datetime.fromisoformat(e.at).timestamp()
            except Exception:
                ts = cutoff  # undated event → still count it
            if ts >= cutoff:
                realized += e.pnl
        # Plus exits carried from previous sessions that fall inside the window.
        for ce in self.carry_exits:
            try:
                ts = datetime.fromisoformat(ce["at"]).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                realized += float(ce.get("pnl") or 0.0)
        unrealized = sum(
            (p.last_price - p.entry_price) * p.qty
            for p in self.pm.positions.values()
            if not p.closed and p.last_price > 0
        )
        return round(realized + unrealized, 2)

    def balance(self) -> float:
        """What the dashboard shows: live balance when keys exist, else paper."""
        return self.real_account["total_usdt"] if self.real_account.get("has_keys") else self.paper_equity()


# uid -> UserBot. The owner uses the sentinel "owner" (matches auth.OWNER_UID).
_bots: dict[str, UserBot] = {}


def get_bot(uid: str | None) -> UserBot:
    uid = str(uid or "owner")
    bot = _bots.get(uid)
    if bot is None:
        bot = UserBot(uid)
        _bots[uid] = bot
    return bot


def all_bots() -> list[UserBot]:
    return list(_bots.values())


def ensure_bots(uids: list[str]) -> None:
    for u in uids:
        get_bot(u)

"""Risk guard for the execution layer.

No order — paper or live — is placed without passing this guard. It prefers the
canonical Risk Engine service when RISK_ENGINE_URL is set, and fails closed for
live trades when that service is unreachable. The local mirror enforces the same
caps so the rules hold even before the service is wired.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

RISK_ENGINE_URL = os.getenv("RISK_ENGINE_URL")  # e.g. http://risk-engine:8000


@dataclass
class RiskLimits:
    max_daily_loss_usd: float = 250.0
    max_drawdown_pct: float = 5.0
    max_position_size_usd: float = 500.0
    # Paper demo is forgiving (manual "Act" + auto-entry share this cap); tune via
    # PUMP_MAX_OPEN_TRADES. Keep it low for live.
    max_open_trades: int = int(os.getenv("PUMP_MAX_OPEN_TRADES", "12"))
    max_leverage: float = 2.0


@dataclass
class RiskContext:
    position_size_usd: float
    leverage: float = 1.0
    open_trades: int = 0
    daily_loss_usd: float = 0.0
    current_drawdown_pct: float = 0.0


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    kill_switch_active: bool = False


class RiskGuard:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.kill_switch = False
        self.kill_reason = "inactive"

    def set_kill_switch(self, active: bool, reason: str = "manual") -> None:
        self.kill_switch = active
        self.kill_reason = reason if active else "inactive"

    def _local_eval(self, ctx: RiskContext) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, self.kill_reason, True)
        limits = self.limits
        if ctx.position_size_usd > limits.max_position_size_usd:
            return RiskDecision(False, "position size exceeds limit")
        if ctx.leverage > limits.max_leverage:
            return RiskDecision(False, "leverage exceeds limit")
        if ctx.open_trades >= limits.max_open_trades:
            return RiskDecision(False, "open trade limit reached")
        if ctx.daily_loss_usd >= limits.max_daily_loss_usd:
            return RiskDecision(False, "daily loss limit reached")
        if ctx.current_drawdown_pct >= limits.max_drawdown_pct:
            return RiskDecision(False, "drawdown limit reached")
        return RiskDecision(True, "risk checks passed")

    async def evaluate(self, ctx: RiskContext, *, live: bool) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, self.kill_reason, True)

        if RISK_ENGINE_URL:
            payload = {
                "product": "grvtbot_pro",  # execution role allowed to auto-trade
                "action": "execute_order",
                "position_size_usd": ctx.position_size_usd,
                "leverage": ctx.leverage,
                "open_trades": ctx.open_trades,
                "daily_loss_usd": ctx.daily_loss_usd,
                "current_drawdown_pct": ctx.current_drawdown_pct,
            }
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(f"{RISK_ENGINE_URL}/risk/evaluate", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    return RiskDecision(
                        bool(data["allowed"]),
                        str(data["reason"]),
                        bool(data["kill_switch_active"]),
                    )
            except Exception:
                if live:
                    return RiskDecision(False, "risk engine unreachable; live trade blocked")
                # paper mode may continue on the local mirror

        return self._local_eval(ctx)

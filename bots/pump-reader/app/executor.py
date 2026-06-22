"""Execution layer for the pump system.

Default mode is PAPER (no real money). Live trading is opt-in: it requires
PUMP_EXEC_MODE=live plus per-exchange API keys supplied by the user, and every
order still passes the RiskGuard + kill switch. API keys with withdrawal
permission must never be used here.

Capital is split across the configured exchanges (the source tool used MEXC and
Bitget). Each leg attaches stop-loss / take-profit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from .risk import RiskContext, RiskGuard


class ExecMode(StrEnum):
    paper = "paper"
    live = "live"


class Side(StrEnum):
    buy = "buy"
    sell = "sell"


class OrderType(StrEnum):
    market = "market"
    limit = "limit"


def current_mode() -> ExecMode:
    raw = os.getenv("PUMP_EXEC_MODE", "paper").lower()
    return ExecMode.live if raw == "live" else ExecMode.paper


def configured_exchanges() -> list[str]:
    raw = os.getenv("PUMP_EXEC_EXCHANGES", "mexc,bitget")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


# STOP_LOSS_PCT / TAKE_PROFIT_PCT removed as fixed exit params — exits are owned
# by position_manager's DYNAMIC trailing stop now (no 60/40, no fixed TP). They
# survive only inline in act() to attach a protective SL/TP to a LIVE reduce
# order; paper ignores them.
# STOP_LOSS_PCT = float(os.getenv("PUMP_STOP_LOSS_PCT", "8"))
# TAKE_PROFIT_PCT = float(os.getenv("PUMP_TAKE_PROFIT_PCT", "25"))
SLIPPAGE_PCT = float(os.getenv("PUMP_PAPER_SLIPPAGE_PCT", "0.5"))

# --- Iceberg (anti-slippage) ------------------------------------------------
# Si el notional de un leg supera ICEBERG_DEPTH_PCT% de la profundidad del libro,
# se parte en ICEBERG_SLICES órdenes consecutivas para no mover el precio en
# contra. En paper se MODELA el beneficio (slippage promedio ~/ sqrt(slices));
# en live se colocan N órdenes market hijas y se promedia el fill.
ICEBERG_DEPTH_PCT = float(os.getenv("PUMP_ICEBERG_DEPTH_PCT", "2.0"))
ICEBERG_SLICES = max(1, int(os.getenv("PUMP_ICEBERG_SLICES", "3")))


def _iceberg_slices(notional_usd: float, book_depth_usd: float | None) -> int:
    """N órdenes en que partir la entrada. 1 = sin iceberg."""
    if not book_depth_usd or book_depth_usd <= 0 or ICEBERG_SLICES <= 1:
        return 1
    if notional_usd > book_depth_usd * ICEBERG_DEPTH_PCT / 100.0:
        return ICEBERG_SLICES
    return 1


@dataclass
class OrderLeg:
    exchange: str
    symbol: str
    side: Side
    notional_usd: float
    order_type: OrderType
    entry_price: float
    stop_loss: float
    take_profit: float


@dataclass
class Fill:
    id: str
    exchange: str
    symbol: str
    side: Side
    notional_usd: float
    fill_price: float
    amount: float
    stop_loss: float
    take_profit: float
    mode: ExecMode
    created_at: datetime
    slices: int = 1  # nº de órdenes iceberg que componen este fill (1 = directo)


@dataclass
class ExecutionResult:
    symbol: str
    mode: ExecMode
    requested_usd: float
    fills: list[Fill] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


class PaperBroker:
    """Simulated fills off a provided reference price. No network, no money."""

    def place(self, leg: OrderLeg, slices: int = 1) -> Fill:
        # Iceberg: partir reduce el slippage promedio ~/ sqrt(slices) (regla de
        # microestructura: cada slice come menos profundidad). slices=1 → directo.
        eff_slip = SLIPPAGE_PCT / (slices ** 0.5) if slices > 1 else SLIPPAGE_PCT
        slip = 1 + eff_slip / 100 if leg.side == Side.buy else 1 - eff_slip / 100
        fill_price = leg.entry_price * slip
        amount = leg.notional_usd / fill_price if fill_price > 0 else 0.0
        return Fill(
            id=str(uuid4()),
            exchange=leg.exchange,
            symbol=leg.symbol,
            side=leg.side,
            notional_usd=leg.notional_usd,
            fill_price=round(fill_price, 8),
            amount=round(amount, 8),
            stop_loss=leg.stop_loss,
            take_profit=leg.take_profit,
            mode=ExecMode.paper,
            created_at=datetime.now(UTC),
            slices=slices,
        )


# Per-exchange API key env var names. Keys MUST be created without withdrawal
# permission (spot trading only). This code never calls any withdrawal method.
KEY_ENV = {
    "binance": ("BINANCE_API_KEY", "BINANCE_SECRET", None),
    "binanceus": ("BINANCEUS_API_KEY", "BINANCEUS_SECRET", None),
    "mexc": ("MEXC_API_KEY", "MEXC_SECRET", None),
    "bitget": ("BITGET_API_KEY", "BITGET_SECRET", "BITGET_PASSWORD"),
}


class LiveBroker:
    """Real spot orders via CCXT. Only reachable when PUMP_EXEC_MODE=live AND the
    user has supplied that exchange's API keys (no withdrawal permission).

    Places the market entry, then attempts best-effort take-profit / stop-loss
    reduce orders. SL/TP live enforcement is exchange-specific and must be
    verified per exchange before trading real size.
    """

    @staticmethod
    def _credentials(exchange_id: str) -> dict | None:
        mapping = KEY_ENV.get(exchange_id)
        if not mapping:
            return None
        api = os.getenv(mapping[0])
        secret = os.getenv(mapping[1])
        if not api or not secret:
            return None
        cfg = {"apiKey": api, "secret": secret, "enableRateLimit": True}
        if mapping[2]:
            password = os.getenv(mapping[2])
            if not password:
                return None
            cfg["password"] = password
        return cfg

    async def place(self, leg: OrderLeg, slices: int = 1) -> Fill:
        import ccxt.async_support as ccxt

        cfg = self._credentials(leg.exchange)
        if cfg is None:
            raise RuntimeError(
                f"live mode but no API keys for {leg.exchange}. Set its env keys "
                f"(spot, NO withdrawal permission) and restart."
            )
        if not hasattr(ccxt, leg.exchange):
            raise RuntimeError(f"exchange {leg.exchange} not supported by ccxt")

        client = getattr(ccxt, leg.exchange)(cfg)
        try:
            total_amount = leg.notional_usd / leg.entry_price if leg.entry_price > 0 else 0.0
            # Iceberg: N market hijas de igual tamaño; promedia precio+cantidad.
            n = max(1, slices)
            slice_amt = total_amount / n
            filled = 0.0
            cost = 0.0
            for _ in range(n):
                order = await client.create_order(leg.symbol, "market", leg.side.value, slice_amt)
                px = float(order.get("average") or order.get("price") or leg.entry_price)
                got = float(order.get("filled") or slice_amt)
                filled += got
                cost += px * got
            fill_price = (cost / filled) if filled > 0 else leg.entry_price
            amount = total_amount  # reportado por compatibilidad con el cálculo de TP abajo

            # Best-effort protective take-profit (reduce). Failures do not abort
            # the recorded entry, but must be hardened before real-size trading.
            try:
                await client.create_order(
                    leg.symbol, "limit", "sell", filled, leg.take_profit, {"reduceOnly": True}
                )
            except Exception:
                pass
            # Best-effort protective STOP-LOSS resting on the VENUE. The in-process
            # dynamic stop dies with the process; a real exchange-side stop means a
            # LIVE position is never left unprotected if the bot crashes. Uses ccxt's
            # unified `stopLossPrice` trigger; semantics vary per venue → best-effort,
            # MUST be verified per exchange before real-size trading (see class doc).
            try:
                await client.create_order(
                    leg.symbol, "market", "sell", filled, None,
                    {"reduceOnly": True, "stopLossPrice": leg.stop_loss},
                )
            except Exception:
                pass

            return Fill(
                id=str(uuid4()),
                exchange=leg.exchange,
                symbol=leg.symbol,
                side=leg.side,
                notional_usd=leg.notional_usd,
                fill_price=round(fill_price, 8),
                amount=round(filled, 8),
                stop_loss=leg.stop_loss,
                take_profit=leg.take_profit,
                mode=ExecMode.live,
                created_at=datetime.now(UTC),
                slices=n,
            )
        finally:
            await client.close()


class ExecutionEngine:
    def __init__(self, guard: RiskGuard) -> None:
        self.guard = guard
        self.paper = PaperBroker()
        self.live = LiveBroker()
        self.positions: list[Fill] = []

    async def act(
        self,
        *,
        symbol: str,
        side: Side,
        reference_price: float,
        capital_usd: float,
        exchanges: list[str] | None = None,
        order_type: OrderType = OrderType.market,
        open_trades: int | None = None,
        book_depth_usd: float | None = None,
    ) -> ExecutionResult:
        mode = current_mode()
        # Default: trade on the venue(s) where the token actually lists.
        exchanges = exchanges or configured_exchanges()
        result = ExecutionResult(symbol=symbol, mode=mode, requested_usd=capital_usd)

        if not exchanges or reference_price <= 0:
            result.rejected.append("no exchanges configured or invalid price")
            return result

        per_leg = capital_usd / len(exchanges)
        # Read inline (only used to protect a LIVE reduce order; exits owned by
        # position_manager's dynamic stop).
        sl = reference_price * (1 - float(os.getenv("PUMP_STOP_LOSS_PCT", "8")) / 100)
        tp = reference_price * (1 + float(os.getenv("PUMP_TAKE_PROFIT_PCT", "25")) / 100)
        # Caller passes the live OPEN-position count (lifetime fills would block
        # entries forever once the cap is hit). Fall back to lifetime fills.
        base_open = open_trades if open_trades is not None else len(self.positions)

        for idx, exchange in enumerate(exchanges):
            ctx = RiskContext(
                position_size_usd=per_leg,
                leverage=1.0,
                open_trades=base_open + idx,
            )
            decision = await self.guard.evaluate(ctx, live=mode == ExecMode.live)
            if not decision.allowed:
                result.rejected.append(f"{exchange}: {decision.reason}")
                continue

            leg = OrderLeg(
                exchange=exchange,
                symbol=symbol,
                side=side,
                notional_usd=per_leg,
                order_type=order_type,
                entry_price=reference_price,
                stop_loss=round(sl, 8),
                take_profit=round(tp, 8),
            )
            slices = _iceberg_slices(per_leg, book_depth_usd)
            try:
                fill = (await self.live.place(leg, slices) if mode == ExecMode.live
                        else self.paper.place(leg, slices))
            except Exception as exc:  # noqa: BLE001 - surface broker errors as rejections, never crash
                result.rejected.append(f"{exchange}: {exc}")
                continue
            self.positions.append(fill)
            result.fills.append(fill)

        return result

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
# Costo realista del fill (paper antes mentía: solo slippage de ENTRADA, sin fee, sin
# impacto). taker fee por lado + IMPACTO de mercado: una orden grande contra un libro
# thin mueve el precio en contra. Sin esto las "ganancias" en microcaps eran fantasía.
FEE_PCT = float(os.getenv("PUMP_PAPER_FEE_PCT", "0.1"))            # taker, por lado
IMPACT_COEF = float(os.getenv("PUMP_PAPER_IMPACT_COEF", "1.0"))    # impacto = coef·notional/profundidad
# LIMIT/maker entry cost — SOLO la tesis de ACUMULACIÓN (lead on-chain): no persigues,
# descansas un bid en el libro quieto → llenas cerca del mid con fee maker (mucho menor que
# el taker market). Baja el round-trip ~1.2%→~0.67% donde la tesis es comprar barato en
# quieto — que es donde el edge medido (~0.7-1%) SÍ puede superar el costo y dar VERDE. La
# SALIDA siempre es market taker (vendes el pump rápido, la maneja position_manager).
MAKER_SLIP_PCT = float(os.getenv("PUMP_PAPER_MAKER_SLIP_PCT", "0.05"))   # limit descansando ~ mid
MAKER_FEE_PCT = float(os.getenv("PUMP_PAPER_MAKER_FEE_PCT", "0.02"))     # maker fee, por lado


def market_impact_pct(notional_usd: float, book_depth_usd: float | None) -> float:
    """Slippage por IMPACTO de mercado en %: una orden grande vs un libro thin mueve el
    precio. coef·(notional/profundidad)·100. Sin libro/orden → 0. Compartido entry+exit."""
    if not book_depth_usd or book_depth_usd <= 0 or notional_usd <= 0:
        return 0.0
    return IMPACT_COEF * (notional_usd / book_depth_usd) * 100.0

# --- Iceberg (anti-slippage) ------------------------------------------------
# Si el notional de un leg supera ICEBERG_DEPTH_PCT% de la profundidad del libro,
# se parte en ICEBERG_SLICES órdenes consecutivas para no mover el precio en
# contra. En paper se MODELA el beneficio (slippage promedio ~/ sqrt(slices));
# en live se colocan N órdenes market hijas y se promedia el fill.
ICEBERG_DEPTH_PCT = float(os.getenv("PUMP_ICEBERG_DEPTH_PCT", "2.0"))
ICEBERG_SLICES = max(1, int(os.getenv("PUMP_ICEBERG_SLICES", "3")))


# --- Tope de costo de entrada -----------------------------------------------
# Medido sobre los 704 trades cerrados (2026-08-15): el costo de ENTRADA es lo
# que mejor separa ganadores de perdedores, mejor que score, régimen o setup.
#
#   costo <=0.1%     181 trades   -$192   22.7% aciertos   PF 0.380
#   costo 0.1-0.3%    13 trades     +$6   46.2% aciertos   PF 1.856
#   costo 0.3-0.6%    82 trades   -$125   17.1% aciertos   PF 0.271
#   costo 0.6-1.0%   354 trades   -$660    2.3% aciertos   PF 0.056
#   costo >1.0%       74 trades   -$195    2.7% aciertos   PF 0.061
#
# 428 trades (61%) entraron pagando >0.6% y perdieron $855 con 2.4% de aciertos.
# La MFE mediana de los perdedores es 0.00%: el precio nunca subió, así que ese
# costo no se recupera nunca. Cortar en 0.3% deja la pérdida histórica en -$186
# en vez de -$1165.
#
# OJO: esto NO vuelve rentable al bot — el mejor subconjunto sigue en PF 0.616.
# Es reducción de daño mientras la señal de entrada se arregla, que es el
# problema de fondo (ni las entradas más limpias tienen edge).
MAX_ENTRY_COST_PCT = float(os.getenv("PUMP_MAX_ENTRY_COST_PCT", "0.3"))


def estimated_entry_cost_pct(notional_usd: float, book_depth_usd: float | None,
                             slices: int = 1, maker: bool = False) -> float:
    """Costo estimado de cruzar el spread en una COMPRA, en %.

    Mismo modelo que PaperBroker.place: slippage base (maker o taker, dividido
    por sqrt(slices) si hay iceberg) + impacto de libro + fee del lado. Se
    calcula ANTES de mandar la orden para poder rechazarla, y por eso vale
    también en live: es una estimación previa, no una lectura del fill.
    """
    base = MAKER_SLIP_PCT if maker else SLIPPAGE_PCT
    fee = MAKER_FEE_PCT if maker else FEE_PCT
    base_slip = base / (slices ** 0.5) if slices > 1 else base
    return base_slip + market_impact_pct(notional_usd, book_depth_usd) + fee


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

    def place(self, leg: OrderLeg, slices: int = 1, book_depth_usd: float | None = None,
              maker: bool = False) -> Fill:
        # Maker/limit entry (solo BUY de acumulación lead): llena cerca del mid con fee maker
        # → costo de entrada mucho menor que un market taker. La salida no pasa por aquí
        # (la maneja position_manager con costo taker), así que maker solo abarata la entrada.
        _use_maker = maker and leg.side == Side.buy
        _slip = MAKER_SLIP_PCT if _use_maker else SLIPPAGE_PCT
        _fee = MAKER_FEE_PCT if _use_maker else FEE_PCT
        # Iceberg: partir reduce el slippage promedio ~/ sqrt(slices) (regla de
        # microestructura: cada slice come menos profundidad). slices=1 → directo.
        base_slip = _slip / (slices ** 0.5) if slices > 1 else _slip
        # + IMPACTO de mercado (tamaño vs profundidad del libro) → realista en microcaps.
        eff_slip = base_slip + market_impact_pct(leg.notional_usd, book_depth_usd)
        slip = 1 + eff_slip / 100 if leg.side == Side.buy else 1 - eff_slip / 100
        # + fee (maker si limit-entry, taker si market): el buy paga, recibe menos cantidad.
        fee = 1 + _fee / 100 if leg.side == Side.buy else 1 - _fee / 100
        fill_price = leg.entry_price * slip * fee
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
        daily_loss_usd: float = 0.0,
        current_drawdown_pct: float = 0.0,
        entry_maker: bool = False,
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
                daily_loss_usd=daily_loss_usd,
                current_drawdown_pct=current_drawdown_pct,
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
            # Gate de costo de entrada: si cruzar el spread cuesta más que el
            # tope, no se entra. Solo aplica a COMPRAS — una venta es una salida
            # y bloquearla dejaría la posición atrapada.
            if side == Side.buy and MAX_ENTRY_COST_PCT > 0:
                cost = estimated_entry_cost_pct(per_leg, book_depth_usd, slices, entry_maker)
                if cost > MAX_ENTRY_COST_PCT:
                    result.rejected.append(
                        f"{exchange}: entrada cara ({cost:.2f}% > {MAX_ENTRY_COST_PCT:.2f}%)"
                    )
                    continue
            try:
                fill = (await self.live.place(leg, slices) if mode == ExecMode.live
                        else self.paper.place(leg, slices, book_depth_usd=book_depth_usd,
                                              maker=entry_maker))
            except Exception as exc:  # noqa: BLE001 - surface broker errors as rejections, never crash
                result.rejected.append(f"{exchange}: {exc}")
                continue
            self.positions.append(fill)
            result.fills.append(fill)

        return result

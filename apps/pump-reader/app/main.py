"""Pump Reader API + built-in dashboard.

Scans Binance/MEXC/Bitget for scam-pump patterns, scores them with auditable
rules, and (in paper mode by default) can execute. Auto-scans on a timer so it
runs as a bot, not just a manual API. Every order passes the Risk Engine +
kill switch (see docs/security-invariants.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from statistics import mean, median
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import store
from .account import real_balances
from .dashboard import DASHBOARD_HTML
from .executor import ExecMode, ExecutionEngine, Side, current_mode
from .grid import GridBot, fetch_price
from .market import market_for_symbol
from .notify import format_alert, send_telegram
from .position_manager import PositionManager
from .risk import RiskGuard
from .scanner import ScannedCandidate, fetch_token_detail, scan_markets
from .velocity import VelocityWatcher, watch_list_from_scores

logger = logging.getLogger("pump-reader")

# Auto-scan cadence (the "Update" loop from the source tool). 5 min default.
SCAN_INTERVAL_SECONDS = int(os.getenv("PUMP_SCAN_INTERVAL_SECONDS", "300"))
# GRVT grid tick cadence (paper price feed step).
GRID_TICK_SECONDS = int(os.getenv("GRVT_TICK_SECONDS", "15"))
# Velocity watcher cadence — the fast loop that fires on volume acceleration
# between slow scans (this is the real-time entry trigger).
VELOCITY_TICK_SECONDS = int(os.getenv("PUMP_VELOCITY_TICK_SECONDS", "20"))


class CandidateStatus(StrEnum):
    watching = "watching"
    waiting_confirmation = "waiting_confirmation"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


# Score at/above which a candidate is surfaced for human confirmation.
WAITING_CONFIRMATION_THRESHOLD = 75


class TokenCandidate(BaseModel):
    id: str
    symbol: str
    exchange: str
    last_price: float
    quote_volume_24h: float
    price_change_pct_24h: float
    volume_spike: float
    orderbook_imbalance: float
    liquidity_usd: float
    pump_score: int
    confidence_score: int
    classification: str
    cluster: str = "long_pump"
    flags: list[str] = Field(default_factory=list)
    spark: list[float] = Field(default_factory=list)
    status: CandidateStatus
    updated_at: datetime


class ScanResponse(BaseModel):
    scanned_at: datetime
    count: int
    candidates: list[TokenCandidate]


class LearningRecord(BaseModel):
    id: str
    symbol: str
    action: str
    mode: str
    pump_score: int
    classification: str
    detail: str
    created_at: datetime


class ActResponse(BaseModel):
    symbol: str
    mode: str
    requested_usd: float
    fills: list[dict]
    rejected: list[str]


# In-memory store keyed by "exchange:symbol" (Postgres persistence is the next
# step; until DATABASE_URL is wired this is the source of truth).
_candidates: dict[str, TokenCandidate] = {}

# Execution layer (paper by default) + risk guard + learning ledger + exits.
_guard = RiskGuard()
_engine = ExecutionEngine(_guard)
_pm = PositionManager()
_learning: list[LearningRecord] = []
_last_scan_at: datetime | None = None

# Auto-entry (paper only): the bot buys candidates that cross the confirmation
# threshold so the exit engine has something to manage. Never auto-enters live.
AUTO_ENTRY = os.getenv("PUMP_AUTO_ENTRY", "true").lower() == "true"
AUTO_ENTRY_USD = float(os.getenv("PUMP_AUTO_ENTRY_USD", "100"))
# Adaptive confirmation threshold — the learning loop lowers it after late
# entries (be more sensitive to early moves) and raises it after false starts.
_adaptive_threshold = float(WAITING_CONFIRMATION_THRESHOLD)

# Paper account state for the dashboard equity curve / balance widgets.
PAPER_BALANCE = float(os.getenv("PUMP_PAPER_BALANCE", "1000"))
_equity_history: list[dict] = []

# Capital allocation: bot total + per-exchange split (% of effective equity).
_allocation: dict = {
    "bot_total_usdt": PAPER_BALANCE,
    "splits": {"mexc": 100.0, "bitget": 0.0},
}

# GRVTBot grid-trading section (separate product). Paper grid engine modeled on
# github.com/kmanus88/GRVTBot. Live GRVT execution needs the user's GRVT keys.
_grid = GridBot()

# Real-time volume-acceleration entry trigger (fires between slow scans).
_velocity = VelocityWatcher()

# Cached real account balance (only populated when the user's read-only keys are
# set). Until then the dashboard shows the paper balance.
_real_account: dict = {"has_keys": False, "total_usdt": 0.0, "connected": [], "snapshots": []}
# Real-account snapshot cadence (seconds). Only runs when keys are present.
ACCOUNT_POLL_SECONDS = int(os.getenv("PUMP_ACCOUNT_POLL_SECONDS", "120"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_auto_scan_loop()),
        asyncio.create_task(_grid_tick_loop()),
        asyncio.create_task(_monitor_loop()),
        asyncio.create_task(_velocity_loop()),
        asyncio.create_task(_account_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await _velocity.close()
        await store.close()


async def _auto_scan_loop() -> None:
    """Run a scan on startup, then every SCAN_INTERVAL_SECONDS, forever."""
    while True:
        try:
            await _perform_scan()
            logger.info("auto-scan done: %d candidates", len(_candidates))
        except Exception:
            logger.exception("auto-scan failed")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _grid_tick_loop() -> None:
    """When the grid is running, fetch a live price and advance the grid."""
    while True:
        try:
            if _grid.running and _grid.grid:
                price = await fetch_price(_grid.pair)
                if price > 0:
                    _grid.step(price)
        except Exception:
            logger.exception("grid tick failed")
        await asyncio.sleep(GRID_TICK_SECONDS)


async def _monitor_loop() -> None:
    """Tick every open managed position against a live price and run exits."""
    while True:
        try:
            for key, pos in list(_pm.positions.items()):
                if pos.closed:
                    continue
                price = await fetch_price(pos.symbol, pos.exchange)
                if price <= 0:
                    continue
                for event in _pm.step(key, price):
                    await _handle_exit(pos, event)
        except Exception:
            logger.exception("monitor loop failed")
        await asyncio.sleep(GRID_TICK_SECONDS)


async def _velocity_loop() -> None:
    """Fast loop: fire the entry the instant a watched symbol's volume
    accelerates, instead of waiting for the next 5-min scan."""
    while True:
        try:
            triggers = await _velocity.poll_once()
            for t in triggers:
                key = f"{t.exchange}:{t.symbol}"
                candidate = _candidates.get(key)
                if candidate is None:
                    continue
                if not (AUTO_ENTRY and current_mode() == ExecMode.paper):
                    continue
                if _pm.has(t.exchange, t.symbol):
                    continue
                candidate.last_price = t.price  # fire at the fresh trigger price
                _record_learning(
                    candidate.symbol, "velocity_trigger", "paper", candidate,
                    f"vol accel {t.accel:.1f}x @ {t.price}",
                )
                await send_telegram(
                    f"⚡ VOLUME SPIKE {t.symbol} ({t.exchange}) accel {t.accel:.1f}x @ {t.price} — entering"
                )
                await _auto_enter(candidate)
        except Exception:
            logger.exception("velocity loop failed")
        await asyncio.sleep(VELOCITY_TICK_SECONDS)


async def _account_loop() -> None:
    """Refresh the real read-only account balance when the user's keys exist.
    No keys → does nothing (paper balance stays). Never trades, read-only."""
    global _real_account
    while True:
        try:
            acct = await real_balances()
            if acct.get("has_keys"):
                _real_account = acct
                for snap in acct.get("snapshots", []):
                    await store.insert_account_snapshot(snap)
        except Exception:
            logger.exception("account loop failed")
        await asyncio.sleep(ACCOUNT_POLL_SECONDS)


async def _persist_position(pos) -> None:
    await store.upsert_position({
        "key": _pm.key(pos.exchange, pos.symbol),
        "symbol": pos.symbol, "exchange": pos.exchange,
        "entry_price": pos.entry_price, "qty": round(pos.qty, 8),
        "initial_qty": round(pos.initial_qty, 8), "phase": pos.phase,
        "peak_price": pos.peak_price, "last_price": pos.last_price,
        "realized_pnl": round(pos.realized_pnl, 4), "closed": pos.closed,
        "pump_score": pos.pump_score, "classification": pos.classification,
        "entry_at": pos.entry_at.isoformat(),
    })


async def _handle_exit(pos, event) -> None:
    pct = round(event.fraction * 100)
    _record_learning_raw(
        pos.symbol, f"exit_{event.reason}", "paper", pos.pump_score, pos.classification,
        f"sold {pct}% @ {event.price} pnl {event.pnl:+.2f}",
    )
    await store.insert_exit(event.__dict__)
    await _persist_position(pos)
    await send_telegram(
        f"💰 {event.reason.upper()} {pos.symbol} ({pos.exchange}) sold {pct}% @ {event.price} · pnl {event.pnl:+.2f}"
    )
    if event.closed:
        quality = _pm.entry_quality(pos)
        _record_learning_raw(
            pos.symbol, "trade_closed", "paper", pos.pump_score, pos.classification,
            f"realized {pos.realized_pnl:+.2f} · entry {quality}",
        )
        _apply_learning(quality)
        await send_telegram(
            f"✅ CLOSED {pos.symbol} · realized {pos.realized_pnl:+.2f} · entry {quality} · threshold now {round(_adaptive_threshold)}"
        )


def _apply_learning(quality: str) -> None:
    """Feedback loop: late entries make the bot more sensitive (lower the
    confirmation threshold); false-positive closes raise it back."""
    global _adaptive_threshold
    if quality == "late_entry":
        _adaptive_threshold = max(55.0, _adaptive_threshold - 3)
    elif quality == "early_entry":
        _adaptive_threshold = min(90.0, _adaptive_threshold + 1)


app = FastAPI(title="TradeOS AI Pump Reader", version="0.4.0", lifespan=lifespan)


def _record_learning_raw(symbol: str, action: str, mode: str, pump_score: int, classification: str, detail: str) -> None:
    rec = LearningRecord(
        id=str(uuid4()),
        symbol=symbol,
        action=action,
        mode=mode,
        pump_score=pump_score,
        classification=classification,
        detail=detail,
        created_at=datetime.now(UTC),
    )
    _learning.append(rec)
    del _learning[:-200]
    if store.enabled():
        asyncio.create_task(store.insert_learning({
            "id": rec.id, "symbol": rec.symbol, "action": rec.action, "mode": rec.mode,
            "pump_score": rec.pump_score, "classification": rec.classification,
            "detail": rec.detail, "created_at": rec.created_at.isoformat(),
        }))


def _record_learning(symbol: str, action: str, mode: str, candidate: TokenCandidate, detail: str) -> None:
    _record_learning_raw(symbol, action, mode, candidate.pump_score, candidate.classification, detail)


def _status_for(pump_score: int) -> CandidateStatus:
    if pump_score >= _adaptive_threshold:
        return CandidateStatus.waiting_confirmation
    return CandidateStatus.watching


def _to_candidate(scanned: ScannedCandidate) -> TokenCandidate:
    return TokenCandidate(
        id=str(uuid4()),
        symbol=scanned.symbol,
        exchange=scanned.exchange,
        last_price=scanned.last_price,
        quote_volume_24h=scanned.quote_volume_24h,
        price_change_pct_24h=scanned.price_change_pct_24h,
        volume_spike=scanned.volume_spike,
        orderbook_imbalance=scanned.orderbook_imbalance,
        liquidity_usd=scanned.liquidity_usd,
        pump_score=scanned.pump_score,
        confidence_score=scanned.confidence_score,
        classification=scanned.classification,
        cluster=scanned.cluster,
        flags=scanned.flags,
        spark=scanned.spark,
        status=_status_for(scanned.pump_score),
        updated_at=datetime.now(UTC),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pump-reader"}


@app.get("/candidates", response_model=list[TokenCandidate])
async def list_candidates() -> list[TokenCandidate]:
    return sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)


def _scan_exchanges() -> list[str]:
    raw = os.getenv("PUMP_SCAN_EXCHANGES", "binance,mexc,bitget")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


async def _auto_enter(candidate: TokenCandidate) -> None:
    """Paper-only auto buy on a confirmed candidate; hand it to the exit engine."""
    result = await _engine.act(
        symbol=candidate.symbol, side=Side.buy, reference_price=candidate.last_price,
        capital_usd=AUTO_ENTRY_USD, exchanges=[candidate.exchange],
    )
    for fill in result.fills:
        _pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
        )
        opened = _pm.positions.get(_pm.key(fill.exchange, fill.symbol))
        if opened:
            await _persist_position(opened)
        _record_learning(candidate.symbol, "auto_entry", "paper", candidate, f"bought ${AUTO_ENTRY_USD:.0f} @ {fill.fill_price}")
        await send_telegram(
            f"🚨 ENTRY {candidate.symbol} ({candidate.exchange}) score {candidate.pump_score} · ${AUTO_ENTRY_USD:.0f} @ {fill.fill_price}"
        )


async def _perform_scan(min_pump_score: int = 1) -> ScanResponse:
    global _last_scan_at
    scanned = await scan_markets(_scan_exchanges(), min_pump_score=min_pump_score)
    _candidates.clear()
    for item in scanned:
        candidate = _to_candidate(item)
        _candidates[f"{candidate.exchange}:{candidate.symbol}"] = candidate
        if candidate.status == CandidateStatus.waiting_confirmation:
            await send_telegram(
                format_alert(candidate.symbol, candidate.pump_score, candidate.classification, candidate.flags)
            )
            await store.insert_alert({
                "symbol": candidate.symbol, "exchange": candidate.exchange,
                "pump_score": candidate.pump_score, "classification": candidate.classification,
                "flags": candidate.flags,
            })
            if AUTO_ENTRY and current_mode() == ExecMode.paper and not _pm.has(candidate.exchange, candidate.symbol):
                await _auto_enter(candidate)
    _last_scan_at = datetime.now(UTC)
    # Persist the scan snapshot (no-op without Supabase keys).
    await store.upsert_candidates([
        {
            "symbol": c.symbol, "exchange": c.exchange, "last_price": c.last_price,
            "quote_volume_24h": c.quote_volume_24h, "price_change_pct_24h": c.price_change_pct_24h,
            "volume_spike": c.volume_spike, "orderbook_imbalance": c.orderbook_imbalance,
            "liquidity_usd": c.liquidity_usd, "pump_score": c.pump_score,
            "confidence_score": c.confidence_score, "classification": c.classification,
            "cluster": c.cluster, "flags": c.flags, "spark": c.spark,
            "status": c.status.value, "updated_at": c.updated_at.isoformat(),
        }
        for c in _candidates.values()
    ])
    # Refresh the velocity hot-list so the fast loop watches the hottest names
    # and can fire on acceleration before the next slow scan.
    try:
        await _velocity.sync(
            watch_list_from_scores(
                [(c.exchange, c.symbol, c.pump_score) for c in _candidates.values()]
            )
        )
    except Exception:
        logger.exception("velocity sync failed")
    # Mark equity (real account total when keys exist, else paper balance).
    equity_v = _real_account["total_usdt"] if _real_account.get("has_keys") else PAPER_BALANCE
    point = {"t": _last_scan_at.isoformat(), "v": equity_v}
    _equity_history.append(point)
    del _equity_history[:-200]
    await store.insert_equity(point)
    ranked = sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)
    return ScanResponse(scanned_at=_last_scan_at, count=len(ranked), candidates=ranked)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/status")
async def status() -> dict:
    return {
        "service": "pump-reader",
        "exec_mode": os.getenv("PUMP_EXEC_MODE", "paper"),
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "exchanges": _scan_exchanges(),
        "last_scan_at": _last_scan_at.isoformat() if _last_scan_at else None,
        "candidate_count": len(_candidates),
        "kill_switch_active": _guard.kill_switch,
        "open_positions": len(_engine.positions),
        "persistence": "supabase" if store.enabled() else "memory",
        "account_connected": _real_account.get("connected", []),
    }


@app.get("/account")
async def account() -> dict:
    """Real read-only balance (only when the user's keys are set), else paper."""
    global _real_account
    acct = await real_balances()
    if acct.get("has_keys"):
        _real_account = acct
        for snap in acct.get("snapshots", []):
            await store.insert_account_snapshot(snap)
        return {**acct, "source": "live_account"}
    return {
        "has_keys": False, "source": "paper", "total_usdt": PAPER_BALANCE,
        "connected": [], "snapshots": [],
        "note": "No exchange keys set. Add read-only spot keys (no withdrawal) to env to show your real balance.",
    }


@app.get("/token/market")
async def token_market(symbol: str) -> dict:
    """Real FDV / market cap / supply from CoinGecko (no key). n/a if no match."""
    base = symbol.upper().split("/")[0]
    data = await market_for_symbol(base)
    if data:
        await store.upsert_token_market({
            "symbol": base, "coingecko_id": data.get("coingecko_id"), "name": data.get("name"),
            "market_cap_usd": data.get("market_cap_usd"), "fdv_usd": data.get("fdv_usd"),
            "circulating_supply": data.get("circulating_supply"), "total_supply": data.get("total_supply"),
            "price_usd": data.get("price_usd"),
        })
        return {"found": True, **data}
    return {"found": False, "symbol": base}


def _cluster_stats(cluster: str) -> dict:
    scores = [c.pump_score for c in _candidates.values() if c.cluster == cluster]
    if not scores:
        return {"count": 0, "avg": 0.0, "median": 0.0, "max": 0.0}
    return {
        "count": len(scores),
        "avg": round(mean(scores), 2),
        "median": round(median(scores), 2),
        "max": round(max(scores), 2),
    }


def _ago(dt: datetime) -> str:
    secs = (datetime.now(UTC) - dt).total_seconds()
    if secs < 90:
        return "ahora"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


@app.get("/overview")
async def overview() -> dict:
    ranked = sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)
    top = ranked[0] if ranked else None
    alerts = [c for c in ranked if c.status == CandidateStatus.waiting_confirmation]

    return {
        "now": datetime.now(UTC).isoformat(),
        "exchanges": _scan_exchanges(),
        "monitored": len(_candidates),
        "exec_mode": os.getenv("PUMP_EXEC_MODE", "paper"),
        "live": True,
        "score_max": (
            {"value": top.pump_score, "symbol": top.symbol, "cluster": top.cluster}
            if top
            else None
        ),
        "alerts_24h": {
            "total": len(alerts),
            "classic": sum(1 for c in alerts if c.cluster == "classic"),
            "long_pump": sum(1 for c in alerts if c.cluster == "long_pump"),
        },
        "cluster_split": {
            "classic": _cluster_stats("classic"),
            "long_pump": _cluster_stats("long_pump"),
        },
        "open_positions": len(_engine.positions),
        "balance": _real_account["total_usdt"] if _real_account.get("has_keys") else PAPER_BALANCE,
        "balance_source": "live_account" if _real_account.get("has_keys") else "paper",
        "account_connected": _real_account.get("connected", []),
        "persistence": "supabase" if store.enabled() else "memory",
        "pnl_7d": 0.0,
        "equity_curve": _equity_history,
        "table": [
            {
                "cluster": c.cluster,
                "score": c.pump_score,
                "symbol": c.symbol,
                "exchange": c.exchange,
                "top20": round(c.orderbook_imbalance * 100, 1),
                "delta_24h": c.price_change_pct_24h,
                "spark": c.spark,
            }
            for c in ranked[:12]
        ],
        "latest_alerts": [
            {
                "symbol": c.symbol,
                "cluster": c.cluster,
                "score": c.pump_score,
                "ago": _ago(c.updated_at),
            }
            for c in alerts[:6]
        ],
    }


class AllocationRequest(BaseModel):
    bot_total_usdt: float = Field(ge=0)
    splits: dict[str, float]


@app.get("/allocation")
async def get_allocation() -> dict:
    total_pct = round(sum(_allocation["splits"].values()), 2)
    return {**_allocation, "sum_pct": total_pct, "valid": abs(total_pct - 100.0) < 0.01}


@app.post("/allocation")
async def set_allocation(req: AllocationRequest) -> dict:
    total_pct = round(sum(req.splits.values()), 2)
    if abs(total_pct - 100.0) >= 0.01:
        raise HTTPException(status_code=400, detail=f"splits must sum to 100% (got {total_pct}%)")
    _allocation["bot_total_usdt"] = req.bot_total_usdt
    _allocation["splits"] = {k.lower(): float(v) for k, v in req.splits.items()}
    return {**_allocation, "sum_pct": total_pct, "valid": True}


class GridConfigRequest(BaseModel):
    pair: str = "BTC/USDT"
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    levels: int = Field(ge=2, le=200)
    capital: float = Field(gt=0)


@app.get("/grvt/status")
async def grvt_status() -> dict:
    return _grid.stats()


@app.post("/grvt/config")
async def grvt_config(req: GridConfigRequest) -> dict:
    try:
        _grid.configure(req.pair, req.lower, req.upper, req.levels, req.capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _grid.stats()


@app.post("/grvt/start")
async def grvt_start() -> dict:
    try:
        _grid.start()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Immediate first tick so the UI updates without waiting for the loop.
    price = await fetch_price(_grid.pair)
    if price > 0:
        _grid.step(price)
    return _grid.stats()


@app.post("/grvt/stop")
async def grvt_stop() -> dict:
    _grid.stop()
    return _grid.stats()


@app.post("/scan", response_model=ScanResponse)
async def run_scan(min_pump_score: int = 1) -> ScanResponse:
    return await _perform_scan(min_pump_score=min_pump_score)


@app.post("/act", response_model=ActResponse)
async def act_on_candidate(symbol: str, capital_usd: float = 100.0, exchange: str | None = None) -> ActResponse:
    symbol_u = symbol.upper()
    matches = [c for c in _candidates.values() if c.symbol == symbol_u]
    if exchange:
        matches = [c for c in matches if c.exchange == exchange.lower()]
    if not matches:
        raise HTTPException(status_code=404, detail="candidate not found; run /scan first")
    candidate = max(matches, key=lambda c: c.pump_score)

    result = await _engine.act(
        symbol=candidate.symbol,
        side=Side.buy,
        reference_price=candidate.last_price,
        capital_usd=capital_usd,
        exchanges=[candidate.exchange],
    )

    for fill in result.fills:
        _pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
        )

    detail = (
        f"{len(result.fills)} fills, {len(result.rejected)} rejected"
        if result.fills or result.rejected
        else "no action"
    )
    _record_learning(candidate.symbol, "execute", result.mode.value, candidate, detail)

    return ActResponse(
        symbol=result.symbol,
        mode=result.mode.value,
        requested_usd=result.requested_usd,
        fills=[fill.__dict__ | {"side": fill.side.value, "mode": fill.mode.value} for fill in result.fills],
        rejected=result.rejected,
    )


@app.get("/positions")
async def list_positions() -> list[dict]:
    return [fill.__dict__ | {"side": fill.side.value, "mode": fill.mode.value} for fill in _engine.positions]


@app.get("/managed")
async def list_managed() -> dict:
    open_positions = [
        {
            "symbol": p.symbol,
            "exchange": p.exchange,
            "entry_price": p.entry_price,
            "qty": round(p.qty, 8),
            "phase": p.phase,
            "peak_price": p.peak_price,
            "last_price": p.last_price,
            "pump_score": p.pump_score,
            "classification": p.classification,
            "gain_pct": round((p.last_price - p.entry_price) / p.entry_price * 100, 2) if p.entry_price else 0.0,
            "realized_pnl": round(p.realized_pnl, 4),
            "unrealized_pnl": round((p.last_price - p.entry_price) * p.qty, 4),
        }
        for p in _pm.positions.values()
        if not p.closed
    ]
    return {
        "open": open_positions,
        "exits": [e.__dict__ for e in reversed(_pm.history[-20:])],
        "adaptive_threshold": round(_adaptive_threshold, 1),
        "auto_entry": AUTO_ENTRY,
    }


@app.get("/velocity")
async def velocity_status() -> dict:
    return _velocity.status()


@app.get("/token/detail")
async def token_detail(symbol: str, exchange: str) -> dict:
    detail = await fetch_token_detail(exchange.lower(), symbol.upper())
    if detail is None:
        raise HTTPException(status_code=404, detail="could not fetch market data")
    return detail


@app.get("/learning", response_model=list[LearningRecord])
async def list_learning() -> list[LearningRecord]:
    return _learning


@app.post("/risk/kill-switch")
async def set_kill_switch(active: bool, reason: str = "manual") -> dict:
    _guard.set_kill_switch(active, reason)
    return {"kill_switch_active": _guard.kill_switch, "reason": _guard.kill_reason}

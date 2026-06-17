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

import httpx
from datetime import UTC, datetime
from enum import StrEnum
from statistics import mean, median
from uuid import uuid4

from dotenv import load_dotenv

# Load .env (SUPABASE_*, exchange keys, Telegram) before importing modules that
# read these at import time (store, executor, velocity, …).
load_dotenv()

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import (
    COOKIE, LOGIN_HTML, MAX_AGE, auth_enabled, check_credentials, make_token, valid_token,
)

from . import grid_sync, store
from .account import real_balances
from .dashboard import DASHBOARD_HTML
from .executor import ExecMode, ExecutionEngine, Side, current_mode
from .grid import GridBot, backtest, fetch_ohlcv_for, fetch_price
from .grvt_proxy import register_grvt_proxy
from .market import market_for_symbol
from . import notify
from .notify import format_alert, send_telegram
from .position_manager import PositionManager
from .risk import RiskGuard
from .scanner import ScannedCandidate, fetch_token_detail, scan_markets
from .velocity import VelocityWatcher, watch_list_from_scores
from .learning import LearningLab

logger = logging.getLogger("pump-reader")

# Auto-scan cadence (the "Update" loop from the source tool). 5 min default.
SCAN_INTERVAL_SECONDS = int(os.getenv("PUMP_SCAN_INTERVAL_SECONDS", "300"))
# GRVT grid tick cadence (paper price feed step).
GRID_TICK_SECONDS = int(os.getenv("GRVT_TICK_SECONDS", "15"))
# Velocity watcher cadence — the fast loop that fires on volume acceleration
# between slow scans (this is the real-time entry trigger).
VELOCITY_TICK_SECONDS = int(os.getenv("PUMP_VELOCITY_TICK_SECONDS", "20"))
# Grid→Supabase mirror cadence — copies the embedded GRVTBot state into the
# shared realtime store. No-op when Supabase or the bot is unavailable.
GRID_SYNC_SECONDS = int(os.getenv("GRID_SYNC_SECONDS", "60"))


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
    score_long_pump: int = 0
    score_classic: int = 0
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

# Learning lab: tracks whether alerts fired BEFORE the pump (MFE/MAE, lead time,
# precision/recall) and proposes threshold tweaks once outcomes settle.
_lab = LearningLab()

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
        asyncio.create_task(_grid_sync_loop()),
        asyncio.create_task(_daily_discover_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await _velocity.close()
        await grid_sync.close()
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


async def _daily_discover_loop() -> None:
    """Run a full discover once per day and log a dated report (on top of the
    fast 5-min monitor scan). First run ~30s after boot for immediate evidence."""
    await asyncio.sleep(30)
    while True:
        try:
            await _perform_scan()
            ranked = sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)
            top = ranked[0] if ranked else None
            msg = (
                f"Daily discover · {len(_candidates)} tokens scanned · top {top.symbol} "
                f"{top.pump_score} ({top.cluster})"
                if top else "Daily discover · no candidates found"
            )
            logger.info(msg)
            if store.enabled():
                await store.insert_bot_log("PUMP_SCANNER", "INFO", msg)
        except Exception:
            logger.exception("daily discover failed")
        await asyncio.sleep(86400)


async def _grid_sync_loop() -> None:
    """Mirror the embedded GRVTBot state into Supabase on a timer (best-effort)."""
    if not store.enabled():
        return
    while True:
        try:
            await grid_sync.sync_once()
        except Exception:
            logger.exception("grid sync failed")
        await asyncio.sleep(GRID_SYNC_SECONDS)


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
            # Learning lab: track each alerted token's MFE/MAE/lead time vs live
            # price so we can tell whether alerts fire BEFORE the pump.
            for exch, sym in _lab.active_symbols():
                price = await fetch_price(sym, exch)
                if price > 0:
                    _lab.step(exch, sym, price)
            _lab.settle_due()
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
    await store.insert_bot_log(
        "PUMP_SCANNER",
        "PANIC_SELL" if event.reason in ("dump", "hard_stop") else "TRADE_SELL",
        f"{event.reason} {pos.symbol} sold {pct}% @ {event.price}",
        pnl=event.pnl,
    )
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

# Same-origin reverse proxy to the real GRVTBot (Node) under /grid/*.
register_grvt_proxy(app)

_PUBLIC_PATHS = {"/login", "/logout", "/health"}


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Require login for everything once APP_PASSWORD is set (off in dev/paper)."""
    if not auth_enabled():
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/grid"):
        return await call_next(request)
    if valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return LOGIN_HTML.replace("<!--ERR-->", "")


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if check_credentials(username, password):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, make_token(username), max_age=MAX_AGE, httponly=True, samesite="lax")
        return resp
    return HTMLResponse(LOGIN_HTML.replace("<!--ERR-->", "Invalid username or password"), status_code=401)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


_grid_token_cache: str | None = None
_grid_token_at: float = 0.0


@app.get("/grid-sso")
async def grid_sso():
    """Single sign-on for the embedded GRVTBot.

    The TradeOS login is the only login the user sees. This route (gated by the
    auth middleware) logs into the GRVTBot with owner credentials server-side
    and returns the JWT so the iframe SPA boots already authenticated. The owner
    password never reaches the browser.

    The minted token (GRVT JWT, 24h) is cached and reused for 12h so repeated
    grid opens don't trip the bot's login rate-limiter.
    """
    global _grid_token_cache, _grid_token_at
    import time

    now = time.time()
    if _grid_token_cache and (now - _grid_token_at) < 12 * 3600:
        return {"ok": True, "key": "grvt-grid-token", "token": _grid_token_cache}

    email = os.getenv("GRID_OWNER_EMAIL", "admin@tradeos.local")
    password = os.getenv("GRID_OWNER_PASSWORD", "")
    if not password:
        return JSONResponse({"ok": False, "error": "GRID_OWNER_PASSWORD not set"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "http://127.0.0.1:3848/api/v2/auth/login",
                json={"email": email, "password": password},
            )
    except httpx.HTTPError:
        if _grid_token_cache:
            return {"ok": True, "key": "grvt-grid-token", "token": _grid_token_cache, "cached": True}
        return JSONResponse({"ok": False, "error": "grid_offline"}, status_code=502)
    if resp.status_code == 200:
        _grid_token_cache = resp.json().get("token")
        _grid_token_at = now
        return {"ok": True, "key": "grvt-grid-token", "token": _grid_token_cache}
    # Login throttled/failed — fall back to a still-valid cached token if we have one.
    if _grid_token_cache:
        return {"ok": True, "key": "grvt-grid-token", "token": _grid_token_cache, "cached": True}
    return JSONResponse({"ok": False, "error": "grid_login_failed", "code": resp.status_code}, status_code=502)


@app.get("/telegram")
async def telegram_status() -> dict:
    return notify.status()


@app.post("/telegram/config")
async def telegram_config(token: str = Form(""), chat_id: str = Form("")) -> dict:
    notify.configure(token=token or None, chat_id=chat_id or None)
    notify.persist_env()
    return notify.status()


@app.get("/telegram/updates")
async def telegram_updates() -> dict:
    """List chats the bot can see, so the user can pick their group's id."""
    return await notify.get_updates()


@app.post("/telegram/test")
async def telegram_test() -> dict:
    return {"ok": await notify.send_test(), **notify.status()}


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
        score_long_pump=scanned.score_long_pump,
        score_classic=scanned.score_classic,
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
        await store.insert_bot_log(
            "PUMP_SCANNER", "TRADE_BUY",
            f"Auto-entry {candidate.symbol} ${AUTO_ENTRY_USD:.0f} @ {fill.fill_price}",
            volumen=candidate.volume_spike,
        )
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
            await store.insert_pump_candidate({
                "symbol": candidate.symbol, "exchange": candidate.exchange.upper(),
                "current_spread": None, "volume_acceleration": candidate.volume_spike,
                "status": "TRIGGERED",
            })
            await store.insert_bot_log(
                "PUMP_SCANNER", "INFO",
                f"Alert {candidate.symbol} ({candidate.exchange.upper()}) score {candidate.pump_score} · {candidate.classification}",
                volumen=candidate.volume_spike,
            )
            _lab.record_alert(
                symbol=candidate.symbol, exchange=candidate.exchange, alert_price=candidate.last_price,
                pump_score=candidate.pump_score, cluster=candidate.cluster, classification=candidate.classification,
                signals={
                    "volume_spike": candidate.volume_spike,
                    "price_change_pct_24h": candidate.price_change_pct_24h,
                    "orderbook_imbalance": candidate.orderbook_imbalance,
                    "liquidity_usd": candidate.liquidity_usd,
                },
            )
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
    equity_v = _real_account["total_usdt"] if _real_account.get("has_keys") else _paper_equity()
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
        "has_keys": False, "source": "paper", "total_usdt": _paper_equity(),
        "allocated_usdt": float(_allocation.get("bot_total_usdt") or PAPER_BALANCE),
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


def _pnl_7d() -> float:
    """Paper/live P&L over the last 7 days: realized exits (7d) + open unrealized.

    Makes demo gains visible — every value comes from real managed positions
    priced against the live market, nothing invented.
    """
    cutoff = datetime.now(UTC).timestamp() - 7 * 86400
    realized = 0.0
    for e in _pm.history:
        try:
            ts = datetime.fromisoformat(e.at).timestamp()
        except Exception:
            ts = cutoff  # undated event → still count it
        if ts >= cutoff:
            realized += e.pnl
    unrealized = sum(
        (p.last_price - p.entry_price) * p.qty
        for p in _pm.positions.values()
        if not p.closed and p.last_price > 0
    )
    return round(realized + unrealized, 2)


def _paper_equity() -> float:
    """Paper balance = capital you allocated (bot total) + demo gains. Driven by
    the allocation modal, so adding capital updates the balance immediately."""
    base = float(_allocation.get("bot_total_usdt") or PAPER_BALANCE)
    realized = sum(e.pnl for e in _pm.history)
    unrealized = sum(
        (p.last_price - p.entry_price) * p.qty
        for p in _pm.positions.values()
        if not p.closed and p.last_price > 0
    )
    return round(base + realized + unrealized, 2)


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
        "balance": _real_account["total_usdt"] if _real_account.get("has_keys") else _paper_equity(),
        "balance_source": "live_account" if _real_account.get("has_keys") else "paper",
        "account_connected": _real_account.get("connected", []),
        "persistence": "supabase" if store.enabled() else "memory",
        "pnl_7d": _pnl_7d(),
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
    await store.upsert_allocation({
        "bot_total_usdt": _allocation["bot_total_usdt"], "splits": _allocation["splits"],
    })
    return {**_allocation, "sum_pct": total_pct, "valid": True}


class GridConfigRequest(BaseModel):
    pair: str = "BTC/USDT"
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    levels: int = Field(ge=2, le=200)
    capital: float = Field(gt=0)


async def _persist_grid() -> None:
    await store.upsert_grid({
        "pair": _grid.pair, "lower_price": _grid.lower, "upper_price": _grid.upper,
        "levels": _grid.levels, "capital": _grid.capital, "cash": _grid.cash,
        "position": _grid.position, "realized": _grid.realized, "last_price": _grid.last_price,
        "running": _grid.running, "grid": _grid.grid, "held": _grid.held, "qty": _grid.qty,
    })


@app.get("/grvt/status")
async def grvt_status() -> dict:
    return _grid.stats()


@app.post("/grvt/config")
async def grvt_config(req: GridConfigRequest) -> dict:
    try:
        _grid.configure(req.pair, req.lower, req.upper, req.levels, req.capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _persist_grid()
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
    await _persist_grid()
    return _grid.stats()


@app.post("/grvt/stop")
async def grvt_stop() -> dict:
    _grid.stop()
    await _persist_grid()
    return _grid.stats()


class GridBacktestRequest(BaseModel):
    pair: str = "BTC/USDT"
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    levels: int = Field(ge=2, le=200)
    capital: float = Field(gt=0)
    timeframe: str = "1h"
    limit: int = Field(default=168, ge=20, le=1000)
    fee_pct: float = Field(default=0.1, ge=0, le=1)


@app.post("/grvt/backtest")
async def grvt_backtest(req: GridBacktestRequest) -> dict:
    """Backtest a grid config over real historical candles before risking it."""
    if req.upper <= req.lower:
        raise HTTPException(status_code=400, detail="upper must be > lower")
    candles = await fetch_ohlcv_for(req.pair, req.timeframe, req.limit)
    if not candles:
        raise HTTPException(status_code=400, detail="no historical data for this pair")
    result = backtest(req.lower, req.upper, req.levels, req.capital, candles, req.fee_pct)
    return {**result, "pair": req.pair.upper(), "timeframe": req.timeframe}


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


class SettingsRequest(BaseModel):
    confirmation_threshold: float | None = Field(default=None, ge=1, le=100)
    auto_entry: bool | None = None
    auto_entry_usd: float | None = Field(default=None, ge=1)


def _settings_payload() -> dict:
    return {
        "confirmation_threshold": round(_adaptive_threshold, 1),
        "auto_entry": AUTO_ENTRY,
        "auto_entry_usd": AUTO_ENTRY_USD,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "velocity_accel_factor": _velocity.status().get("accel_factor"),
        "exec_mode": current_mode().value,
        "exchanges": _scan_exchanges(),
    }


@app.get("/settings")
async def get_settings() -> dict:
    return _settings_payload()


@app.post("/settings")
async def update_settings(req: SettingsRequest) -> dict:
    """Live bot configuration. Lowering the confirmation threshold makes the bot
    more sensitive (more alerts + paper auto-entries)."""
    global _adaptive_threshold, AUTO_ENTRY, AUTO_ENTRY_USD
    if req.confirmation_threshold is not None:
        _adaptive_threshold = float(req.confirmation_threshold)
    if req.auto_entry is not None:
        AUTO_ENTRY = bool(req.auto_entry)
    if req.auto_entry_usd is not None:
        AUTO_ENTRY_USD = float(req.auto_entry_usd)
    # Re-evaluate candidate statuses so the Alerts view reflects the change now.
    for c in _candidates.values():
        c.status = _status_for(c.pump_score)
    return _settings_payload()


@app.get("/learning")
async def learning_snapshot() -> dict:
    """Feedback-loop analytics: did alerts fire before the pump, precision/recall,
    lead time, component contributions, and threshold proposals."""
    return _lab.snapshot()


@app.get("/learning/ledger", response_model=list[LearningRecord])
async def list_learning() -> list[LearningRecord]:
    return _learning


class MissedPumpRequest(BaseModel):
    symbol: str
    exchange: str = "n/a"


@app.post("/learning/missed")
async def report_missed(req: MissedPumpRequest) -> dict:
    """User reports a pump the bot did NOT alert (lowers recall)."""
    return _lab.record_missed(req.symbol, req.exchange)


@app.post("/risk/kill-switch")
async def set_kill_switch(active: bool, reason: str = "manual") -> dict:
    _guard.set_kill_switch(active, reason)
    return {"kill_switch_active": _guard.kill_switch, "reason": _guard.kill_reason}

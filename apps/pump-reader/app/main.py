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
import statistics
import sys
import traceback
from contextlib import asynccontextmanager

import httpx
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from statistics import mean, median, stdev  # noqa: F401
from uuid import uuid4

from dotenv import load_dotenv

# Load .env (SUPABASE_*, exchange keys, Telegram) before importing modules that
# read these at import time (store, executor, velocity, …).
load_dotenv()

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth as auth_mod
from .auth import (
    COOKIE, LOGIN_HTML, MAX_AGE, auth_enabled, authenticate, make_token, read_token,
)

from . import analytics, db_migrate, events, grid_sync, marketdata, store, telemetry
from .analytics import get_engine as get_analytics
from .events import EXIT_REASON_EVENT, EventType, get_bus
from .account import real_balances
from .dashboard import DASHBOARD_HTML
from .executor import ExecMode, ExecutionEngine, Side, current_mode
from .grid import GridBot, backtest, fetch_ohlcv_for, fetch_price
from .grvt_proxy import register_grvt_proxy
from .market import market_for_symbol
from . import notify
from .notify import format_alert, send_telegram
from .position_manager import (
    BREAKEVEN_PCT, DUMP_TICK_PCT, TIMEOUT_MINUTES, ManagedPosition, PositionManager,
)
from .risk import RiskGuard
from .scanner import ScannedCandidate, _cluster, fetch_token_detail, forensic_check, scan_markets
from .velocity import VelocityWatcher, watch_list_from_scores
from .learning import LearningLab
from .websocket_manager import USE_WEBSOCKETS, get_manager
from .user_bot import PAPER_BALANCE, UserBot, all_bots, default_allocation, ensure_bots, get_bot
from .microstructure import MicroObserver, iso as micro_iso
from .forensics import ForensicsStore
from .pipeline import Pipeline

logger = logging.getLogger("pump-reader")


def _fatal_excepthook(exc_type, exc, tb) -> None:
    """On an uncaught crash, push the traceback to Telegram BEFORE dying so the
    operator sees why (systemd then restarts the process). KeyboardInterrupt is
    left to the default handler (clean Ctrl-C / shutdown, not a crash)."""
    if not issubclass(exc_type, KeyboardInterrupt):
        tb_text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.critical("FATAL uncaught exception:\n%s", tb_text)
        try:
            notify.send_error_sync("FATAL · proceso uvicorn", tb_text)
        except Exception:  # noqa: BLE001 - already crashing, never mask the original
            pass
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _fatal_excepthook

# Auto-scan cadence (the "Update" loop from the source tool). 5 min default.
SCAN_INTERVAL_SECONDS = int(os.getenv("PUMP_SCAN_INTERVAL_SECONDS", "300"))
# GRVT grid tick cadence (paper price feed step).
GRID_TICK_SECONDS = int(os.getenv("GRVT_TICK_SECONDS", "15"))
# Velocity watcher cadence — the fast loop that fires on volume acceleration
# between slow scans (this is the real-time entry trigger).
VELOCITY_TICK_SECONDS = int(os.getenv("PUMP_VELOCITY_TICK_SECONDS", "10"))
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
WAITING_CONFIRMATION_THRESHOLD = int(os.getenv("PUMP_WAITING_CONFIRMATION_THRESHOLD", "75"))


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
    spread_pct: float = 0.0
    top_book_share: float = 0.0
    manipulation_suspect: bool = False
    flags: list[str] = Field(default_factory=list)
    spark: list[float] = Field(default_factory=list)
    # FSM (Fase 2) analysis merged in, so the Análisis view = market + pre-pump
    # analysis in one table. None until the token enters the pipeline.
    fsm_state: str | None = None
    fsm_acc: int | None = None
    fsm_pers: int | None = None
    fsm_rug: int | None = None
    fsm_confirm: int | None = None
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

# Per-user trading state — each account is its OWN bot (balance, positions, risk,
# equity, P&L) and lives in the user_bot registry (get_bot / all_bots). The owner
# is the default tenant ("owner"). Everything below stays GLOBAL — the shared
# brain that every account's bot consumes.
OWNER_UID = "owner"
_learning: list[LearningRecord] = []
_last_scan_at: datetime | None = None

# Auto-entry (paper only): the bot buys candidates that cross the confirmation
# threshold so the exit engine has something to manage. Never auto-enters live.
AUTO_ENTRY = os.getenv("PUMP_AUTO_ENTRY", "true").lower() == "true"
AUTO_ENTRY_USD = float(os.getenv("PUMP_AUTO_ENTRY_USD", "100"))
# Entry authority. The pump must be caught BEFORE it runs, so by default ONLY the
# pre-pump FSM (accumulation/persistence/rug over a window) may auto-enter. The
# momentum scan-path (pump_score>=threshold = already up) and the velocity-accel
# path (a move already underway) are LATE BY CONSTRUCTION. Their auto-entry is OFF
# by default — they still scan, alert and feed the recorder/FSM, they just don't
# buy the breakout. Flip these on only to deliberately allow late momentum chasing.
MOMENTUM_AUTOENTRY = os.getenv("PUMP_MOMENTUM_AUTOENTRY", "false").lower() == "true"
VELOCITY_AUTOENTRY = os.getenv("PUMP_VELOCITY_AUTOENTRY", "false").lower() == "true"
# Entry momentum/exhaustion gate (anti-chase): skip a candidate already up this
# much on 24h (the pump ran — buying it = buying the top), and require a minimum
# volume spike behind scan-path entries.
ENTRY_MAX_CHASE_PCT = float(os.getenv("PUMP_ENTRY_MAX_CHASE_PCT", "60"))
ENTRY_MIN_VOL_SPIKE = float(os.getenv("PUMP_ENTRY_MIN_VOL_SPIKE", "2.5"))
# Confidence floor: the scanner's confidence_score (~35 thin spike … ~95 deep
# book + clean live move) must clear this before any auto-entry. Filters the
# low-confidence thin-book signals that just bleed the spread.
ENTRY_MIN_CONFIDENCE = float(os.getenv("PUMP_ENTRY_MIN_CONFIDENCE", "50"))
# Adaptive confirmation threshold — the learning loop lowers it after late
# entries (be more sensitive to early moves) and raises it after false starts.
# Aggressive start: enter on score >= 45 so the bot trades often and LEARNS fast
# from zero. The optimizer/learning then tunes it up toward quality over time
# (clamped to [THRESHOLD_FLOOR, THRESHOLD_CEIL]). Override via PUMP_INITIAL_THRESHOLD.
_adaptive_threshold = float(os.getenv("PUMP_INITIAL_THRESHOLD", "45"))

# GRVTBot grid-trading section (separate product). Paper grid engine modeled on
# github.com/kmanus88/GRVTBot. Live GRVT execution needs the user's GRVT keys.
_grid = GridBot()

# Real-time volume-acceleration entry trigger (fires between slow scans).
_velocity = VelocityWatcher()

# Learning lab: tracks whether alerts fired BEFORE the pump (MFE/MAE, lead time,
# precision/recall) and proposes threshold tweaks once outcomes settle.
_lab = LearningLab()

# Real-account snapshot cadence (seconds). Only runs when keys are present.
ACCOUNT_POLL_SECONDS = int(os.getenv("PUMP_ACCOUNT_POLL_SECONDS", "120"))

# Microstructure recorder (FASE 1 — solo recolección de datos). Independiente de
# la lógica de trading; se inicializa en lifespan. None hasta que arranca el app
# (así importar main.py — p.ej. desde tools/simulate.py — no abre la DB).
_micro: MicroObserver | None = None
OBSERVE_TICK_SECONDS = int(os.getenv("PUMP_OBSERVE_INTERVAL_SECONDS", "60"))
# Trade forensics (Fases 7+8+9). Autopsia de cada trade (read-only sobre lo que
# el bot ya hace). None hasta arranque del app.
_forensics: ForensicsStore | None = None
# FASE 2 + observabilidad (fases 4/5/6): máquina de estados Candidate→…→Entry +
# Decision Log. Lee ventanas de micro_snapshots y las puntúa (scores.py). En modo
# shadow (default) solo observa y registra; en enforcing gobierna las entradas.
_pipeline: Pipeline | None = None
PIPELINE_TICK_SECONDS = int(os.getenv("PUMP_FSM_TICK_SECONDS", "60"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Self-migration (Phase D): ensure the bot's Supabase tables exist before any
    # read/write. Runs only when SUPABASE_DB_URL is set; otherwise a safe no-op.
    try:
        mig = await db_migrate.run()
        logger.info("db self-migration: %s", mig)
    except Exception:
        logger.exception("db self-migration call failed")
    # Async DB writer (Phase D-0.7): drains trade-path writes in the background so
    # the event-driven exit path never blocks on Supabase/SQLite I/O.
    await store.start_writer()
    # Analytics layer (Phase D): persist each closed trade through the async queue
    # (never blocks trading) and rehydrate history so metrics survive restarts.
    get_analytics().persist = lambda row: store.enqueue(
        lambda r=row: store.upsert_trade_analytics(r))
    try:
        arows = await store.list_trade_analytics()
        an = get_analytics().load_rows(arows)
        if an:
            logger.info("analytics: restored %d trade records", an)
    except Exception:
        logger.exception("startup analytics restore failed")
    # Sync_State_on_Startup: rebuild open positions before the loops start so the
    # exit engine never loses Phase 1/2 context after a restart.
    try:
        await auth_mod.load_users()
    except Exception:
        logger.exception("startup user load failed")
    try:
        await _restore_positions()
    except Exception:
        logger.exception("startup position restore failed")
    # Learning persistence: rehydrate MFE/MAE/lead-time outcomes so the optimizer
    # accumulates knowledge across restarts instead of starting from zero.
    try:
        rows = await store.list_learning_outcomes()
        n = _lab.load_rows(rows)
        if n:
            logger.info("learning: restored %d outcomes from store", n)
    except Exception:
        logger.exception("startup learning restore failed")
    # Continuous learning of the entry gate: restore the adaptive threshold the
    # bot tuned in previous sessions (was reset to the initial value every boot).
    try:
        saved = await store.get_state("adaptive_threshold")
        if saved is not None:
            global _adaptive_threshold
            _adaptive_threshold = max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, float(saved)))
            logger.info("threshold restored from store: %.1f", _adaptive_threshold)
    except Exception:
        logger.exception("threshold restore failed")
    # Microstructure recorder (FASE 1): inicia el observador y re-siembra su
    # watchlist desde la DB local para no perder pre-historia tras un reinicio.
    global _micro, _forensics, _pipeline
    try:
        _micro = MicroObserver()
        _micro.warm_start()
    except Exception:
        logger.exception("microstructure recorder init failed")
        _micro = None
    try:
        _forensics = ForensicsStore()
    except Exception:
        logger.exception("forensics store init failed")
        _forensics = None
    try:
        _pipeline = Pipeline()
        logger.info("pipeline FSM iniciado en modo %s", _pipeline.mode)
    except Exception:
        logger.exception("pipeline FSM init failed")
        _pipeline = None
    tasks = [
        asyncio.create_task(_auto_scan_loop()),
        asyncio.create_task(_grid_tick_loop()),
        asyncio.create_task(_monitor_loop()),
        asyncio.create_task(_velocity_loop()),
        asyncio.create_task(_account_loop()),
        asyncio.create_task(_grid_sync_loop()),
        asyncio.create_task(_daily_discover_loop()),
        asyncio.create_task(_observe_loop()),
        asyncio.create_task(_pipeline_loop()),
        asyncio.create_task(_optimization_loop()),
        asyncio.create_task(_grid_summary_loop()),
        asyncio.create_task(_websocket_loop()),
        asyncio.create_task(_learning_flush_loop()),
        asyncio.create_task(_regime_loop()),
        asyncio.create_task(_analytics_snapshot_loop()),
    ]
    asyncio.create_task(notify.send_system(
        f"🟢 <b>Bot iniciado</b> · modo {os.getenv('PUMP_EXEC_MODE', 'paper')} · "
        f"escaneando {', '.join(_scan_exchanges())}"
    ))
    try:
        yield
    finally:
        try:
            await notify.send_system("🔴 <b>Bot detenido</b>")
        except Exception:
            pass
        for task in tasks:
            task.cancel()
        await _velocity.close()
        await grid_sync.close()
        if _micro is not None:
            await _micro.close()
        if _forensics is not None:
            _forensics.close()
        if _pipeline is not None:
            _pipeline.close()
        if USE_WEBSOCKETS:
            try:
                await get_manager().stop()
            except Exception:
                pass
        try:
            await marketdata.close_all()
        except Exception:
            pass
        # Final learning flush so the last window of MFE/MAE isn't lost on shutdown.
        try:
            await store.upsert_learning_outcomes(_lab.export_rows())
        except Exception:
            pass
        await store.close()


# --- Global kill switch auto-trigger (capital protection) -------------------
# Halts ALL bots when the market/data layer turns unhealthy: repeated scan
# failures (rate-limit storms, exchange outages) or a sudden collapse in market
# volume. Auto-set halts carry an "auto:" reason and auto-clear once the market
# recovers; a kill switch set MANUALLY via the API is never auto-cleared.
KILL_FAIL_LIMIT = int(os.getenv("PUMP_KILL_FAIL_LIMIT", "3"))
KILL_VOL_DROP_PCT = float(os.getenv("PUMP_KILL_VOL_DROP_PCT", "60"))


class _KillMonitor:
    def __init__(self) -> None:
        self.fails = 0
        self.vol_baseline = 0.0    # EMA of healthy total scan volume
        self.auto_active = False

    def _halt(self, reason: str) -> None:
        self.auto_active = True
        for bot in all_bots():
            bot.guard.set_kill_switch(True, f"auto: {reason}")
        logger.warning("KILL SWITCH ON (auto): %s", reason)

    def _resume(self) -> None:
        self.auto_active = False
        for bot in all_bots():  # lift only the halts WE set; leave manual ones
            if bot.guard.kill_switch and bot.guard.kill_reason.startswith("auto:"):
                bot.guard.set_kill_switch(False, "auto-recover")
        logger.warning("KILL SWITCH OFF (auto-recover): market healthy")

    async def on_failure(self, exc: Exception) -> None:
        self.fails += 1
        if self.auto_active:
            return
        msg = repr(exc).lower()
        rate_limited = any(k in msg for k in ("rate", "429", "too many", "ddos"))
        if self.fails >= KILL_FAIL_LIMIT or (rate_limited and self.fails >= 2):
            why = "rate-limit storm" if rate_limited else f"{self.fails} fallos de escaneo seguidos"
            self._halt(why)
            await notify.send_system(f"KILL SWITCH (auto): {why}. Trading detenido.")

    async def on_success(self, total_volume: float) -> None:
        self.fails = 0
        crash = (self.vol_baseline > 0 and 0 < total_volume
                 < self.vol_baseline * (1 - KILL_VOL_DROP_PCT / 100))
        if total_volume > 0:  # slow EMA; decays during a halt so it self-heals
            alpha = 0.1 if crash else 0.3
            self.vol_baseline = (total_volume if self.vol_baseline == 0
                                 else (1 - alpha) * self.vol_baseline + alpha * total_volume)
        if crash and not self.auto_active:
            self._halt("caída brusca de volumen de mercado")
            await notify.send_system("KILL SWITCH (auto): caída brusca de volumen. Trading detenido.")
        elif self.auto_active and not crash:
            self._resume()
            await notify.send_system("Mercado recuperado. KILL SWITCH desactivado (auto). Trading reanudado.")


_kill = _KillMonitor()


# --- Real-time price feed (WebSockets) --------------------------------------
# Pushes sub-second prices into the live candidate objects so the exit monitor +
# dump detector react instantly. FAIL-SAFE: if WS is off or a socket is down,
# every consumer falls back to REST polling (see scanner.get_price usage).
WS_RESYNC_SECONDS = int(os.getenv("PUMP_WEBSOCKET_RESYNC_SECONDS", "120"))


# Coalesces bursty WS ticks per position key: while one tick is being evaluated
# for a symbol, later ticks for the SAME symbol are dropped (the cache already
# holds the freshest price and the next tick re-evaluates). Stops task pile-up.
_evaluating: set[str] = set()


async def on_websocket_price(exchange: str, symbol: str, price: float) -> None:
    """WebSocket PRICE_UPDATE handler — the CRITICAL TRADING PATH (Phase D-0.7).

    A held position reacts to THIS tick immediately: trailing / hard-stop / dump /
    break-even fire in milliseconds, not on the next safety-net poll. The exit
    logic (pm.step) is byte-for-byte the same as the poll path — only the trigger
    changes from 'every N seconds' to 'on each price event'. The _monitor_loop
    stays as reconciliation + volume-aware time-stop + learning-lab + a backstop
    for symbols whose socket is silent (low-liquidity / unsupported exchange)."""
    if price <= 0:
        return
    c = _candidates.get(f"{exchange}:{symbol}")
    if c is not None:
        c.last_price = price

    key = f"{exchange}:{symbol}"
    if key in _evaluating:
        return
    holders = []
    for bot in all_bots():
        pos = bot.pm.positions.get(key)
        if pos is not None and not pos.closed:
            holders.append((bot, pos))
    if not holders:
        return
    get_analytics().observe_price(key, price)   # MAE tracking (analytics)
    _evaluating.add(key)
    try:
        for bot, pos in holders:
            # Telemetry: this exit (if any) was triggered by a live WS tick.
            pos.exit_source = "ws"
            pos.exit_price_age_ms = marketdata.ws_age_ms(symbol, exchange)
            for event in bot.pm.step(key, price):
                await _handle_exit(bot, pos, event)
    except Exception:
        logger.exception("event-driven exit eval failed for %s", key)
    finally:
        _evaluating.discard(key)


async def _websocket_loop() -> None:
    """Keep WS subscriptions in sync with the current candidates + open positions
    (they rotate each scan). Re-subscribes every WS_RESYNC_SECONDS."""
    if not USE_WEBSOCKETS:
        return
    mgr = get_manager()
    mgr.add_callback(on_websocket_price)
    while True:
        try:
            pairs = {(c.exchange, c.symbol) for c in list(_candidates.values())}
            for bot in all_bots():
                for p in list(bot.pm.positions.values()):
                    if not p.closed:
                        pairs.add((p.exchange, p.symbol))
            if pairs:
                await mgr.resync(list(pairs))
        except Exception:
            logger.exception("websocket resync failed")
        await asyncio.sleep(WS_RESYNC_SECONDS)


async def _auto_scan_loop() -> None:
    """Run a scan on startup, then every SCAN_INTERVAL_SECONDS, forever."""
    while True:
        try:
            await _perform_scan()
            logger.info("auto-scan done: %d candidates", len(_candidates))
            await _kill.on_success(sum((c.quote_volume_24h or 0.0) for c in _candidates.values()))
        except Exception as exc:
            logger.exception("auto-scan failed")
            await _kill.on_failure(exc)
            await notify.send_error("Scan loop", repr(exc))
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


_last_grid_fill_at = ""
GRID_FILL_ALERTS = os.getenv("PUMP_GRID_FILL_ALERTS", "true").lower() == "true"


async def _grid_tick_loop() -> None:
    """When the grid is running, fetch a live price and advance the grid. Notifies
    each NEW fill (open/close) in real time with PnL ([GRID] tag)."""
    global _last_grid_fill_at
    while True:
        try:
            if _grid.running and _grid.grid:
                price = await fetch_price(_grid.pair)
                if price > 0:
                    _grid.step(price)
                    if GRID_FILL_ALERTS:
                        # Fills nuevos = los que tienen 'at' posterior al último visto
                        # (robusto al cap de la lista; 'at' ISO ordena cronológico).
                        new = [f for f in _grid.fills if f.at > _last_grid_fill_at]
                        for f in new:
                            await notify.send_telegram(notify.format_grid_fill(
                                _grid.pair, f.side, f.price, f.qty, f.pnl))
                        if _grid.fills:
                            _last_grid_fill_at = _grid.fills[-1].at
        except Exception as exc:
            logger.exception("grid tick failed")
            await notify.send_error("Grid tick", repr(exc))
        await asyncio.sleep(GRID_TICK_SECONDS)


GRID_SUMMARY_SECONDS = int(os.getenv("PUMP_GRID_SUMMARY_SECONDS", "3600"))  # 1h


async def _grid_summary_loop() -> None:
    """Resumen periódico del Grid Bot ([GRID]): estado, PnL realizado/no realizado,
    equity. Silencia el reenvío si el equity no cambió >= umbral ($5)."""
    while True:
        await asyncio.sleep(GRID_SUMMARY_SECONDS)
        try:
            if _grid.running:
                s = _grid.stats()
                if notify.grid_summary_changed(s.get("equity", 0.0) or 0.0):
                    await notify.send_telegram(notify.format_grid_summary(s))
        except Exception:
            logger.exception("grid summary loop failed")


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
        except Exception as exc:
            logger.exception("daily discover failed")
            await notify.send_error("Daily discover", repr(exc))
        await asyncio.sleep(86400)


async def _grid_sync_loop() -> None:
    """Mirror the embedded GRVTBot state into Supabase on a timer (best-effort)."""
    if not store.enabled():
        return
    while True:
        try:
            await grid_sync.sync_once()
        except Exception as exc:
            logger.exception("grid sync failed")
            await notify.send_error("Grid sync", repr(exc))
        await asyncio.sleep(GRID_SYNC_SECONDS)


async def _monitor_loop() -> None:
    """Tick every open managed position against a live price and run exits, for
    every user's bot (each account's positions are isolated).

    Execution upgrade (Phase D-0.5): prices come from the WebSocket-first unified
    cache (marketdata) over a PERSISTENT ccxt pool, and every price/volume read for
    the tick runs CONCURRENTLY (asyncio.gather) instead of one-by-one. The exit
    logic itself (pm.step) is byte-for-byte unchanged — only the data source and
    the I/O concurrency change."""
    while True:
        try:
            # 1) Collect every open position across all bots.
            jobs: list[tuple] = []  # (bot, key, pos)
            for bot in all_bots():
                for key, pos in list(bot.pm.positions.items()):
                    if not pos.closed:
                        jobs.append((bot, key, pos))

            # 2) Distinct symbols needing a price: open positions + learning-lab
            #    tracked alerts. One concurrent WS-first fetch each.
            lab_syms = _lab.active_symbols()
            price_syms = list({(p.exchange, p.symbol) for _, _, p in jobs} | set(lab_syms))
            price_res = await asyncio.gather(
                *(marketdata.get_price(sym, exch) for (exch, sym) in price_syms),
                return_exceptions=True,
            )
            prices: dict[tuple, float] = {}
            sources: dict[tuple, str] = {}
            for (exch, sym), res in zip(price_syms, price_res):
                if isinstance(res, tuple):
                    prices[(exch, sym)], sources[(exch, sym)] = res
                else:
                    prices[(exch, sym)], sources[(exch, sym)] = 0.0, "none"

            # 3) Volume only for held symbols (volume-aware time-stop fuel), concurrent.
            held_syms = list({(p.exchange, p.symbol) for _, _, p in jobs})
            vol_res = await asyncio.gather(
                *(marketdata.get_1m_volume(sym, exch) for (exch, sym) in held_syms),
                return_exceptions=True,
            )
            vols: dict[tuple, float] = {}
            for (exch, sym), res in zip(held_syms, vol_res):
                vols[(exch, sym)] = res if isinstance(res, (int, float)) else 0.0

            # 4) Step each position (sync, in-memory — order-consistent, no races).
            for bot, key, pos in jobs:
                skey = (pos.exchange, pos.symbol)
                price = prices.get(skey, 0.0)
                if price <= 0:
                    continue
                get_analytics().observe_price(key, price)   # MAE tracking (analytics)
                # Stamp the data source for exit telemetry BEFORE stepping, so a
                # triggered exit records where its price came from + WS feed age.
                pos.exit_source = sources.get(skey, "")
                pos.exit_price_age_ms = (marketdata.ws_age_ms(pos.symbol, pos.exchange)
                                         if pos.exit_source == "ws" else None)
                vol = vols.get(skey) or None
                for event in bot.pm.step(key, price, volume=vol):
                    await _handle_exit(bot, pos, event)

            # 5) Learning lab: track each alerted token's MFE/MAE/lead vs live price.
            for exch, sym in lab_syms:
                price = prices.get((exch, sym), 0.0)
                if price > 0:
                    _lab.step(exch, sym, price)
            _lab.settle_due()
        except Exception as exc:
            logger.exception("monitor loop failed")
            await notify.send_error("Monitor loop (exits)", repr(exc))
        await asyncio.sleep(GRID_TICK_SECONDS)


LEARNING_FLUSH_SECONDS = int(os.getenv("PUMP_LEARNING_FLUSH_SECONDS", "60"))


async def _learning_flush_loop() -> None:
    """Persist learning outcomes on a timer so MFE/MAE/lead-time survive a restart
    (best-effort; failures never touch trading). Updates active + settled rows."""
    while True:
        await asyncio.sleep(LEARNING_FLUSH_SECONDS)
        try:
            await store.upsert_learning_outcomes(_lab.export_rows())
        except Exception:
            logger.exception("learning flush failed")


REGIME_TICK_SECONDS = int(os.getenv("PUMP_REGIME_TICK_SECONDS", "300"))
ANALYTICS_SNAPSHOT_SECONDS = int(os.getenv("PUMP_ANALYTICS_SNAPSHOT_SECONDS", "1800"))
REGIME_REF = os.getenv("PUMP_REGIME_REF_SYMBOL", "BTC/USDT")
REGIME_REF_EXCHANGE = os.getenv("PUMP_REGIME_REF_EXCHANGE", "binance")


async def _regime_loop() -> None:
    """Classify the market regime (bull/bear/sideways · high/low vol) off a
    reference asset and stamp it on the analytics engine. Pure measurement — the
    regime is stored on each trade for segmentation, it never gates trading."""
    while True:
        try:
            daily = await marketdata.closes(REGIME_REF, REGIME_REF_EXCHANGE, "1d", 8)
            hourly = await marketdata.closes(REGIME_REF, REGIME_REF_EXCHANGE, "1h", 24)
            trend, _ = analytics.classify_regime(daily)
            _, vol = analytics.classify_regime(hourly)
            if trend != "unknown" or vol != "unknown":
                get_analytics().set_regime(trend, vol)
        except Exception:
            logger.exception("regime detection failed")
        await asyncio.sleep(REGIME_TICK_SECONDS)


async def _analytics_snapshot_loop() -> None:
    """Persist a headline-metrics snapshot on a slow timer so edge/expectancy/PF
    trends can be charted over time (Module 2/3 historical snapshots)."""
    while True:
        await asyncio.sleep(ANALYTICS_SNAPSHOT_SECONDS)
        try:
            eng = get_analytics()
            if not eng.trades:
                continue
            ex = analytics.expectancy_of(eng.trades)
            pf = analytics.profit_factor_of(eng.trades)
            dd = analytics.drawdown_of(eng.trades)
            store.enqueue(lambda: store.insert_metrics_snapshot({
                "at": datetime.now(UTC).isoformat(),
                "trades": len(eng.trades),
                "win_rate": ex["win_rate"], "expectancy": ex["expectancy"],
                "profit_factor": (pf["profit_factor"] if pf["profit_factor"] != "inf" else None),
                "max_drawdown": dd["max_drawdown"], "net_equity": dd["net_equity"],
                "regime": eng.regime, "edge_status": eng.edge_status().get("status"),
            }))
        except Exception:
            logger.exception("analytics snapshot failed")


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
                if current_mode() != ExecMode.paper:
                    continue
                candidate.last_price = t.price  # fire at the fresh trigger price
                _record_learning(
                    candidate.symbol, "velocity_trigger", "paper", candidate,
                    f"vol accel {t.accel:.1f}x @ {t.price}",
                )
                # Velocity = a move already underway = LATE. Off by default; the
                # trigger is still recorded above (alert/observation) but does not
                # buy unless momentum chasing is explicitly re-enabled.
                if not VELOCITY_AUTOENTRY:
                    continue
                # Every user's bot enters independently — only if THAT user has
                # auto-entry on and isn't already in the symbol.
                for bot in all_bots():
                    if bot.auto_entry and not bot.pm.has(t.exchange, t.symbol):
                        await _auto_enter(bot, candidate, accel=t.accel)
        except Exception as exc:
            logger.exception("velocity loop failed")
            await notify.send_error("Velocity loop", repr(exc))
        await asyncio.sleep(VELOCITY_TICK_SECONDS)


async def _account_loop() -> None:
    """Refresh the real read-only account balance when the owner's keys exist.
    No keys → does nothing (paper balance stays). Never trades, read-only. Keys
    are a single env set today, so this populates the owner bot only."""
    while True:
        try:
            acct = await real_balances()
            if acct.get("has_keys"):
                get_bot(OWNER_UID).real_account = acct
                for snap in acct.get("snapshots", []):
                    await store.insert_account_snapshot({**snap, "user_id": OWNER_UID})
        except Exception as exc:
            logger.exception("account loop failed")
            await notify.send_error("Account loop", repr(exc))
        await asyncio.sleep(ACCOUNT_POLL_SECONDS)


async def _observe_loop() -> None:
    """FASE 1: cada minuto graba la microestructura de los símbolos observados.
    Independiente del trading — solo recolección de datos. Nunca tumba el bot."""
    while True:
        try:
            if _micro is not None:
                n = await _micro.observe_once()
                if n:
                    logger.info("microstructure: grabadas %d filas", n)
        except Exception as exc:
            logger.exception("observe loop failed")
            await notify.send_error("Observe loop (microstructure)", repr(exc))
        await asyncio.sleep(OBSERVE_TICK_SECONDS)


def _find_candidate(exchange: str, symbol: str) -> TokenCandidate | None:
    for c in _candidates.values():
        if c.exchange.lower() == exchange.lower() and c.symbol.upper() == symbol.upper():
            return c
    return None


def _candidate_from_micro(exchange: str, symbol: str, intent_score: int) -> TokenCandidate | None:
    """FSM confirmed a PRE-PUMP entry but the scan loop already cleared the
    candidate from _candidates (confirmation takes minutes; the scan refreshes
    faster). Rebuild a minimal candidate from the latest micro_snapshot (<=60s
    old) so the early signal is NOT lost. Carries the real book metrics the
    ForensicFilter + RiskGuard need — no fabricated data."""
    if _micro is None:
        return None
    try:
        rows = _micro.store.recent(symbol, exchange, 10)
    except Exception:
        return None
    if not rows:
        return None
    r = rows[-1]
    price = float(r.get("last_price") or 0.0)
    if price <= 0:
        return None
    velocity = float(r.get("velocity") or 1.0)
    imbalance = float(r.get("imbalance") or 0.0)
    # Derive the cluster from the live book so the exit profile differentiates:
    # flat + stacked bids = classic (grind); volume surging = long_pump (impulse).
    cluster = _cluster(price_change_pct=0.0, volume_spike=velocity, imbalance=imbalance)
    return TokenCandidate(
        id=str(uuid4()), symbol=symbol.upper(), exchange=exchange.lower(),
        last_price=price, quote_volume_24h=0.0, price_change_pct_24h=0.0,
        volume_spike=velocity,
        orderbook_imbalance=imbalance,
        liquidity_usd=float(r.get("liquidity_usd") or 0.0),
        pump_score=int(intent_score), confidence_score=100,
        classification="pre_pump_accumulation", cluster=cluster,
        spread_pct=float(r.get("spread_pct") or 0.0),
        top_book_share=float(r.get("top_book_share") or 0.0),
        status=CandidateStatus.approved, updated_at=datetime.now(UTC),
    )


async def _pipeline_loop() -> None:
    """FASE 2: avanza la máquina de estados cada PIPELINE_TICK_SECONDS. En modo
    shadow solo registra en decision_log lo que HARÍA; en enforcing ejecuta los
    intents confirmados por el motor de ejecución actual (sin tocar TP/SL/risk).
    Best-effort: nunca tumba el bot."""
    while True:
        try:
            if _pipeline is not None:
                intents = await asyncio.to_thread(_pipeline.tick)
                if intents and _pipeline.mode == "enforcing":
                    for it in intents:
                        cand = _find_candidate(it.exchange, it.symbol)
                        if cand is None:
                            # Scan dropped it — rebuild from the live micro series
                            # so the PRE-PUMP signal isn't lost (the whole point).
                            cand = _candidate_from_micro(it.exchange, it.symbol, it.scores.accumulation)
                        if cand is None:
                            continue
                        entered = False
                        for bot in all_bots():
                            if not bot.auto_entry:
                                continue
                            if bot.pm.has(it.exchange, it.symbol):
                                entered = True          # already holding it
                                continue
                            if await _auto_enter(bot, cand, fsm_path=True):
                                entered = True          # real fill happened
                        # Mark 'entry' ONLY on an actual buy. If forensic/risk blocked
                        # it, stays in confirmation and retries next tick (or expires).
                        if entered:
                            _pipeline.mark_entered(it.symbol, it.exchange)
        except Exception:
            logger.exception("pipeline loop failed")
        await asyncio.sleep(PIPELINE_TICK_SECONDS)


async def _persist_position(bot: UserBot, pos) -> None:
    await store.upsert_position({
        # key carries the uid so two users holding the same symbol don't collide
        # on the unique(key) constraint; in-memory each bot keys by exchange:symbol.
        "key": f"{bot.uid}:{pos.exchange}:{pos.symbol}",
        "user_id": bot.uid,
        "symbol": pos.symbol, "exchange": pos.exchange,
        "entry_price": pos.entry_price, "qty": round(pos.qty, 8),
        "initial_qty": round(pos.initial_qty, 8), "phase": pos.phase,
        "peak_price": pos.peak_price, "last_price": pos.last_price,
        "realized_pnl": round(pos.realized_pnl, 4), "closed": pos.closed,
        "pump_score": pos.pump_score, "classification": pos.classification,
        "entry_at": pos.entry_at.isoformat(),
    })


async def _restore_positions() -> None:
    """Rebuild every user's open positions + equity curve from Supabase on startup
    so Phase 1/2 context and balances survive a restart (Sync_State_on_Startup)."""
    if not store.enabled():
        return
    # One bot per known account: the owner plus every app_users row.
    uids = [OWNER_UID] + [u["id"] for u in auth_mod.list_users() if u.get("id") and not u.get("owner")]
    ensure_bots(uids)
    total = 0
    for bot in all_bots():
        rows = await store.list_open_positions(user_id=bot.uid)
        for r in rows:
            try:
                entry_at = datetime.fromisoformat(r["entry_at"]) if r.get("entry_at") else datetime.now(UTC)
                pos = ManagedPosition(
                    symbol=r["symbol"], exchange=r["exchange"],
                    entry_price=float(r["entry_price"]), qty=float(r["qty"]),
                    initial_qty=float(r.get("initial_qty") or r["qty"]),
                    entry_at=entry_at,
                    peak_price=float(r.get("peak_price") or r["entry_price"]),
                    peak_at=datetime.now(UTC),
                    phase=int(r.get("phase") or 1),
                    realized_pnl=float(r.get("realized_pnl") or 0.0),
                    last_price=float(r.get("last_price") or r["entry_price"]),
                    pump_score=int(r.get("pump_score") or 0),
                    classification=r.get("classification") or "n/a",
                )
                bot.pm.positions[bot.pm.key(pos.exchange, pos.symbol)] = pos
                total += 1
            except Exception:
                logger.exception("restore position failed for row %s", r)
        # Rehydrate this bot's equity curve so the chart isn't blank after restart.
        try:
            pts = await store.list_equity(200, user_id=bot.uid)
            if pts:
                bot.equity_history.clear()
                bot.equity_history.extend({"t": p.get("t"), "v": float(p.get("v") or 0)} for p in pts)
        except Exception:
            logger.exception("equity restore failed for %s", bot.uid)
        # Rehydrate REALIZED P&L so the paper balance doesn't reset to the base
        # capital on restart (carry = all prior exits; pm.history stays this-session
        # only → no double count). carry_exits feeds the 7d P&L figure.
        try:
            exits = await store.list_exits(user_id=bot.uid, limit=5000)
            bot.realized_carry = round(sum(float(r.get("pnl") or 0.0) for r in exits), 4)
            bot.carry_exits = [{"at": r.get("at"), "pnl": float(r.get("pnl") or 0.0)}
                               for r in exits[:500] if r.get("at")]
            if bot.realized_carry:
                logger.info("restored realized P&L %.2f for %s (%d exits)",
                            bot.realized_carry, bot.uid, len(exits))
        except Exception:
            logger.exception("realized P&L restore failed for %s", bot.uid)
    if total:
        logger.info("restored %d open positions across %d bots", total, len(all_bots()))
        await notify.send_system(f"🔄 <b>Estado recuperado</b> · {total} posiciones abiertas reconstruidas")


async def _handle_exit(bot: UserBot, pos, event) -> None:
    pct = round(event.fraction * 100)
    # Event bus (Phase D-0.7): publish the specific trigger (trailing / stop /
    # dump / break-even / time-stop) for observers + /diagnostics throughput.
    trig = EXIT_REASON_EVENT.get(event.reason)
    if trig is not None:
        get_bus().emit(trig, symbol=pos.symbol, exchange=pos.exchange,
                       price=event.price, pnl=event.pnl, user_id=bot.uid)
    _record_learning_raw(
        pos.symbol, f"exit_{event.reason}", "paper", pos.pump_score, pos.classification,
        f"sold {pct}% @ {event.price} pnl {event.pnl:+.2f}",
    )
    # Trade-path DB writes go through the async queue: the exit (paper sale) is
    # already executed inside pm.step — this is bookkeeping, so it must NOT delay
    # the next position's evaluation on the event path.
    store.enqueue(lambda r={**event.__dict__, "user_id": bot.uid}: store.insert_exit(r))
    store.enqueue(lambda: _persist_position(bot, pos))
    store.enqueue(lambda reason=event.reason, sym=pos.symbol, p=event.price,
                  q=pct, pnl=event.pnl: store.insert_bot_log(
        "PUMP_SCANNER",
        "PANIC_SELL" if reason in ("dump", "hard_stop") else "TRADE_SELL",
        f"{reason} {sym} sold {q}% @ {p}",
        pnl=pnl,
    ))
    if event.closed:
        # Full close → close card with overall PnL%.
        cost = pos.entry_price * pos.initial_qty
        pnl_pct = (pos.realized_pnl / cost * 100) if cost > 0 else 0.0
        # Forensics (Fase 7/8): finaliza la fila del trade con el contexto de SALIDA.
        if _forensics is not None:
            try:
                await asyncio.to_thread(_forensics.record_exit, bot.uid, pos,
                                        event.price, pos.realized_pnl, pnl_pct, event.reason)
            except Exception:
                logger.exception("forensics record_exit failed")
        quality = bot.pm.entry_quality(pos)
        _record_learning_raw(
            pos.symbol, "trade_closed", "paper", pos.pump_score, pos.classification,
            f"realized {pos.realized_pnl:+.2f} · entry {quality}",
        )
        _apply_learning(quality, pos.realized_pnl)
        # Analytics (Phase D): build + persist the permanent TradeRecord (expectancy
        # / PF / quality / confidence-sizing simulation all derive from these).
        try:
            get_analytics().close_trade(bot.pm.key(pos.exchange, pos.symbol),
                                        pos=pos, event=event, entry_grade=quality)
        except Exception:
            logger.exception("analytics close_trade failed")
        # Exit-engine telemetry (Phase D-0.5): signal/entry/exit timing, where the
        # triggering price came from, and how the protective stops armed.
        tel_row = {
            "symbol": pos.symbol, "exchange": pos.exchange, "cluster": pos.cluster,
            "signal_at": pos.signal_at.isoformat() if pos.signal_at else None,
            "entry_at": pos.entry_at.isoformat(), "exit_at": event.at,
            "exit_reason": event.reason,
            "be_activated_at": pos.be_at.isoformat() if pos.be_at else None,
            "trail_activated_at": pos.trail_at.isoformat() if pos.trail_at else None,
            "holding_secs": round((datetime.now(UTC) - pos.entry_at).total_seconds(), 1),
            "exit_source": pos.exit_source or None,
            "ws_reaction_delay_ms": pos.exit_price_age_ms,
            "pnl": round(pos.realized_pnl, 4), "user_id": bot.uid,
        }
        telemetry.exits.record(tel_row)
        store.enqueue(lambda row=tel_row: store.insert_exit_telemetry(row))
        get_bus().emit(EventType.POSITION_CLOSED, symbol=pos.symbol, exchange=pos.exchange,
                       reason=event.reason, pnl=round(pos.realized_pnl, 4),
                       pnl_pct=round(pnl_pct, 2), user_id=bot.uid)
        note = f"{quality.upper()} | THRESHOLD: {round(_adaptive_threshold)}"
        await notify.send_exit(
            notify.format_exit(pos.symbol, pos.exchange, event.price, pnl_pct, event.reason, note)
        )
    else:
        # Partial take-profit (rest keeps running).
        get_bus().emit(EventType.PARTIAL_EXIT, symbol=pos.symbol, exchange=pos.exchange,
                       fraction=event.fraction, pnl=event.pnl, user_id=bot.uid)
        await notify.send_entry(
            notify.format_partial(pos.symbol, pos.exchange, event.price, pct, event.pnl, event.reason)
        )


# Confirmation-threshold band. Floor 70 stops the bot drifting into reckless
# over-trading; ceiling 90 is maximally selective.
THRESHOLD_FLOOR = float(os.getenv("PUMP_THRESHOLD_FLOOR", "40"))   # aggressive floor (was 70) so the optimizer can keep entering while it learns
THRESHOLD_CEIL = float(os.getenv("PUMP_THRESHOLD_CEIL", "90"))


def _apply_learning(quality: str, pnl: float) -> None:
    """Loss-averse feedback loop mejorado para ajuste más rápido."""
    global _adaptive_threshold
    # Si pierdes, sube el umbral (más selectivo)
    if pnl < 0:
        _adaptive_threshold = min(THRESHOLD_CEIL, _adaptive_threshold + 3)
    # Si ganas con una entrada temprana, baja el umbral (más agresivo)
    elif quality == "early_entry" and pnl > 5:
        _adaptive_threshold = max(THRESHOLD_FLOOR, _adaptive_threshold - 2)
    # Si ganas pero entraste tarde, no toques el umbral
    elif quality == "late_entry" and pnl > 0:
        pass  # mantener
    # Si pierdes por Time Out, sube el umbral (más selectivo)
    elif quality == "timeout" and pnl < 0:
        _adaptive_threshold = min(THRESHOLD_CEIL, _adaptive_threshold + 5)

    # Limitar el umbral al rango permitido
    _adaptive_threshold = max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, _adaptive_threshold))
    _persist_threshold()


def _persist_threshold() -> None:
    """Guarda el umbral aprendido para que el aprendizaje de la puerta de entrada
    sea CONTINUO entre reinicios (antes se perdía en cada arranque)."""
    store.enqueue(lambda: store.set_state("adaptive_threshold", str(round(_adaptive_threshold, 2))))


async def _optimize_tp_sl() -> None:
    """Optimiza el Trailing Stop (PUMP_DYNAMIC_STOP_PCT) y el Hard Stop
    (PUMP_STOP_LOSS_PCT) cada 24h a partir del forensics. Ya NO ajusta un
    take-profit fijo: el motor sale por trailing dinámico sobre el pico, así que
    el optimizador afina la distancia de giveback en vez de un TP que nadie usa."""
    global _forensics
    if _forensics is None:
        return
    try:
        opt = await asyncio.to_thread(_forensics.optimize_tp_sl)
        if opt.get("sl") and opt.get("avg_loss") is not None:
            # Trailing ~1.5x la pérdida típica: no te sacude el ruido normal pero
            # banca la corrida antes del round-trip. Clamp a una banda sana [3,10].
            trail = max(3.0, min(10.0, round(opt["avg_loss"] * 1.5, 1)))
            os.environ["PUMP_DYNAMIC_STOP_PCT"] = str(trail)
            os.environ["PUMP_STOP_LOSS_PCT"] = str(opt["sl"])
            logger.info(f"✅ Trailing/Hard optimizado: Trailing={trail}%, HardStop={opt['sl']}% | Avg Win: {opt['avg_win']:.1f}%, Avg Loss: {opt['avg_loss']:.1f}%")
    except Exception:
        logger.exception("Trailing/SL optimization failed")


async def _optimize_timeout() -> None:
    """Optimiza Timeout basado en learning cada 24h."""
    global _lab
    try:
        opt = await asyncio.to_thread(_lab.optimize_timeout)
        if opt.get("timeout"):
            os.environ["PUMP_TIMEOUT_MINUTES"] = str(opt["timeout"])
            logger.info(f"✅ Timeout optimizado: {opt['timeout']} min | Avg Lead: {opt['avg_lead']:.1f}min, Std: {opt['std_lead']:.1f}min")
    except Exception:
        logger.exception("Timeout optimization failed")


async def _optimization_loop() -> None:
    """Bucle que ejecuta la optimización de parámetros cada 24h."""
    global _adaptive_threshold
    while True:
        await asyncio.sleep(86400)  # 24 horas
        try:
            await _optimize_tp_sl()
            await _optimize_timeout()

            metrics = await asyncio.to_thread(_lab.metrics)
            if metrics.get("precision") is not None:
                precision = metrics["precision"]
                if precision < 0.4:
                    new_threshold = _adaptive_threshold + 5
                elif precision > 0.6:
                    new_threshold = _adaptive_threshold - 5
                else:
                    new_threshold = _adaptive_threshold
                # BUGFIX: this loop used max(30, …) which let the threshold sink
                # below the learning floor (70) → bot entered weak signals (score
                # 45) → timeout losses. Clamp to the SAME [FLOOR, CEIL] band the
                # learning path uses so the two can't fight each other.
                _adaptive_threshold = max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, new_threshold))
                _persist_threshold()
                logger.info(f"✅ Umbral ajustado: {_adaptive_threshold} (precisión: {precision:.0%})")
        except Exception:
            logger.exception("Optimization loop failed")


app = FastAPI(title="TradeOS AI Pump Reader", version="0.4.0", lifespan=lifespan)

# Same-origin reverse proxy to the real GRVTBot (Node) under /grid/*.
register_grvt_proxy(app)

_PUBLIC_PATHS = {"/login", "/logout", "/health"}

# Admin-only page to create/manage the per-user accounts. Same dark palette as
# the dashboard + login. Reached at /admin (gated to role=admin in _auth_gate).
ADMIN_USERS_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TradeOS AI · Cuentas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  *{box-sizing:border-box} body{margin:0;font-family:Geist,system-ui,sans-serif;background:#080b11;color:#e7ebf2;padding:26px;
    background-image:radial-gradient(900px 460px at 12% -8%,rgba(160,92,242,.10),transparent)}
  .wrap{max-width:820px;margin:0 auto}
  .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
  .brand{display:flex;align-items:center;gap:11px}
  .brand .dot{width:26px;height:26px;border-radius:8px;
    background:radial-gradient(circle at 30% 30%,#d9b8ff,#a05cf2 55%,#6a2bb0);
    box-shadow:0 0 0 1px rgba(160,92,242,.35),0 5px 15px -5px rgba(160,92,242,.55)}
  h1{font-size:18px;margin:0;font-weight:600;letter-spacing:-.01em}
  a.back{color:#6f7a8e;text-decoration:none;font-size:13px;transition:color .15s} a.back:hover{color:#a05cf2}
  .card{background:rgba(16,21,30,.55);border:1px solid rgba(255,255,255,.07);border-radius:16px;padding:20px;margin-bottom:16px;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 18px 40px -26px rgba(0,0,0,.7)}
  .card h2{font-size:11px;margin:0 0 14px;color:#6f7a8e;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
  .row{display:flex;gap:11px;flex-wrap:wrap;align-items:end}
  .fld{flex:1;min-width:150px} label{display:block;font-size:11px;color:#6f7a8e;margin:0 0 6px;letter-spacing:.05em;text-transform:uppercase}
  input,select{width:100%;background:#0c1018;border:1px solid #1b2333;border-radius:10px;color:#e7ebf2;padding:10px 12px;font-family:inherit;font-size:13px;outline:none;transition:border-color .15s,box-shadow .15s}
  input:focus,select:focus{border-color:#a05cf2;box-shadow:0 0 0 3px rgba(160,92,242,.16)}
  button{background:linear-gradient(135deg,#b988f2,#a05cf2 55%,#7a3fd0);border:0;color:#fff;padding:10px 18px;border-radius:10px;font-weight:600;font-size:13px;cursor:pointer;font-family:inherit;box-shadow:0 10px 24px -12px rgba(160,92,242,.7);transition:transform .12s,filter .15s}
  button:hover{filter:brightness(1.06)} button:active{transform:translateY(1px)}
  button.ghost{background:transparent;border:1px solid #2a3447;color:#aeb6c6;box-shadow:none}
  button.ghost:hover{border-color:#a05cf2;color:#fff;filter:none}
  table{width:100%;border-collapse:collapse;font-size:13px} th{text-align:left;color:#6f7a8e;font-weight:500;font-size:10px;letter-spacing:.08em;text-transform:uppercase;padding:9px 10px;border-bottom:1px solid #1b2333}
  td{padding:11px 10px;border-bottom:1px solid #131923}
  .tag{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:3px 10px;border-radius:999px;background:rgba(255,255,255,.05);color:#aeb6c6}
  .tag.admin{color:#c69cff;background:rgba(160,92,242,.16)} .tag.on{color:#2fd08a;background:rgba(47,208,138,.15)} .tag.off{color:#ff6b6b;background:rgba(232,85,106,.15)}
  .msg{font-size:12px;margin-top:11px;min-height:14px} .msg.err{color:#ff6b6b} .msg.ok{color:#2fd08a}
  .acts{display:flex;gap:6px;justify-content:flex-end}
</style></head><body><div class="wrap">
  <div class="top"><div class="brand"><div class="dot"></div><h1>Cuentas · TradeOS AI</h1></div><a class="back" href="/">← Volver al panel</a></div>
  <div class="card">
    <h2>Crear cuenta</h2>
    <div class="row">
      <div class="fld"><label>Usuario</label><input id="u" autocomplete="off"/></div>
      <div class="fld"><label>Contraseña</label><input id="p" type="text" autocomplete="off"/></div>
      <div class="fld" style="max-width:150px"><label>Rol</label>
        <select id="r"><option value="operator">Operador (su bot)</option><option value="admin">Admin</option></select></div>
      <button onclick="createUser()">Crear</button>
    </div>
    <div id="cmsg" class="msg"></div>
  </div>
  <div class="card">
    <h2>Cuentas existentes</h2>
    <table><thead><tr><th>Usuario</th><th>Rol</th><th>Estado</th><th></th></tr></thead><tbody id="rows"></tbody></table>
  </div>
  <div class="card">
    <h2>Resumen de bots · todas las cuentas</h2>
    <table><thead><tr><th>Usuario</th><th>Balance</th><th>Abiertas</th><th>PnL 7d</th><th>Auto</th></tr></thead><tbody id="ovrows"></tbody></table>
  </div>
</div>
<script>
async function load(){
  const r = await fetch('/admin/users'); const d = await r.json();
  const tb = document.getElementById('rows'); tb.innerHTML='';
  for(const u of d.users){
    const tr = document.createElement('tr');
    const role = u.role==='admin' ? '<span class="tag admin">admin</span>' : '<span class="tag">operador</span>';
    const st = u.active ? '<span class="tag on">activo</span>' : '<span class="tag off">inactivo</span>';
    let acts = '';
    if(!u.owner){
      acts = '<div class="acts">'
        + '<button class="ghost" onclick="resetPw(\\''+u.id+'\\')">Reset pass</button>'
        + '<button class="ghost" onclick="toggle(\\''+u.id+'\\','+(!u.active)+')">'+(u.active?'Desactivar':'Activar')+'</button></div>';
    } else { acts = '<div class="acts"><span class="tag">dueño</span></div>'; }
    tr.innerHTML = '<td>'+u.username+'</td><td>'+role+'</td><td>'+st+'</td><td>'+acts+'</td>';
    tb.appendChild(tr);
  }
}
async function createUser(){
  const m=document.getElementById('cmsg'); m.className='msg'; m.textContent='';
  const fd=new FormData(); fd.append('username',document.getElementById('u').value);
  fd.append('password',document.getElementById('p').value); fd.append('role',document.getElementById('r').value);
  const r=await fetch('/admin/users',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ m.className='msg ok'; m.textContent='Cuenta creada.'; document.getElementById('u').value=''; document.getElementById('p').value=''; load(); }
  else { m.className='msg err'; m.textContent=d.error||'Error'; }
}
async function toggle(id,active){
  const fd=new FormData(); fd.append('active',active);
  await fetch('/admin/users/'+id+'/active',{method:'POST',body:fd}); load();
}
async function resetPw(id){
  const pw=prompt('Nueva contraseña (mín 6):'); if(!pw) return;
  const fd=new FormData(); fd.append('password',pw);
  const r=await fetch('/admin/users/'+id+'/password',{method:'POST',body:fd}); const d=await r.json();
  if(!d.ok) alert(d.error||'Error');
}
async function loadOverview(){
  const r=await fetch('/admin/overview'); const d=await r.json();
  const tb=document.getElementById('ovrows'); tb.innerHTML='';
  for(const a of d.accounts){
    const pnl=(a.pnl_7d>=0?'+':'')+Number(a.pnl_7d).toFixed(2);
    const cls=a.pnl_7d>0?'on':(a.pnl_7d<0?'off':'');
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+a.username+'</td><td>$'+Number(a.balance).toFixed(2)+'</td><td>'+a.open_positions
      +'</td><td><span class="tag '+cls+'">'+pnl+'</span></td><td>'+(a.auto_entry?'on':'off')+'</td>';
    tb.appendChild(tr);
  }
}
load(); loadOverview(); setInterval(loadOverview, 15000);
</script></body></html>"""


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Require login for everything once APP_PASSWORD is set (off in dev/paper).

    On success the decoded session ({"username","id","role"}) is attached to
    request.state.user so per-user routes know who is calling and admin-only
    routes (/admin/*, the grid) can check the role."""
    if not auth_enabled():
        request.state.user = {"id": auth_mod.OWNER_UID, "username": "dev", "role": "admin"}
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS:
        return await call_next(request)
    user = read_token(request.cookies.get(COOKIE))
    if user is None:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    request.state.user = user
    # Admin-only area: account management. The grid is per-user (each TradeOS
    # account maps to its own GRVTBot user → its own isolated grids), so any
    # logged-in user reaches /grid and only sees their own grids.
    if path.startswith("/admin") and user.get("role") != "admin":
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                "<body style='font-family:system-ui;background:#070a0f;color:#e6e9ef;"
                "display:flex;height:100vh;align-items:center;justify-content:center'>"
                "<div>Acceso solo para admin. <a style='color:#ff5a86' href='/'>Volver</a></div></body>",
                status_code=403,
            )
        return JSONResponse({"detail": "admin only"}, status_code=403)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return LOGIN_HTML.replace("<!--ERR-->", "")


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    user = authenticate(username, password)
    if user:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, make_token(user), max_age=MAX_AGE, httponly=True, samesite="lax")
        return resp
    return HTMLResponse(LOGIN_HTML.replace("<!--ERR-->", "Invalid username or password"), status_code=401)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/me")
async def whoami(request: Request) -> dict:
    """Who is logged in (the dashboard uses role to show/hide admin tools)."""
    u = getattr(request.state, "user", None) or {}
    return {"username": u.get("username"), "role": u.get("role", "operator"), "id": u.get("id")}


# --- admin: account management (admin role only; gated in _auth_gate) ---------

@app.get("/admin/users")
async def admin_list_users() -> dict:
    return {"users": auth_mod.list_users()}


@app.post("/admin/users")
async def admin_create_user(username: str = Form(...), password: str = Form(...),
                            role: str = Form("operator")) -> JSONResponse:
    try:
        created = await auth_mod.create_user(username, password, role)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # Spin up the new account's bot now so the scan/monitor loops include it.
    if created.get("id"):
        get_bot(created["id"])
    return JSONResponse({"ok": True, "user": created})


@app.post("/admin/users/{user_id}/active")
async def admin_set_active(user_id: str, active: str = Form("true")) -> dict:
    await auth_mod.set_active(user_id, active.lower() in ("1", "true", "yes", "on"))
    return {"ok": True}


@app.post("/admin/users/{user_id}/password")
async def admin_reset_password(user_id: str, password: str = Form(...)) -> JSONResponse:
    try:
        await auth_mod.set_password(user_id, password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/admin/overview")
async def admin_overview() -> dict:
    """Every account's bot at a glance (admin only). Balance / open positions /
    7d P&L per user — so the owner can see all accounts in one place."""
    rows = []
    for u in auth_mod.list_users():
        b = get_bot(u["id"])
        rows.append({
            "username": u["username"], "role": u["role"], "active": u.get("active", True),
            "balance": b.balance(), "open_positions": b.open_count(),
            "pnl_7d": b.pnl_7d(), "auto_entry": b.auto_entry,
        })
    return {"accounts": rows, "count": len(rows)}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return ADMIN_USERS_HTML


# Per-user grid JWT cache: uid -> (token, minted_at). The embedded GRVTBot is a
# single Node process but is multi-tenant natively (grid_bots are scoped by
# user_id, enforced server-side via the JWT), so each TradeOS account maps to its
# OWN GRVTBot user → its own isolated grids. No process-per-user needed.
_grid_token_cache: dict[str, tuple[str, float]] = {}


def _grid_creds(user: dict) -> tuple[str, str]:
    """Deterministic GRVTBot email + password for a TradeOS user, derived from
    the user id + the app secret. Stable across restarts (so we can re-login
    without storing a second password) and unguessable from outside."""
    import hashlib
    import hmac

    uid = str((user or {}).get("id") or "owner")
    email = f"{uid}@tradeos.local"
    secret = os.getenv("APP_SECRET_KEY", "tradeos-dev-secret-change-me").encode()
    pw = "G" + hmac.new(secret, uid.encode(), hashlib.sha256).hexdigest()[:24]  # >=8 chars
    return email, pw


@app.get("/grid-sso")
async def grid_sso(request: Request):
    """Single sign-on for the embedded GRVTBot, scoped to the logged-in user.

    The TradeOS login is the only login the user sees. This route logs the
    session user into the GRVTBot server-side (auto-creating their GRVTBot
    account on first open) and returns THAT user's JWT, so the iframe SPA boots
    showing only this person's grids. The derived grid password never reaches
    the browser. Tokens are cached per user for 12h to avoid the login limiter.
    """
    import time

    user = getattr(request.state, "user", None) or {"id": "owner"}
    uid = str(user.get("id") or "owner")
    now = time.time()
    cached = _grid_token_cache.get(uid)
    if cached and (now - cached[1]) < 12 * 3600:
        return {"ok": True, "key": "grvt-grid-token", "token": cached[0]}

    email, password = _grid_creds(user)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "http://127.0.0.1:3848/api/v2/auth/login",
                json={"email": email, "password": password},
            )
            if resp.status_code != 200:
                # First time for this user (or no account yet): create it, then
                # the returned token logs them straight in.
                resp = await client.post(
                    "http://127.0.0.1:3848/api/v2/auth/signup",
                    json={"email": email, "password": password, "terms_lang": "es"},
                )
    except httpx.HTTPError:
        if cached:
            return {"ok": True, "key": "grvt-grid-token", "token": cached[0], "cached": True}
        return JSONResponse({"ok": False, "error": "grid_offline"}, status_code=502)

    if resp.status_code == 200:
        token = resp.json().get("token")
        _grid_token_cache[uid] = (token, now)
        return {"ok": True, "key": "grvt-grid-token", "token": token}
    if cached:
        return {"ok": True, "key": "grvt-grid-token", "token": cached[0], "cached": True}
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


# A token already up this much on 24h has ALREADY pumped — the move happened. It is
# NOT a candidate to enter (the chase gate blocks it and momentum auto-entry is
# off), so showing it on the candidate boards just looks like the bot is "late".
# Hide post-pump blow-offs from the candidate views — the dashboard should show
# PRE-pump / early setups. The scan still feeds micro/FSM in the background.
POSTPUMP_HIDE_PCT = float(os.getenv("PUMP_POSTPUMP_HIDE_PCT", str(ENTRY_MAX_CHASE_PCT)))


def _is_candidate_display(c: TokenCandidate) -> bool:
    """True if this is still a forward-looking candidate (not an already-run pump)."""
    return c.price_change_pct_24h < POSTPUMP_HIDE_PCT


# Pre-pump entries trade SMALLER accumulation tokens — their books are naturally
# thinner than the big momentum gainers, so the 120k forensic floor (tuned for the
# rug fat-tail of momentum CHASING) blocks every single one → nothing ever buys.
# The FSM rug_risk score already vetted the live book for deterioration, so the
# pre-pump path uses a lower floor. Still blocks the genuinely rug-thin (<40k).
PREPUMP_MIN_LIQUIDITY_USD = float(os.getenv("PUMP_PREPUMP_MIN_LIQUIDITY_USD", "40000"))


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
        spread_pct=scanned.spread_pct,
        top_book_share=scanned.top_book_share,
        manipulation_suspect=scanned.manipulation_suspect,
        flags=scanned.flags,
        spark=scanned.spark,
        status=_status_for(scanned.pump_score),
        updated_at=datetime.now(UTC),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pump-reader"}


@app.get("/diagnostics")
async def diagnostics() -> dict:
    """Execution-quality telemetry (Phase D-0.5): WebSocket health, market-data
    source hit-rates, entry-latency stages, exit-engine reaction diagnostics."""
    try:
        ws = get_manager().health()
    except Exception:
        ws = {"error": "unavailable"}
    return {
        "websocket": ws,
        "marketdata": marketdata.stats(),
        "events": get_bus().stats(),
        "db_queue": {"depth": store._wq.qsize() if store._wq is not None else None,
                     "writer_running": store._writer_task is not None},
        "entry_latency_ms": telemetry.latency.metrics(),
        "exit_engine": telemetry.exits.summary(),
        "recent_exits": telemetry.exits.recent(20),
    }


# --- Quantitative intelligence API (Phase D analytics) ----------------------
# All read-only. Pure measurement on top of the trades the engine already makes;
# nothing here changes trading behaviour. Confidence sizing is SIMULATION-only.

@app.get("/analytics")
async def analytics_dashboard() -> dict:
    """Module 11 — headline performance dashboard + recent trades."""
    eng = get_analytics()
    return {"dashboard": eng.dashboard(), "recent_trades": eng.recent(30)}


@app.get("/expectancy")
async def analytics_expectancy() -> dict:
    return get_analytics().expectancy()


@app.get("/profit-factor")
async def analytics_profit_factor() -> dict:
    return get_analytics().profit_factor()


@app.get("/setup-ranking")
async def analytics_setup_ranking() -> dict:
    return {"leaderboard": get_analytics().setup_ranking()}


@app.get("/confidence")
async def analytics_confidence() -> dict:
    eng = get_analytics()
    setups = sorted({t.setup_type for t in eng.trades} | {"accumulation", "velocity", "momentum"})
    return {
        "distribution": eng.confidence_distribution(),
        "current_by_setup": {s: eng.confidence_for(s, "") for s in setups},
        "sizing_simulation": eng.sizing_simulation(),
    }


@app.get("/edge-monitor")
async def analytics_edge_monitor() -> dict:
    return get_analytics().edge_status()


@app.get("/drawdown")
async def analytics_drawdown() -> dict:
    return get_analytics().drawdown()


@app.get("/reports")
async def analytics_reports(period: str = "all") -> dict:
    eng = get_analytics()
    if period in ("daily", "weekly", "monthly"):
        return eng.report(period)
    return {p: eng.report(p) for p in ("daily", "weekly", "monthly")}


@app.get("/trade-quality")
async def analytics_trade_quality() -> dict:
    eng = get_analytics()
    return {
        "distribution": eng.quality_distribution(),
        "recent": [{"symbol": t.symbol, "setup": t.setup_type, "reason": t.exit_reason,
                    "quality": t.trade_quality_score, "pnl_usd": t.pnl_usd}
                   for t in eng.trades[-30:][::-1]],
    }


@app.get("/candidates", response_model=list[TokenCandidate])
async def list_candidates() -> list[TokenCandidate]:
    # Hide already-pumped blow-offs — they are not candidates to enter.
    shown = [c for c in _candidates.values() if _is_candidate_display(c)]
    return sorted(shown, key=lambda c: c.pump_score, reverse=True)


def _scan_exchanges() -> list[str]:
    raw = os.getenv("PUMP_SCAN_EXCHANGES", "binance,bitget,mexc,okx")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


async def _auto_enter(bot: UserBot, candidate: TokenCandidate, accel: float | None = None,
                      fsm_path: bool = False) -> bool:
    """Paper-only auto buy on a confirmed candidate into ONE user's bot; hand it
    to that bot's exit engine.

    Two entry paths:
      - momentum/scan path (fsm_path=False): gates (1) confidence, (2) anti-chase,
        (3) volume floor — these stop the bot from chasing a pump that already ran.
      - PRE-PUMP path (fsm_path=True): the FSM already validated sustained
        accumulation + persistence + low rug-risk over a window. That IS the
        quality signal, and it is the OPPOSITE of chasing, so the anti-chase
        gates are skipped. The CAPITAL protections (ForensicFilter liquidity/
        spread/concentration + RiskGuard) still apply to every entry.
    Returns True only if a real position was opened (so the FSM marks 'entry'
    ONLY on an actual fill — a blocked candidate must NOT show as entered)."""
    sw = telemetry.Stopwatch()  # entry-latency stages (Phase D-0.5 observability)
    if not fsm_path:
        # (1) Confidence gate — only act on signals the scanner trusts.
        if candidate.confidence_score < ENTRY_MIN_CONFIDENCE:
            _record_learning(candidate.symbol, "skip_low_confidence", "paper", candidate,
                             f"confianza {candidate.confidence_score} < {ENTRY_MIN_CONFIDENCE:.0f}")
            return False
        # (2) Momentum / exhaustion gate — the #1 source of "enter then time out at
        # a small loss" churn is chasing finished or thin pumps.
        if candidate.price_change_pct_24h >= ENTRY_MAX_CHASE_PCT:
            _record_learning(candidate.symbol, "skip_exhausted", "paper", candidate,
                             f"+{candidate.price_change_pct_24h:.0f}% 24h ya corrido (chase)")
            return False
        # Volume floor applies to scan-path entries. Velocity-path entries (accel
        # set) already proved live acceleration + rising price, so they skip this.
        if accel is None and candidate.volume_spike < ENTRY_MIN_VOL_SPIKE:
            _record_learning(candidate.symbol, "skip_low_volume", "paper", candidate,
                             f"vol spike {candidate.volume_spike:.1f}x < {ENTRY_MIN_VOL_SPIKE}x")
            return False

    # (3) ForensicFilter — ALWAYS applies (capital integrity). Pre-pump uses a
    # lower liquidity floor (accumulation tokens have thinner books; rug already
    # vetted by the FSM rug_risk score).
    ok, reasons = forensic_check(
        spread_pct=candidate.spread_pct,
        liquidity_usd=candidate.liquidity_usd,
        top_book_share=candidate.top_book_share,
        min_liquidity_usd=PREPUMP_MIN_LIQUIDITY_USD if fsm_path else None,
    )
    if not ok:
        _record_learning(candidate.symbol, "forensic_block", "paper", candidate, "; ".join(reasons))
        await store.insert_bot_log(
            "PUMP_SCANNER", "INFO",
            f"ForensicFilter bloqueó {candidate.symbol}: {'; '.join(reasons)}",
        )
        logger.info("forensic block %s: %s", candidate.symbol, reasons)
        return False

    # Position size by FIXED RISK: risk a fixed % of balance per trade, sized so
    # that hitting the dynamic stop loses exactly that. size = risk$ / (stop% ).
    # Floor $10, never exceed balance. Falls back to auto_entry_usd if misconfigured.
    risk_pct = float(os.getenv("PUMP_RISK_PER_TRADE_PCT", "1.0"))
    stop_pct = float(os.getenv("PUMP_DYNAMIC_STOP_PCT", "5.0"))
    balance = bot.balance()
    size = (balance * risk_pct / 100) / (stop_pct / 100) if (stop_pct > 0 and balance > 0) else bot.auto_entry_usd
    size = round(max(10.0, min(size, balance or bot.auto_entry_usd)), 2)

    sw.mark("validation")  # gates + ForensicFilter + risk sizing done
    result = await bot.engine.act(
        symbol=candidate.symbol, side=Side.buy, reference_price=candidate.last_price,
        capital_usd=size, exchanges=[candidate.exchange],
        open_trades=bot.open_count(),
    )
    sw.mark("order")  # order submission (paper fill) done
    # Entry latency: Detection (signal age) -> Validation (gates) -> Order submission.
    try:
        detection_ms = max(0.0, (datetime.now(UTC) - candidate.updated_at).total_seconds() * 1000)
    except Exception:
        detection_ms = None
    telemetry.latency.record({
        "detection_ms": round(detection_ms, 1) if detection_ms is not None else None,
        "validation_ms": sw.stages.get("validation"),
        "order_ms": sw.stages.get("order"),
        "total_ms": sw.total_ms(),
    })
    # Analytics (Phase D): setup type + entry-time confidence + sizing SIMULATION.
    # Confidence is derived from this setup's historical edge; the multiplier is
    # computed and stored but NEVER applied to the live size (simulation mode).
    setup_type = "accumulation" if fsm_path else ("velocity" if accel else "momentum")
    _eng = get_analytics()
    confidence = _eng.confidence_for(setup_type, candidate.exchange)
    size_mult = _eng.sizing_multiplier(confidence)

    opened_any = False
    for fill in result.fills:
        bot.pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
            cluster=candidate.cluster, signal_at=candidate.updated_at,
        )
        opened = bot.pm.positions.get(bot.pm.key(fill.exchange, fill.symbol))
        if opened:
            opened_any = True
            get_bus().emit(EventType.POSITION_OPENED, symbol=opened.symbol,
                           exchange=opened.exchange, entry_price=opened.entry_price,
                           cluster=opened.cluster, user_id=bot.uid)
            entry_slip = ((fill.fill_price - candidate.last_price) / candidate.last_price * 100
                          if candidate.last_price else 0.0)
            _eng.note_open(bot.pm.key(fill.exchange, fill.symbol), {
                "trade_id": f"{bot.uid}:{fill.exchange}:{fill.symbol}:{int(candidate.updated_at.timestamp())}",
                "setup_type": setup_type, "position_size": size,
                "entry_price": fill.fill_price,
                "confidence_score": confidence, "risk_used": risk_pct,
                "market_regime": _eng.regime, "entry_slippage_pct": entry_slip,
                "sizing_mode": "simulation", "sizing_multiplier": size_mult,
                "theoretical_size": round(size * size_mult, 2), "user_id": bot.uid,
            })
            await _persist_position(bot, opened)
            # Forensics (Fase 7/8): captura el contexto de ENTRADA del trade.
            if _forensics is not None:
                try:
                    await asyncio.to_thread(_forensics.record_entry, bot.uid, candidate, opened, accel)
                except Exception:
                    logger.exception("forensics record_entry failed")
        _record_learning(candidate.symbol, "auto_entry", "paper", candidate, f"bought ${size:.0f} @ {fill.fill_price}")
        await store.insert_bot_log(
            "PUMP_SCANNER", "TRADE_BUY",
            f"Auto-entry {candidate.symbol} ${size:.0f} @ {fill.fill_price}",
            volumen=candidate.volume_spike,
        )
        await notify.send_entry(notify.format_entry(
            symbol=candidate.symbol, exchange=candidate.exchange, price=fill.fill_price,
            accel=accel if accel is not None else candidate.volume_spike,
            score=candidate.pump_score, classification=candidate.classification,
            flags=candidate.flags, dump_pct=DUMP_TICK_PCT,
            timeout_min=TIMEOUT_MINUTES, be_pct=BREAKEVEN_PCT,
        ))
    for rej in result.rejected:
        logger.info("auto-entry rejected %s: %s", candidate.symbol, rej)
    return opened_any


# Cross-exchange arbitrage detection. OFF by default — it is alert-only (can't
# execute in paper) and a wide "spread" between two CEXes is almost always a
# stale/thin price on one venue, not real arbitrage. So it was just noise on
# Telegram. Re-enable with PUMP_ARB_ALERTS=true if you want it.
ARB_ALERTS = os.getenv("PUMP_ARB_ALERTS", "false").lower() == "true"
ARB_SPREAD_PCT = float(os.getenv("PUMP_ARB_SPREAD_PCT", "1.5"))
# Ignore absurd gaps — a >5% CEX-to-CEX gap is a data artifact (illiquid/stale
# book), not a tradeable spread.
ARB_MAX_SPREAD_PCT = float(os.getenv("PUMP_ARB_MAX_SPREAD_PCT", "5"))


async def _arbitrage_scan() -> None:
    """Same symbol on 2+ scanned exchanges with a price gap in
    [ARB_SPREAD_PCT, ARB_MAX_SPREAD_PCT] → alert. Real prices from the scan; no
    execution in paper. No-op unless PUMP_ARB_ALERTS=true."""
    if not ARB_ALERTS:
        return
    by_symbol: dict[str, list[TokenCandidate]] = {}
    for c in _candidates.values():
        if c.last_price > 0:
            by_symbol.setdefault(c.symbol, []).append(c)
    for sym, lst in by_symbol.items():
        if len(lst) < 2:
            continue
        lo = min(lst, key=lambda c: c.last_price)
        hi = max(lst, key=lambda c: c.last_price)
        if lo.last_price <= 0 or lo.exchange == hi.exchange:
            continue
        spread = (hi.last_price - lo.last_price) / lo.last_price * 100
        if ARB_SPREAD_PCT <= spread <= ARB_MAX_SPREAD_PCT:
            await notify.send_arbitrage(sym, lo.exchange, lo.last_price, hi.exchange, hi.last_price, spread)
            await store.insert_bot_log(
                "PUMP_SCANNER", "INFO",
                f"Arbitraje {sym}: {lo.exchange}@{lo.last_price:g} → {hi.exchange}@{hi.last_price:g} ({spread:.2f}%)",
            )


async def _perform_scan(min_pump_score: int = 1) -> ScanResponse:
    global _last_scan_at
    scanned = await scan_markets(_scan_exchanges(), min_pump_score=min_pump_score)
    _candidates.clear()
    for item in scanned:
        candidate = _to_candidate(item)
        _candidates[f"{candidate.exchange}:{candidate.symbol}"] = candidate
        if candidate.status == CandidateStatus.waiting_confirmation:
            # QUALITY GATE: solo notifica Telegram si cruza los pisos (score/conf/
            # vol/liq), es ALTA/MEDIA y no está en cooldown. El dashboard + learning
            # SIGUEN registrando todas (eso es data, no ruido).
            key = f"{candidate.exchange}:{candidate.symbol}"
            send, imp, reason = notify.alert_gate.evaluate(
                key, score=candidate.pump_score, confidence=candidate.confidence_score,
                vol_spike=candidate.volume_spike, liquidity=candidate.liquidity_usd)
            if send:
                await notify.send_alert(format_alert(
                    candidate.symbol, candidate.pump_score, candidate.classification,
                    candidate.flags, cluster=candidate.cluster, exchange=candidate.exchange,
                    liquidity_usd=candidate.liquidity_usd, importance=imp,
                ))
            else:
                logger.info("alerta Telegram suprimida %s: %s [%s]", candidate.symbol, reason, imp)
            await store.insert_alert({
                "symbol": candidate.symbol, "exchange": candidate.exchange,
                "pump_score": candidate.pump_score, "classification": candidate.classification,
                "flags": candidate.flags,
            })
            await store.insert_pump_candidate({
                "symbol": candidate.symbol, "exchange": candidate.exchange.upper(),
                "current_spread": candidate.spread_pct, "volume_acceleration": candidate.volume_spike,
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
            if current_mode() == ExecMode.paper and MOMENTUM_AUTOENTRY:
                # LATE path (off by default): only runs if momentum chasing is
                # explicitly re-enabled. The pre-pump FSM is the default authority.
                for bot in all_bots():
                    if bot.auto_entry and not bot.pm.has(candidate.exchange, candidate.symbol):
                        await _auto_enter(bot, candidate)
    # Cross-exchange arbitrage detection (alert-only in paper).
    try:
        await _arbitrage_scan()
    except Exception:
        logger.exception("arbitrage scan failed")
    _last_scan_at = datetime.now(UTC)
    # FASE 1 (microstructure): alimenta la watchlist de observación con los
    # símbolos del scan. NO cambia nada del trading — solo marca qué grabar.
    if _micro is not None:
        try:
            _micro.note_candidates([(c.exchange, c.symbol) for c in _candidates.values()])
        except Exception:
            logger.exception("micro note_candidates failed")
    # FASE 2 (pipeline FSM): admite cada candidato del scan a la máquina de
    # estados. La FSM decide cuándo (y si) promociona a entrada — no entra aquí.
    if _pipeline is not None:
        for c in _candidates.values():
            try:
                _pipeline.note_candidate(c.symbol, c.exchange)
            except Exception:
                logger.exception("pipeline note_candidate failed")
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
    # Mark equity per user (live total when that bot has keys, else paper).
    for bot in all_bots():
        point = {"t": _last_scan_at.isoformat(), "v": bot.balance()}
        bot.equity_history.append(point)
        del bot.equity_history[:-200]
        await store.insert_equity({**point, "user_id": bot.uid})
    ranked = sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)
    return ScanResponse(scanned_at=_last_scan_at, count=len(ranked), candidates=ranked)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/status")
async def status(request: Request) -> dict:
    bot = _req_bot(request)
    return {
        "service": "pump-reader",
        "exec_mode": os.getenv("PUMP_EXEC_MODE", "paper"),
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "exchanges": _scan_exchanges(),
        "last_scan_at": _last_scan_at.isoformat() if _last_scan_at else None,
        "candidate_count": len(_candidates),
        "kill_switch_active": bot.guard.kill_switch,
        "open_positions": bot.open_count(),
        "persistence": "supabase" if store.enabled() else "memory",
        "account_connected": bot.real_account.get("connected", []),
    }


@app.get("/account")
async def account(request: Request) -> dict:
    """Real read-only balance (only when the owner's keys are set), else this
    user's paper balance."""
    bot = _req_bot(request)
    acct = await real_balances() if bot.uid == OWNER_UID else {"has_keys": False}
    if acct.get("has_keys"):
        bot.real_account = acct
        for snap in acct.get("snapshots", []):
            await store.insert_account_snapshot({**snap, "user_id": bot.uid})
        return {**acct, "source": "live_account"}
    return {
        "has_keys": False, "source": "paper", "total_usdt": bot.paper_equity(),
        "allocated_usdt": float(bot.allocation.get("bot_total_usdt") or PAPER_BALANCE),
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


def _req_bot(request: Request) -> UserBot:
    """The UserBot for the logged-in account (set by _auth_gate). Per-user P&L,
    balance and equity helpers live on the bot (see user_bot.py)."""
    uid = (getattr(request.state, "user", None) or {}).get("id") or OWNER_UID
    return get_bot(uid)


@app.get("/overview")
async def overview(request: Request) -> dict:
    bot = _req_bot(request)
    # Candidate boards exclude already-pumped blow-offs (post-pump ≠ candidate).
    shown = [c for c in _candidates.values() if _is_candidate_display(c)]
    ranked = sorted(shown, key=lambda c: c.pump_score, reverse=True)
    top = ranked[0] if ranked else None
    # PRE-PUMP pipeline (the real "antes del estallido" candidates): tokens the FSM
    # is analysing for accumulation + the ones it confirmed/entered. This is what
    # the main "Candidatos pre-estallido" board shows (momentum gainers live in the
    # separate Mercado/Tokens view).
    prepump: list[dict] = []
    if _pipeline is not None:
        try:
            prepump = _pipeline.board(limit=20)
        except Exception:
            prepump = []
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
        "open_positions": bot.open_count(),
        "balance": bot.balance(),
        "balance_source": "live_account" if bot.real_account.get("has_keys") else "paper",
        "account_connected": bot.real_account.get("connected", []),
        "persistence": "supabase" if store.enabled() else "memory",
        "pnl_7d": bot.pnl_7d(),
        "equity_curve": bot.equity_history,
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
        "prepump": prepump,
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
async def get_allocation(request: Request) -> dict:
    bot = _req_bot(request)
    total_pct = round(sum(bot.allocation["splits"].values()), 2)
    return {**bot.allocation, "sum_pct": total_pct, "valid": abs(total_pct - 100.0) < 0.01}


@app.post("/allocation")
async def set_allocation(req: AllocationRequest, request: Request) -> dict:
    bot = _req_bot(request)
    total_pct = round(sum(req.splits.values()), 2)
    if abs(total_pct - 100.0) >= 0.01:
        raise HTTPException(status_code=400, detail=f"splits must sum to 100% (got {total_pct}%)")
    bot.allocation["bot_total_usdt"] = req.bot_total_usdt
    bot.allocation["splits"] = {k.lower(): float(v) for k, v in req.splits.items()}
    await store.upsert_allocation({
        "bot_total_usdt": bot.allocation["bot_total_usdt"], "splits": bot.allocation["splits"],
    }, user_id=bot.uid)
    return {**bot.allocation, "sum_pct": total_pct, "valid": True}


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
async def act_on_candidate(request: Request, symbol: str, capital_usd: float = 100.0, exchange: str | None = None) -> ActResponse:
    bot = _req_bot(request)
    symbol_u = symbol.upper()
    matches = [c for c in _candidates.values() if c.symbol == symbol_u]
    if exchange:
        matches = [c for c in matches if c.exchange == exchange.lower()]
    if not matches:
        raise HTTPException(status_code=404, detail="candidate not found; run /scan first")
    candidate = max(matches, key=lambda c: c.pump_score)

    result = await bot.engine.act(
        symbol=candidate.symbol,
        side=Side.buy,
        reference_price=candidate.last_price,
        capital_usd=capital_usd,
        exchanges=[candidate.exchange],
        open_trades=bot.open_count(),
    )

    for fill in result.fills:
        bot.pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
            cluster=candidate.cluster, signal_at=candidate.updated_at,
        )
        opened = bot.pm.positions.get(bot.pm.key(fill.exchange, fill.symbol))
        if opened:
            await _persist_position(bot, opened)

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
async def list_positions(request: Request) -> list[dict]:
    bot = _req_bot(request)
    return [fill.__dict__ | {"side": fill.side.value, "mode": fill.mode.value} for fill in bot.engine.positions]


@app.get("/managed")
async def list_managed(request: Request) -> dict:
    bot = _req_bot(request)
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
        for p in bot.pm.positions.values()
        if not p.closed
    ]
    return {
        "open": open_positions,
        "exits": [e.__dict__ for e in reversed(bot.pm.history[-20:])],
        "adaptive_threshold": round(_adaptive_threshold, 1),
        "auto_entry": bot.auto_entry,
    }


@app.get("/autotune")
async def autotune_status() -> dict:
    """Qué afina el bot SOLO vs qué es fijo. Lee os.environ en vivo (el optimizador
    24h muta el entorno) + el umbral adaptativo. Alimenta la tabla 'Auto-ajuste'."""
    def envf(k: str, d: str) -> float:
        try:
            return round(float(os.getenv(k, d)), 2)
        except Exception:
            return float(d)
    return {
        "rows": [
            {"param": "Umbral de entrada", "value": f"{round(_adaptive_threshold,1)} pts",
             "auto": True, "how": "cada trade cerrado + optimizador 24h · banda [40,90] · persiste"},
            {"param": "Trailing stop (giveback del pico)", "value": f"{envf('PUMP_DYNAMIC_STOP_PCT','5.0')}%",
             "auto": True, "how": "24h desde forensics (~1.5× pérdida típica) · banda [3,10]"},
            {"param": "Hard stop (pérdida máx)", "value": f"{envf('PUMP_STOP_LOSS_PCT','8')}%",
             "auto": True, "how": "24h desde forensics"},
            {"param": "Timeout (corte por tiempo)", "value": f"{envf('PUMP_TIMEOUT_MINUTES','8')} min",
             "auto": True, "how": "24h desde lead-time del aprendizaje"},
            {"param": "Break-even (protección)", "value": f"{envf('PUMP_BREAKEVEN_PCT','4')}%",
             "auto": False, "how": "fijo (pendiente de auto-ajuste)"},
            {"param": "Dump detector (caída de 1 tick)", "value": f"{envf('PUMP_DUMP_TICK_PCT','10')}%",
             "auto": False, "how": "fijo"},
            {"param": "Tamaño por operación", "value": f"${envf('PUMP_AUTO_ENTRY_USD','50')}",
             "auto": False, "how": "fijo (config de riesgo)"},
            {"param": "Máx operaciones simultáneas", "value": f"{int(envf('PUMP_MAX_OPEN_TRADES','4'))}",
             "auto": False, "how": "fijo (config de riesgo)"},
        ],
        "threshold": round(_adaptive_threshold, 1),
        "note": "Los parámetros de salida (trailing/hard) se multiplican además por cluster: "
                "long_pump corre tight/fast, classic loose/patient.",
    }


@app.get("/velocity")
async def velocity_status() -> dict:
    return _velocity.status()


@app.get("/micro/status")
async def micro_status() -> dict:
    """FASE 1: estado del recolector de microestructura (filas grabadas, símbolos
    en observación, tamaño de la DB local). Solo lectura, no afecta el trading."""
    if _micro is None:
        return {"enabled": False, "note": "recorder not started"}
    s = _micro.status()
    return {"enabled": True, **s,
            "first_ts": micro_iso(s.get("first_ts_ms")),
            "last_ts": micro_iso(s.get("last_ts_ms"))}


@app.get("/forensics/stats")
async def forensics_stats() -> dict:
    """Fases 7-9: resumen de la autopsia de trades + ranking por exchange +
    comparación ganadores vs hard-stops. Solo lectura."""
    if _forensics is None:
        return {"enabled": False}
    return {"enabled": True, "summary": _forensics.stats(),
            "by_exchange": _forensics.exchange_stats(),
            "winners_vs_hardstops": _forensics.compare_winners_vs_hardstops()}


@app.get("/pipeline/status")
async def pipeline_status() -> dict:
    """FASE 2: estado de la máquina de estados (modo, umbrales, conteo por
    estado). Solo lectura."""
    if _pipeline is None:
        return {"enabled": False}
    return {"enabled": True, **_pipeline.status()}


@app.get("/pipeline/board")
async def pipeline_board() -> dict:
    """FASE 2: tablero de símbolos en la FSM con sus scores (acc/pers/rug)."""
    if _pipeline is None:
        return {"enabled": False, "rows": []}
    return {"enabled": True, "rows": _pipeline.board()}


@app.get("/pipeline/decisions")
async def pipeline_decisions() -> dict:
    """FASE 2 (Decision Log): últimas decisiones/transiciones de la FSM."""
    if _pipeline is None:
        return {"enabled": False, "rows": []}
    return {"enabled": True, "rows": _pipeline.recent_decisions()}


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


def _settings_payload(bot: UserBot, role: str = "operator") -> dict:
    return {
        # Shared brain (read-only for operators; only admin can tune it).
        "confirmation_threshold": round(_adaptive_threshold, 1),
        "threshold_editable": role == "admin",
        # Per-user trading preferences.
        "auto_entry": bot.auto_entry,
        "auto_entry_usd": bot.auto_entry_usd,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "velocity_accel_factor": _velocity.status().get("accel_factor"),
        "exec_mode": current_mode().value,
        "exchanges": _scan_exchanges(),
    }


@app.get("/settings")
async def get_settings(request: Request) -> dict:
    user = getattr(request.state, "user", None) or {}
    return _settings_payload(_req_bot(request), user.get("role", "operator"))


@app.post("/settings")
async def update_settings(req: SettingsRequest, request: Request) -> dict:
    """Live bot config. auto_entry / auto_entry_usd are PER-USER (each account
    controls its own bot). The confirmation threshold is the shared brain, so
    only an admin may change it."""
    global _adaptive_threshold
    user = getattr(request.state, "user", None) or {}
    bot = _req_bot(request)
    if req.confirmation_threshold is not None and user.get("role") == "admin":
        # Clamp manual overrides to the same safety band; below the floor the bot
        # over-trades weak signals (the $50 paper-loss cause).
        _adaptive_threshold = max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, float(req.confirmation_threshold)))
        _persist_threshold()
        # Re-evaluate candidate statuses so the Alerts view reflects it now.
        for c in _candidates.values():
            c.status = _status_for(c.pump_score)
    if req.auto_entry is not None:
        bot.auto_entry = bool(req.auto_entry)
    if req.auto_entry_usd is not None:
        bot.auto_entry_usd = float(req.auto_entry_usd)
    return _settings_payload(bot, user.get("role", "operator"))


@app.get("/pnl/breakdown")
async def pnl_breakdown(request: Request) -> dict:
    """Per-token P&L over the last 7d: realized exits + open unrealized, so the
    PNL 7D widget can show which tokens are winning/losing. All from real managed
    positions — nothing invented."""
    bot = _req_bot(request)
    cutoff = datetime.now(UTC).timestamp() - 7 * 86400
    by: dict[str, dict] = {}

    def _row(exchange: str, symbol: str) -> dict:
        k = f"{exchange}:{symbol}"
        return by.setdefault(k, {
            "symbol": symbol, "exchange": exchange,
            "realized": 0.0, "unrealized": 0.0, "trades": 0, "open": False,
        })

    for e in bot.pm.history:
        try:
            ts = datetime.fromisoformat(e.at).timestamp()
        except Exception:
            ts = cutoff
        if ts < cutoff:
            continue
        d = _row(e.exchange, e.symbol)
        d["realized"] += e.pnl
        d["trades"] += 1
    for p in list(bot.pm.positions.values()):
        if p.closed or p.last_price <= 0:
            continue
        d = _row(p.exchange, p.symbol)
        d["unrealized"] += (p.last_price - p.entry_price) * p.qty
        d["open"] = True

    rows = []
    for d in by.values():
        d["total"] = round(d["realized"] + d["unrealized"], 2)
        d["realized"] = round(d["realized"], 2)
        d["unrealized"] = round(d["unrealized"], 2)
        rows.append(d)
    rows.sort(key=lambda r: r["total"], reverse=True)
    return {
        "rows": rows,
        "winners": sum(1 for r in rows if r["total"] > 0),
        "losers": sum(1 for r in rows if r["total"] < 0),
        "total": round(sum(r["total"] for r in rows), 2),
        "pnl_7d": bot.pnl_7d(),
    }


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


@app.post("/debug/seed-learning")
async def debug_seed_learning() -> dict:
    """TEST FIXTURE — inject a complete synthetic dataset into the Learning Lab +
    Analytics so both sections can be exercised end-to-end without waiting weeks
    of live trading. DISABLED by default: only runs when PUMP_ALLOW_SEED=1 (so it
    can never fire in production). Pure in-memory; persistence is best-effort."""
    if os.getenv("PUMP_ALLOW_SEED") != "1":
        raise HTTPException(status_code=403, detail="seeding disabled (set PUMP_ALLOW_SEED=1)")
    import random
    from .analytics import TradeRecord, quality_score
    from .learning import PUMP_MOVE_PCT, Outcome
    random.seed(42)
    now = datetime.now(UTC)
    eng = get_analytics()

    # 1) Learning Lab — settled outcomes (in the 30d window, past 7d horizon) so
    #    precision/recall/lead-time/components/proposals all populate. 24/cluster
    #    clears the 20-sample component-analysis floor.
    out_n = 0
    for cluster in ("long_pump", "classic"):
        for i in range(24):
            alert_at = now - timedelta(days=8 + (i % 20), hours=random.randint(0, 12))
            price = round(random.uniform(0.01, 5.0), 6)
            confirmed = (i % 3 != 0)
            mfe = random.uniform(25, 120) if confirmed else random.uniform(2, 15)
            peak = price * (1 + mfe / 100)
            peak_at = alert_at + timedelta(minutes=random.uniform(10, 180))
            _lab.outcomes.append(Outcome(
                symbol=f"SEED{cluster[:1].upper()}{i}/USDT", exchange="binance",
                source="alert", alert_at=alert_at, alert_price=price,
                pump_score=random.randint(70, 95), cluster=cluster,
                classification="accumulation",
                signals={"volume_spike": round(random.uniform(2, 8), 2),
                         "price_change_pct_24h": round(random.uniform(5, 40), 2),
                         "orderbook_imbalance": round(random.uniform(0.5, 0.9), 2),
                         "liquidity_usd": round(random.uniform(5e4, 5e5))},
                peak_price=peak, peak_at=peak_at, peak_24h=peak,
                low_price=price * (1 - random.uniform(2, 12) / 100), last_price=peak,
                settled=True,
                label="confirmed_pump" if mfe >= PUMP_MOVE_PCT else "no_pump",
            ))
            out_n += 1
    _lab.record_missed("MISSEDONE/USDT", "binance")
    _lab.record_missed("MISSEDTWO/USDT", "mexc")

    # 2) Analytics — complete trades across setups/regimes (21/setup clears the
    #    20-sample ranking floor → real A+..F grades).
    setups = ("accumulation", "velocity", "momentum")
    regimes = ("bull/low_vol", "sideways/high_vol", "bear/high_vol")
    reasons = ("trailing", "break_even", "timeout", "hard_stop", "dump")
    tr_n = 0
    for i in range(63):
        setup = setups[i % 3]
        win = random.random() < (0.6 if setup == "accumulation" else 0.45)
        pnl_pct = random.uniform(3, 40) if win else -random.uniform(2, 9)
        size = round(random.uniform(50, 200), 2)
        pnl_usd = round(size * pnl_pct / 100, 2)
        entry_at = now - timedelta(hours=i * 4 + 1)
        hold = random.uniform(120, 3600)
        exit_at = entry_at + timedelta(seconds=hold)
        conf = random.choice([55, 65, 75, 85, 92])
        mult = eng.sizing_multiplier(conf)
        rec = TradeRecord(
            trade_id=f"SEED-TR-{i}", symbol=f"SEED{i}/USDT", exchange="binance",
            setup_type=setup,
            signal_timestamp=(entry_at - timedelta(seconds=random.uniform(30, 600))).isoformat(),
            entry_timestamp=entry_at.isoformat(), exit_timestamp=exit_at.isoformat(),
            entry_price=1.0, exit_price=round(1 + pnl_pct / 100, 4),
            position_size=size, pnl_pct=round(pnl_pct, 3), pnl_usd=pnl_usd,
            mfe_pct=round(max(pnl_pct, random.uniform(max(pnl_pct, 1), pnl_pct + 20)), 3),
            mae_pct=round(-random.uniform(0.5, 7), 3),
            holding_seconds=round(hold, 1),
            lead_time_seconds=round(random.uniform(30, 600), 1),
            entry_slippage_pct=round(random.uniform(0, 0.3), 4), exit_slippage_pct=0.0,
            exit_reason=random.choice(reasons), confidence_score=conf, risk_used=1.0,
            market_regime=regimes[i % 3], sizing_mode="simulation",
            sizing_multiplier=mult, theoretical_size=round(size * mult, 2),
        )
        rec.theoretical_pnl_usd = round(pnl_usd * mult, 2)
        rec.trade_quality_score = quality_score(rec, "perfect_entry")
        eng.ingest(rec)
        tr_n += 1

    return {"seeded_outcomes": out_n, "seeded_missed": 2, "seeded_trades": tr_n,
            "learning": _lab.metrics(), "analytics_dashboard": eng.dashboard()}


@app.post("/risk/kill-switch")
async def set_kill_switch(request: Request, active: bool, reason: str = "manual") -> dict:
    bot = _req_bot(request)
    bot.guard.set_kill_switch(active, reason)
    return {"kill_switch_active": bot.guard.kill_switch, "reason": bot.guard.kill_reason}


@app.post("/reset")
async def reset_my_bot(request: Request) -> dict:
    """Reset the logged-in user's OWN bot: close every open position (freeing the
    capital) and clear the in-memory equity curve. Keeps the shared learning and
    this user's history. Does not touch any other account."""
    bot = _req_bot(request)
    closed = 0
    for pos in list(bot.pm.positions.values()):
        if not pos.closed:
            pos.closed = True
            pos.qty = 0.0
            await _persist_position(bot, pos)  # marks closed=true in Supabase
            closed += 1
    bot.pm.positions.clear()
    bot.equity_history.clear()
    bot.guard.set_kill_switch(False, "reset")
    return {"ok": True, "closed": closed}

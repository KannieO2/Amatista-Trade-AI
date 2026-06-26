"""Pump Reader API + built-in dashboard.

Scans Binance/MEXC/Bitget for scam-pump patterns, scores them with auditable
rules, and (in paper mode by default) can execute. Auto-scans on a timer so it
runs as a bot, not just a manual API. Every order passes the Risk Engine +
kill switch (see docs/security-invariants.md).
"""

from __future__ import annotations

import asyncio
import json
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

from . import analytics, db_migrate, decision_log, events, grid_sync, marketdata, store, telemetry
from .analytics import get_engine as get_analytics
from .events import EXIT_REASON_EVENT, EventType, get_bus
from .account import real_balances
from .dashboard import DASHBOARD_HTML
from .executor import ExecMode, ExecutionEngine, Side, current_mode
from .grid import GridBot, backtest, fetch_ohlcv_for, fetch_price
from .grvt_proxy import register_grvt_proxy, set_grid_token_provider
from .market import market_for_symbol
from . import notify
from .notify import format_alert, send_telegram
from .position_manager import (
    TRAIL_ARM_PCT, TRAIL_GIVEBACK_PCT,
    ExitEvent, ManagedPosition, PositionManager,
)
from .risk import RiskGuard
from .scanner import (
    FORENSIC_ONCHAIN_OVERRIDE_HEAT, LEARNED_WEIGHTS, SUPPORTED_EXCHANGES,
    ScannedCandidate, _cluster, fetch_token_detail, forensic_check, scan_markets,
    set_learned_weights,
)
from .velocity import VelocityWatcher, watch_list_from_scores
from .learning import LearningLab
from .websocket_manager import USE_WEBSOCKETS, get_manager
from .user_bot import PAPER_BALANCE, UserBot, all_bots, default_allocation, ensure_bots, get_bot
from .microstructure import MicroObserver, iso as micro_iso
from .forensics import ForensicsStore
from types import SimpleNamespace

from .pipeline import CONFIRM_TICKS as FSM_CONFIRM_TICKS
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

# UPDATE cadence: chequea AHORA si hay oportunidad de entrar sobre los candidatos que
# ya se monitorean (light scan). Default 3 min.
SCAN_INTERVAL_SECONDS = int(os.getenv("PUMP_SCAN_INTERVAL_SECONDS", "180"))
# DISCOVER cadence: barrido COMPLETO de TODOS los tokens del exchange (full=True) que
# arma/depura el universo de candidatos. Caro → 1×/día.
DISCOVER_INTERVAL_SECONDS = int(os.getenv("PUMP_DISCOVER_INTERVAL_SECONDS", "86400"))
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
# Cache rodante de data de MERCADO por token (score/cluster/Δ24h/spark), key "exch:SYM"
# en minúsculas:MAYÚS. _candidates se limpia cada scan, pero el FSM monitorea tokens más
# tiempo → este cache retiene su data para la vista unificada de candidatos. Se repuebla
# en cada scan (light + daily full). Cap simple para no crecer sin límite.
_candidate_market: dict[str, dict] = {}
# Re-evaluation accounting: every scan REBUILDS _candidates from live data, so a
# token that no longer qualifies is simply not re-added (= discarded). This holds
# the kept/discarded/new counts of the last pass for the dashboard, making the
# (already-existing) re-analysis VISIBLE.
_last_reeval: dict = {}

# Per-user trading state — each account is its OWN bot (balance, positions, risk,
# equity, P&L) and lives in the user_bot registry (get_bot / all_bots). The owner
# is the default tenant ("owner"). Everything below stays GLOBAL — the shared
# brain that every account's bot consumes.
OWNER_UID = "owner"
_learning: list[LearningRecord] = []
_last_scan_at: datetime | None = None        # último UPDATE (light scan)
_last_discover_at: datetime | None = None     # último DISCOVER (barrido full diario)

# Differentiated learning schema (Lead-Architect §4). Never delete candidates —
# every decision is kept; here we BUCKET them into three honest categories read off
# the action+detail already stored (no new DB columns → Supabase-DDL safe):
#   successful  = a trade was actually taken (passed every filter, executed)
#   dangerous   = a scam/rug tell blocked it (thin+concentrated book = MANIPULATION)
#   failed      = a benign couldn't-enter (insufficient liquidity, no breakout, weak)
_ENTRY_ACTIONS = ("auto_entry", "execute", "onchain_lead_entry")
# Dangerous tokens stay avoided across restarts (a rug that briefly thickens its
# book to bait entries is still a rug). Persisted in bot_state (SQLite, REST-safe),
# but redeemable by strong on-chain buy pressure — same override as forensic.
_dangerous_signals: set[str] = set()


def _learning_bucket(action: str, detail: str) -> str:
    """Map a learning record to its differentiated category."""
    if action in _ENTRY_ACTIONS:
        return "successful"
    if action == "manipulation_suspect" or "MANIPULATION_SUSPECT" in (detail or ""):
        return "dangerous"
    return "failed"


def _mark_dangerous(exchange: str, symbol: str) -> None:
    key = f"{exchange}:{symbol}"
    if key in _dangerous_signals:
        return
    _dangerous_signals.add(key)
    if store.enabled():
        import json as _json
        payload = _json.dumps(sorted(_dangerous_signals)[-500:])
        try:
            asyncio.create_task(store.set_state("dangerous_signals", payload))
        except RuntimeError:
            pass  # no running loop (e.g. called outside async ctx) — in-memory still holds


async def _load_dangerous_signals() -> None:
    raw = await store.get_state("dangerous_signals")
    if not raw:
        return
    import json as _json
    try:
        for k in _json.loads(raw):
            _dangerous_signals.add(str(k))
    except Exception:
        logger.exception("load dangerous_signals failed")

# Auto-entry (paper only): the bot buys candidates that cross the confirmation
# threshold so the exit engine has something to manage. Never auto-enters live.
AUTO_ENTRY = os.getenv("PUMP_AUTO_ENTRY", "true").lower() == "true"
AUTO_ENTRY_USD = float(os.getenv("PUMP_AUTO_ENTRY_USD", "100"))
# Entry authority — CRIMINAL-PUMP ONLY (directiva del usuario: "no momentum, centra el
# bot en los criminal pump"). El pump se caza ANTES de correr → la ÚNICA vía de auto-
# entrada es el FSM pre-pump (acumulación/persistencia/rug sobre una ventana) + su
# acelerador de ruptura (velocity_ruptura, también FSM-confirmado). El chase de momentum/
# velocity/gainers (un movimiento YA en curso = TARDE por construcción, grade F medido)
# queda ELIMINADO: pineado en False, NO env-flippable, para que el bot NUNCA compre un
# breakout perseguido. El scanner sigue manteniendo el radar de candidatos y alimentando
# el FSM; las alertas + entradas salen EXCLUSIVAMENTE del FSM de acumulación.
MOMENTUM_AUTOENTRY = False   # ELIMINADO (criminal-pump only) — antes PUMP_MOMENTUM_AUTOENTRY
VELOCITY_AUTOENTRY = False   # ELIMINADO (criminal-pump only) — antes PUMP_VELOCITY_AUTOENTRY
# --- GAINERS — ENTRADA ELIMINADA (criminal-pump only). Se conservan SOLO las constantes
# de tamaño/forensic abajo por si el radar/explore las referencia, pero NINGÚN gainer
# auto-entra. El "coil" anticipatorio que valía ya vive dentro del FSM de acumulación.
GAINERS_COIL_AUTOENTRY = False   # ELIMINADO (criminal-pump only) — antes PUMP_GAINERS_COIL_AUTOENTRY
GAINERS_MAX_OPEN = int(os.getenv("PUMP_GAINERS_MAX_OPEN", "3"))           # own concurrency budget
GAINERS_MIN_LIQUIDITY_USD = float(os.getenv("PUMP_GAINERS_MIN_LIQUIDITY_USD", "80000"))  # own forensic floor
GAINERS_MAX_CHASE_PCT = float(os.getenv("PUMP_GAINERS_MAX_CHASE_PCT", "40"))  # own anti-top (24h ceiling)
GAINERS_MIN_ACCEL = float(os.getenv("PUMP_GAINERS_MIN_ACCEL", "4"))       # own volume-accel trigger floor
# Anti-top de RAMPA (multi-hora): el gainer entra AL arranque, NO después. Si el
# token ya corrió más de esto sobre su base reciente, ya despegó → no perseguir
# (KAVA spike +18% → crash). Mide sobre el spark completo (_breakout_state).
GAINERS_MAX_RUNUP_PCT = float(os.getenv("PUMP_GAINERS_MAX_RUNUP_PCT", "15"))
# Entry momentum/exhaustion gate (anti-chase): skip a candidate already up this
# much on 24h (the pump ran — buying it = buying the top), and require a minimum
# volume spike behind scan-path entries.
ENTRY_MAX_CHASE_PCT = float(os.getenv("PUMP_ENTRY_MAX_CHASE_PCT", "60"))
ENTRY_MIN_VOL_SPIKE = float(os.getenv("PUMP_ENTRY_MIN_VOL_SPIKE", "2.5"))
# Anti-TOP guard (ALL entry paths): the 24h chase gate misses an INTRADAY spike —
# a token flat over 24h but already +N% over the last few candles is going vertical
# RIGHT NOW, so buying it = buying the top (this ate the EIGEN/SUI hard stops). A
# real accumulation entry sits on a flat base, so this never blocks a true pre-pump.
ENTRY_MAX_RUNUP_PCT = float(os.getenv("PUMP_ENTRY_MAX_RUNUP_PCT", "12"))
# Data-driven precision ceilings — MEASURED on the bot's own settled learning
# outcomes (see learning loop). Confirmed pumps are MICROCAPS with no fake bid wall:
#   liquidity: 100% of confirmed pumps had book liquidity < $150K; the $200K+ big-caps
#              are 16% of the no-pumps and never pumped → ceiling cuts them, 0 confirmed lost.
#   imbalance: an extreme bid wall (>0.85) is a fake-wall/no-pump tell — 0 of 14
#              confirmed exceeded it, 17% of no-pumps did → ceiling cuts them, 0 lost.
ENTRY_MAX_LIQUIDITY_USD = float(os.getenv("PUMP_ENTRY_MAX_LIQUIDITY_USD", "150000"))
ENTRY_MAX_IMBALANCE = float(os.getenv("PUMP_ENTRY_MAX_IMBALANCE", "0.85"))
# Anti-rug (on-chain): cuando HAY cobertura DEX y el flujo está DUMPEANDO (ventas
# dominan las compras con suficiente actividad), es un rug/dump en progreso → veta
# y marca peligroso. No compres la salida del dev. Solo aplica si hay datos DEX
# reales; un token CEX-only sin DEX no se bloquea por esto.
ANTIRUG_MIN_BUY_RATIO = float(os.getenv("PUMP_ANTIRUG_MIN_BUY_RATIO", "0.35"))
ANTIRUG_MIN_FLOW = int(os.getenv("PUMP_ANTIRUG_MIN_FLOW", "20"))
# Techo de market cap: microcaps pumpean, big-caps no. El libro flaco engaña al
# techo de liquidez (APE mid-cap con $5K de libro lo cruza), el mcap real no. 0 = off.
ENTRY_MAX_MARKET_CAP_USD = float(os.getenv("PUMP_ENTRY_MAX_MARKET_CAP_USD", "50000000"))
# Breakout floor — the OTHER side of the same guard. A flat consolidation breaks
# up or down ~50/50; buying it flat is why every recent trade had MFE=+0.0% (price
# never rose after entry). So require a CONFIRMED up-break at entry: price already
# off its base by >= this %, AND the last close is the window high (making a fresh
# high = breaking up now, not rolling over). Entry only in the band [MIN .. MAX).
ENTRY_MIN_BREAKOUT_PCT = float(os.getenv("PUMP_ENTRY_MIN_BREAKOUT_PCT", "1.0"))
# Volume confirmation for the break — applies to EVERY path. A price up-break on
# FADING volume (no buyers) is a fake/late break that rolls over at once: MASK
# entered at volume_spike 0.6x (40% BELOW average) and faded. volume_spike 1.0 =
# average, so require at least baseline buying behind the break. This is what stops
# "entró tarde" — a fresh high with dead volume is the tail of a move, not the start.
ENTRY_MIN_BREAKOUT_VOL = float(os.getenv("PUMP_ENTRY_MIN_BREAKOUT_VOL", "1.0"))
# Confidence floor: the scanner's confidence_score (~35 thin spike … ~95 deep
# book + clean live move) must clear this before any auto-entry. Filters the
# low-confidence thin-book signals that just bleed the spread.
ENTRY_MIN_CONFIDENCE = float(os.getenv("PUMP_ENTRY_MIN_CONFIDENCE", "50"))
# --- PRECISION gate for the PRE-PUMP/FSM path (raise the 22% win-rate) -----------
# The 3 levers combined (user's call), tuned for a CEX bot trading USDT pairs:
#   (a) CONFIRMED micro-breakout: never buy a 100%-flat base that just drifts — wait
#       for the FIRST real push (off the base by >= this %, making a fresh high).
#   (b) On-chain VETO, best-effort: when the token HAS real DEX coverage, require
#       buy-pressure heat >= MIN; ABSENCE of coverage never blocks (CEX-only USDT
#       tokens like SLX have no/weak DEX presence — that's normal, not a red flag).
# (FSM score thresholds = the 3rd lever, raised in .env: PUMP_FSM_ACC_MIN/PERS_MIN.)
FSM_MIN_BREAKOUT_PCT = float(os.getenv("PUMP_FSM_MIN_BREAKOUT_PCT", "1.5"))
FSM_REQUIRE_ONCHAIN_WHEN_AVAIL = os.getenv("PUMP_FSM_REQUIRE_ONCHAIN", "true").lower() == "true"
FSM_ONCHAIN_MIN_HEAT = int(os.getenv("PUMP_FSM_ONCHAIN_MIN_HEAT", "55"))
# 21h-TIMING fix (data-driven). Forensics on 212 real trades: pre_pump_accumulation
# = the WORST setup (9% win) because it buys a FLAT base that pumps ~21h LATER, long
# after the bot timed out. The ONLY profitable bucket was volume_spike > 6x (+0.73%).
# So the FSM no longer buys at the pre-pump signal — it WATCHES and enters only when
# real volume confirms the move has STARTED. Because the scanner re-flags the token
# every cycle while it accumulates, this fires whenever the actual move arrives
# (minutes OR hours later), capturing the lead instead of guessing the bottom.
FSM_REQUIRE_VOLUME = os.getenv("PUMP_FSM_REQUIRE_VOLUME", "true").lower() == "true"
FSM_MIN_ENTRY_VOL_SPIKE = float(os.getenv("PUMP_FSM_MIN_ENTRY_VOL_SPIKE", "4"))
# ENTRAR ANTES (tesis criminal-pump). El FSM confirmado (acc/pers/rug sobre ventana, con el
# accumulation_score exigiendo vol↑ con precio PLANO) ES la entrada — se compra DURANTE la
# acumulación. Los gates de breakout (flat/volumen/on-chain) se ELIMINARON: el log real probó
# que rechazaban el 98% de la tesis (1030/1054 — 657 skip_fsm_flat + 257 on-chain), esperando
# un breakout que llega tarde o nunca. Queda SOLO un piso anti-libro-muerto. On-chain = bono
# de sizing; el anti-rug (dump en curso) + forensic + score/price floor siguen protegiendo.
FSM_CONFIRM_MIN_VOL = float(os.getenv("PUMP_FSM_CONFIRM_MIN_VOL", "1.3"))  # piso anti-libro-muerto
# Piso de volumen MÍNIMO incluso en el path on-chain LEAD (que normalmente waivea el
# gate de 4x). Un lead con libro MUERTO (NIL @1.9x, FTT @0.9x = perdedores con hard_stop)
# no debe entrar solo por heat. El ganador LAYER tuvo 2.1x → el piso 2.0x los separa.
# Reduce pérdidas sin tocar el stop ni los ganadores.
FSM_ONCHAIN_MIN_VOL_FLOOR = float(os.getenv("PUMP_FSM_ONCHAIN_MIN_VOL_FLOOR", "2.0"))
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
    try:
        await _load_dangerous_signals()
    except Exception:
        logger.exception("startup dangerous_signals load failed")
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
    # P5: restore the learned scoring weights (so the learned edge survives a
    # restart). Falls back to neutral 1.0 if none persisted or parse fails.
    try:
        wsaved = await store.get_state("learned_weights")
        if wsaved:
            set_learned_weights(json.loads(wsaved))
            logger.info("learned scoring weights restored: %s", wsaved)
    except Exception:
        logger.exception("learned-weights restore failed")
    # Restore the auto-tuned alert probability floor (precision feedback survives restart).
    try:
        asaved = await store.get_state("alert_min_probability")
        if asaved:
            global ALERT_MIN_PROBABILITY
            ALERT_MIN_PROBABILITY = max(0.25, min(0.70, float(asaved)))
            logger.info("alert prob floor restored: %.2f", ALERT_MIN_PROBABILITY)
    except Exception:
        logger.exception("alert-prob restore failed")
    # Restore the manually-tuned ENGINE KNOBS (pre-pump breakout/volume gates +
    # ruptura accelerator) so your Settings calibration survives a restart instead
    # de volver a los defaults del .env cada boot (leak de calibración).
    try:
        global FSM_MIN_BREAKOUT_PCT, FSM_MIN_ENTRY_VOL_SPIKE
        bsaved = await store.get_state("fsm_min_breakout_pct")
        if bsaved is not None:
            FSM_MIN_BREAKOUT_PCT = float(bsaved)
        vsaved = await store.get_state("fsm_min_entry_vol_spike")
        if vsaved is not None:
            FSM_MIN_ENTRY_VOL_SPIKE = float(vsaved)
        ksaved = await store.get_state("velocity_accel_factor")
        if ksaved is not None:
            from . import velocity as _vel
            _vel.ACCEL_FACTOR = float(ksaved)
        if any(x is not None for x in (bsaved, vsaved, ksaved)):
            logger.info("engine knobs restored: breakout=%s vol=%s accel=%s", bsaved, vsaved, ksaved)
    except Exception:
        logger.exception("engine-knobs restore failed")
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
    # Un solo sistema: arranca el GRVTBot (grid) en 2º plano si no está ya corriendo.
    # No toca su código y NUNCA lo mata (sobrevive a reinicios — maneja dinero real).
    try:
        from . import grid_supervisor
        logger.info("grid supervisor: %s", grid_supervisor.ensure_grid_running())
    except Exception:
        logger.exception("grid supervisor init failed")
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
        asyncio.create_task(_onchain_loop()),
        asyncio.create_task(_coinbase_loop()),
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
    global _last_discover_at
    await asyncio.sleep(30)
    while True:
        try:
            await _perform_scan(full=True)   # whole-universe Discover (scans TODOS)
            _last_discover_at = datetime.now(UTC)
            ranked = sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)
            top = ranked[0] if ranked else None
            msg = (
                f"Daily discover (full) · {len(_candidates)} tokens scanned · top {top.symbol} "
                f"{top.pump_score} ({top.cluster})"
                if top else "Daily discover (full) · no candidates found"
            )
            logger.info(msg)
            if store.enabled():
                await store.insert_bot_log("PUMP_SCANNER", "INFO", msg)
        except Exception as exc:
            logger.exception("daily discover failed")
            await notify.send_error("Daily discover", repr(exc))
        await asyncio.sleep(DISCOVER_INTERVAL_SECONDS)


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


# A position whose price feed stays dead this many minutes is reaped (closed at
# last known price). 0 disables. Long enough that a transient cache miss never
# trips it; short enough that a delisted/dead symbol frees its slot same session.
GHOST_REAP_MINUTES = float(os.getenv("PUMP_GHOST_REAP_MINUTES", "30"))


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
            now = datetime.now(UTC)
            for bot, key, pos in jobs:
                skey = (pos.exchange, pos.symbol)
                price = prices.get(skey, 0.0)
                if price <= 0:
                    # Ghost reaper: a position with no live price never steps, so it
                    # can never exit — it sits open forever (stuck flat, miscounting
                    # open slots = the "open=1 but no position" symptom). If the feed
                    # has been dead GHOST_REAP_MINUTES straight, close at last price.
                    pos.stale_since = pos.stale_since or now
                    if (GHOST_REAP_MINUTES > 0
                            and (now - pos.stale_since).total_seconds() / 60 >= GHOST_REAP_MINUTES):
                        for event in bot.pm.reap(key):
                            await _handle_exit(bot, pos, event)
                    continue
                pos.stale_since = None
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
    """Acelerador de reflejos del ÚNICO motor (prepump/FSM).

    Antes velocity era un motor de gainers/momentum APARTE (perseguía subidas =
    perdía). Reutilizado: velocity ahora SOLO acelera la ruptura de un token que
    el FSM ya validó (acumulación+persistencia+rug bajo) y tiene en 'confirmation'.
    El FSM pone la RIGIDEZ (qué); velocity pone la VELOCIDAD (cuándo) — dispara la
    compra en el milisegundo del break en vez de esperar los ~9 min de ticks de
    confirmación del scan. Sin token en 'confirmation' → no hace nada (cero chase)."""
    if _pipeline is None:
        return
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
                # RIGIDEZ: solo acelera lo que el FSM ya tiene listo para entrar.
                state = await asyncio.to_thread(_pipeline.state_of, t.symbol, t.exchange)
                if state != "confirmation":
                    continue
                candidate.last_price = t.price  # dispara al precio fresco del trigger
                _record_learning(
                    candidate.symbol, "velocity_ruptura", "paper", candidate,
                    f"ruptura acelerada (vol {t.accel:.1f}x @ {t.price}) — FSM confirmado",
                )
                for bot in all_bots():
                    if bot.auto_entry and not bot.pm.has(t.exchange, t.symbol):
                        # accel=t.accel → el gate de volumen ve el break REAL del velocity
                        # (no el volume_spike añejo del último scan).
                        if await _auto_enter(bot, candidate, accel=t.accel, fsm_path=True,
                                             setup_hint="velocity_ruptura"):
                            _pipeline.mark_entered(t.symbol, t.exchange)
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


async def _candidate_from_micro(exchange: str, symbol: str, intent_score: int) -> TokenCandidate | None:
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
    # Anti-TOP needs a MULTI-HOUR price window — micro snapshots span only minutes, so
    # a token that ramped over hours looks 'flat at the top' and slips past (the BTR
    # +40% top-entry). Fetch real 5m candles (~3h); fall back to the micro series.
    spark = [float(x.get("last_price") or 0.0) for x in rows]
    try:
        client = await _velocity._client(exchange.lower())
        if client is not None:
            ohlcv = await client.fetch_ohlcv(symbol.upper(), timeframe="5m", limit=36)
            closes = [c[4] for c in ohlcv if c and c[4] is not None]
            if len(closes) >= 6:
                spark = [round(float(c), 8) for c in closes]
    except Exception:
        pass
    # Confianza REAL del setup prepump (antes 100 FIJO → la tabla mostraba 100 en
    # TODAS, sin información). La señal FSM (acumulación = intent_score) domina, y la
    # calidad del libro la ajusta: libro más profundo + spread más ajustado = más
    # confianza. Acotado 0-100. Ahora cada posición muestra su confianza propia.
    _liq = float(r.get("liquidity_usd") or 0.0)
    _spr = float(r.get("spread_pct") or 0.0)
    _conf = 0.6 * float(intent_score)            # la señal FSM pesa lo principal
    _conf += min(_liq / 5000.0, 25.0)            # hasta +25 por libro profundo
    _conf += max(0.0, 10.0 - _spr * 10.0)        # hasta +10 por spread ajustado (<1%)
    confidence_score = int(max(0, min(100, round(_conf))))
    return TokenCandidate(
        id=str(uuid4()), symbol=symbol.upper(), exchange=exchange.lower(),
        last_price=price, quote_volume_24h=0.0, price_change_pct_24h=0.0,
        volume_spike=velocity,
        orderbook_imbalance=imbalance,
        liquidity_usd=float(r.get("liquidity_usd") or 0.0),
        pump_score=int(intent_score), confidence_score=confidence_score,
        classification="pre_pump_accumulation", cluster=cluster,
        spread_pct=float(r.get("spread_pct") or 0.0),
        top_book_share=float(r.get("top_book_share") or 0.0),
        # Multi-hour price series so the anti-TOP guard catches a ramp (see above).
        spark=spark,
        status=CandidateStatus.approved, updated_at=datetime.now(UTC),
    )


# --- Alertas EN CAPAS (lead-time): avisa ANTES de la entrada -------------------
# El video avisa "pump probable" desde antes y "pump AHORA" al confirmar. La FSM ya
# tiene los stages (watchlist→monitor→confirmation→entry); emitimos una alerta por
# TIER usando ese embudo, no solo al final. Así el pump queda "avisado y analizado
# desde antes" y solo se etiqueta AHORA al confirmar. NO es un reloj exacto (el
# order-book no predice CUÁNDO): es madurez/proximidad de la señal.
#   acumulando → monitor (scores subiendo) · MEDIA
#   inminente  → confirmation (sostiene)   · ALTA
#   ahora      → entry confirmado (compra) · ALTA
#   onchain    → presión DEX / inflows CEX detectados ANTES (señal líder) · ALTA
_ALERT_TIERS: dict[str, dict] = {
    "acumulando": {"cls": "pre_pump_watch",       "imp": "MEDIA", "label": "📡 ACUMULANDO · pump probable, vigilando", "track": False},
    "inminente":  {"cls": "pre_pump_imminent",    "imp": "ALTA",  "label": "🔥 INMINENTE · pump cerca",               "track": True},
    "ahora":      {"cls": "pre_pump_accumulation", "imp": "ALTA",  "label": "🚀 PUMP AHORA · señal confirmada",         "track": True},
    "onchain":    {"cls": "onchain_accumulation",  "imp": "ALTA",  "label": "🐋 ON-CHAIN · acumulación líder (antes)",  "track": True},
}
# cooldown por (tier, exchange, symbol): un token sosteniéndose no spamea cada tick.
_signal_alert_at: dict[str, float] = {}
SIGNAL_ALERT_COOLDOWN_S = int(os.getenv("PUMP_SIGNAL_ALERT_COOLDOWN_S",
                                        str(notify.ALERT_COOLDOWN_S)))
# Madurez mínima para avisar 'acumulando' (evita ruido de monitor inactivo).
EARLY_ALERT_MIN_MATURITY = int(os.getenv("PUMP_EARLY_ALERT_MIN_MATURITY", "40"))
# Probabilidad EMPÍRICA mínima (0..1) para DISPARAR una alerta líder "va a subir".
# El usuario la quiere a 80%: si avisa, es porque su historial confirmó >=80% en ese
# bucket. OJO realidad medida: el mejor bucket histórico (vol>6x) ronda ~31%, así que
# a 0.80 casi NADA dispara — es lo honesto (no gritar sin evidencia). Bajar a ~0.30
# si se prefiere "lo mejor que hay" en vez de silencio. La entrada NO se bloquea por
# esto (sigue operando en paper para generar la data que calibra la probabilidad).
ALERT_MIN_PROBABILITY = float(os.getenv("PUMP_ALERT_MIN_PROBABILITY", "0.45"))
# COLD-START: until a bucket has this many settled samples, the empirical prob is just
# the 10% prior (useless) → gate the leading "before-the-pump" alert on the live FSM
# SIGNAL MATURITY instead, so the user gets a real early heads-up NOW instead of silence
# until weeks of data exist. Once n >= MIN_SAMPLES the honest empirical prob takes over.
# Alerts are NOT entries: alert early & generously, BUY strictly (separate gates).
ALERT_MIN_SAMPLES = int(os.getenv("PUMP_ALERT_MIN_SAMPLES", "8"))
ALERT_COLDSTART_MIN_MATURITY = int(os.getenv("PUMP_ALERT_COLDSTART_MIN_MATURITY", "60"))
# Anti-TARDE para alertas LÍDER ("antes"): un token ya +N% en 24h (o ya parabólico
# intradía) NO está "acumulando antes" — el movimiento YA pasó. Suprime la alerta
# "líder (antes)" para que el bot deje de gritar 'antes' DESPUÉS del pump (SMART
# avisó +83% post-pump). El tier 'ahora' (entrada real) NO se ve afectado.
LEADING_TIERS = ("acumulando", "inminente", "onchain")
ALERT_LEADING_MAX_CHASE_PCT = float(os.getenv("PUMP_ALERT_LEADING_MAX_CHASE_PCT", "30"))


def _maturity_gauge(state: str, acc: int, pers: int, rug: int, cc: int) -> int:
    """0-100 'proximidad al pump' desde el stage FSM + scores. NO es un reloj: es
    qué tan MADURA está la señal (temprana → inminente → ahora)."""
    ct = max(1, FSM_CONFIRM_TICKS)
    if state == "entry":
        return 100
    if state == "confirmation":
        base = 65 + min(cc, ct) / ct * 25          # 65..90 según ticks sostenidos
    elif state == "monitor":
        base = 35
    else:                                          # watchlist
        base = 15
    sig = (acc + pers) / 2.0
    m = base + (sig - 50) * 0.4 - max(0, rug - 40) * 0.2
    return int(max(0, min(100, round(m))))


async def _emit_signal_alert(cand: TokenCandidate, scores, tier: str = "ahora",
                             maturity: int | None = None, extra: dict | None = None) -> None:
    """Telegram + registro de una señal pre-pump en su TIER. Best-effort: nunca
    lanza al loop. Dedupe por (tier, símbolo) para no spamear cada tick."""
    cfg = _ALERT_TIERS.get(tier, _ALERT_TIERS["ahora"])
    # Anti-TARDE: a "leading/antes" alert must fire BEFORE the move. If the token
    # already ran (24h chase or intraday parabolic), it's post-pump — suppress the
    # misleading "líder (antes)" alert (this is the SMART/USDT +83%-late case).
    if tier in LEADING_TIERS:
        runup, _ = _breakout_state(getattr(cand, "spark", None) or [])
        if cand.price_change_pct_24h >= ALERT_LEADING_MAX_CHASE_PCT or runup >= ENTRY_MAX_RUNUP_PCT:
            return
    key = f"{tier}:{cand.exchange}:{cand.symbol}"
    now = datetime.now(UTC).timestamp()
    last = _signal_alert_at.get(key)
    if last is not None and (now - last) < SIGNAL_ALERT_COOLDOWN_S:
        return
    _signal_alert_at[key] = now
    acc, pers, rug = scores.accumulation, scores.persistence, scores.rug_risk
    # EMPIRICAL probability the token actually pumps, from the bot's OWN settled
    # track record for this bucket (cluster+tier+volume). The loud "va a subir"
    # alert only fires when P >= ALERT_MIN_PROBABILITY — if it shouts, it's because
    # the data for this setup actually confirmed that often. No data / weak edge →
    # P stays low → no alert (the outcome is still tracked so P can grow over time).
    prob, prob_n = _lab.pump_probability(
        cluster=cand.cluster, tier=tier, vol_spike=cand.volume_spike)
    # Result-oriented reasons in plain words — NOT internal jargon (acc/pers/rug stay
    # for tracking only, below). This is what makes the alert read coherently.
    reasons: list[str] = []
    if cand.volume_spike and cand.volume_spike >= 1.2:
        reasons.append(f"volumen {cand.volume_spike:.1f}× sobre su media")
    if extra and extra.get("dex"):
        _br = extra["dex"].get("buy_ratio_h1")
        if _br is not None:
            reasons.append(f"compras DEX dominan ({float(_br):.0%})")
    if extra and extra.get("inflow") and (extra["inflow"].get("inflow_usd") or 0) > 0:
        reasons.append(f"entradas a exchange ${int(extra['inflow']['inflow_usd']):,}")
    if maturity is not None:
        reasons.append(f"madurez {maturity}/100")
    # Honest odds: quote a % only once it's backed by samples, else say so plainly.
    odds = (f"sube {prob:.0%} histórico ({prob_n} casos)" if prob_n >= ALERT_MIN_SAMPLES
            else "señal nueva (sin histórico aún)")
    flags = [cfg["label"], odds] + reasons   # persisted for the on-screen panel
    # Confidence gate for LEADING ("antes del pump") alerts. Cold-start aware: with
    # enough settled samples gate on the EMPIRICAL prob (honest); before that the prob
    # is just the prior → gate on the live FSM signal MATURITY so the user still gets a
    # real early heads-up NOW. Non-leading tiers ("ahora") always send (it's the buy).
    if tier not in LEADING_TIERS:
        send_ok = True
    elif prob_n >= ALERT_MIN_SAMPLES:
        send_ok = prob >= ALERT_MIN_PROBABILITY
    else:
        send_ok = (maturity or 0) >= ALERT_COLDSTART_MIN_MATURITY
    if send_ok:
        try:
            await notify.send_alert(format_alert(
                cand.symbol, cfg["label"], cand.exchange, cand.liquidity_usd,
                reasons, odds, importance=cfg["imp"],
            ))
        except Exception:
            logger.exception("signal alert send failed")
    if cfg["track"]:
        try:
            _lab.record_alert(
                symbol=cand.symbol, exchange=cand.exchange, alert_price=cand.last_price,
                pump_score=acc, cluster=cand.cluster, classification=cfg["cls"],
                signals={"accumulation": acc, "persistence": pers, "rug_risk": rug,
                         "maturity": maturity, "tier": tier,
                         "liquidity_usd": cand.liquidity_usd, "volume_spike": cand.volume_spike},
            )
        except Exception:
            logger.exception("signal alert record failed")
    try:
        await store.insert_alert({
            "symbol": cand.symbol, "exchange": cand.exchange,
            "pump_score": maturity if maturity is not None else acc,
            "classification": cfg["cls"], "flags": flags,
        })
        await store.insert_bot_log(
            "PUMP_SCANNER", "INFO",
            f"[{tier}] {cand.symbol} ({cand.exchange.upper()}) "
            f"madurez {maturity} · acc {acc} / pers {pers} / rug {rug}",
            volumen=cand.volume_spike,
        )
        if tier in ("ahora", "onchain"):
            await store.insert_pump_candidate({
                "symbol": cand.symbol, "exchange": cand.exchange.upper(),
                "current_spread": cand.spread_pct, "volume_acceleration": cand.volume_spike,
                "status": "TRIGGERED",
            })
    except Exception:
        logger.exception("signal alert persist failed")


async def _pipeline_loop() -> None:
    """FASE 2: avanza la máquina de estados cada PIPELINE_TICK_SECONDS. En modo
    shadow solo registra en decision_log lo que HARÍA; en enforcing ejecuta los
    intents confirmados por el motor de ejecución actual (sin tocar TP/SL/risk).
    Best-effort: nunca tumba el bot."""
    while True:
        try:
            if _pipeline is not None:
                intents = await asyncio.to_thread(_pipeline.tick)
                # Reconcile FSM 'entry' (COMPRADO) con las posiciones REALES abiertas:
                # mata el "comprado fantasma" (entry viejo que sobrevivió a un cierre o
                # reinicio). Así COMPRADO en el funnel = lo que de verdad está comprado.
                _open_keys = {(p.symbol.upper(), p.exchange.lower())
                              for bot in all_bots() for p in bot.pm.positions.values()
                              if not p.closed}
                await asyncio.to_thread(_pipeline.reconcile_entries, _open_keys)
                if intents and _pipeline.mode == "enforcing":
                    for it in intents:
                        cand = _find_candidate(it.exchange, it.symbol)
                        if cand is None:
                            # Scan dropped it — rebuild from the live micro series
                            # so the PRE-PUMP signal isn't lost (the whole point).
                            cand = await _candidate_from_micro(it.exchange, it.symbol, it.scores.accumulation)
                        if cand is None:
                            continue
                        # Alerta de cara al usuario: la FSM confirmó = "va a subir".
                        # Independiente de si algún bot llega a comprar (capital/cupo).
                        await _emit_signal_alert(cand, it.scores)
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
                # EARLY alerts (lead-time): avisa en monitor/confirmation ANTES de la
                # entrada para que el pump quede "avisado desde antes". Cooldown por
                # tier dedupe; best-effort (skip si no está entre candidatos vivos).
                try:
                    board = await asyncio.to_thread(_pipeline.board, 40)
                except Exception:
                    board = []
                for r in board:
                    st = r.get("state")
                    if st not in ("monitor", "confirmation"):
                        continue
                    acc, pers, rug = int(r.get("acc") or 0), int(r.get("pers") or 0), int(r.get("rug") or 0)
                    cc = int(r.get("confirm_count") or 0)
                    mat = _maturity_gauge(st, acc, pers, rug, cc)
                    if st == "monitor" and mat < EARLY_ALERT_MIN_MATURITY:
                        continue
                    cand = _find_candidate(r["exchange"], r["symbol"])
                    if cand is None:
                        continue
                    sc = SimpleNamespace(accumulation=acc, persistence=pers, rug_risk=rug)
                    tier = "inminente" if st == "confirmation" else "acumulando"
                    await _emit_signal_alert(cand, sc, tier=tier, maturity=mat)
        except Exception:
            logger.exception("pipeline loop failed")
        await asyncio.sleep(PIPELINE_TICK_SECONDS)


# --- Señal LÍDER on-chain: presión DEX (DexScreener, free) + inflows a CEX --------
# (Etherscan, free key). Para los símbolos que la FSM ya vigila, mira la actividad
# ON-CHAIN que precede al pump por horas: compras en DEX + supply entrando a un CEX.
# Si se calienta ANTES de que el order-book confirme → alerta temprana 'onchain'.
# Acotado (top N del board) + cacheado en los módulos. Best-effort, nunca bloquea.
ONCHAIN_LOOP_SECONDS = int(os.getenv("PUMP_ONCHAIN_LOOP_SECONDS", "150"))
ONCHAIN_MAX_SYMBOLS = int(os.getenv("PUMP_ONCHAIN_MAX_SYMBOLS", "12"))
ONCHAIN_HEAT_ALERT = int(os.getenv("PUMP_ONCHAIN_HEAT_ALERT", "55"))
# ENTRADA por señal líder on-chain: compra ANTES de que el order-book confirme,
# cuando (a) el calor on-chain es FUERTE (umbral más alto que la alerta) y (b) el
# FSM ya vigila el token pero aún NO confirmó (watchlist/monitor). Eso es "entrar
# durante la acumulación on-chain real" = antes del movimiento CEX. Paper-only;
# las protecciones de _auto_enter (anti-TOP, forensic, risk) siguen aplicando.
ONCHAIN_AUTOENTRY = os.getenv("PUMP_ONCHAIN_AUTOENTRY", "false").lower() == "true"
ONCHAIN_ENTRY_HEAT = int(os.getenv("PUMP_ONCHAIN_ENTRY_HEAT", "70"))
_onchain_heat: dict[str, dict] = {}     # "exchange:symbol" -> {dex, inflow, heat, at}


def _onchain_heat_score(dex: dict | None, inflow: dict | None) -> int:
    """0-100 'calor on-chain' = presión compradora líder. DEX buy-ratio + volumen 1h
    corriendo caliente vs media 24h + inflows a CEX. Solo datos reales; 0 si no hay."""
    h = 0.0
    if dex:
        br = dex.get("buy_ratio_h1")
        if br is not None and br >= 0.55:
            h += min(40.0, (br - 0.50) * 200)            # sesgo comprador en DEX
        v1, v24 = float(dex.get("vol_h1") or 0), float(dex.get("vol_h24") or 0)
        if v24 > 0 and v1 * 24 >= v24 * 1.5:             # volumen horario acelerando
            h += 25
        if float(dex.get("price_change_h1") or 0) > 0:
            h += 10
    if inflow:
        if (inflow.get("large_deposits") or 0) > 0 or float(inflow.get("inflow_usd") or 0) >= 100_000:
            h += 30                                       # supply llegando a un CEX
    return int(min(100, h))


async def _try_onchain_entry(exchange: str, symbol: str, fsm_row: dict, info: dict) -> None:
    """Entrada LÍDER: el calor on-chain cruzó el umbral de entrada y el FSM aún no
    confirmó → compra durante la acumulación on-chain (antes del movimiento CEX).
    Paper-only. Best-effort. _auto_enter aplica anti-TOP + forensic + risk igual."""
    if current_mode() != ExecMode.paper:
        return                                   # nunca live por heurística on-chain
    cand = _find_candidate(exchange, symbol)
    if cand is None:
        cand = await _candidate_from_micro(exchange, symbol, int(fsm_row.get("acc") or 0))
    if cand is None:
        return
    entered = False
    for bot in all_bots():
        if not bot.auto_entry or bot.pm.has(exchange, symbol):
            entered = entered or bot.pm.has(exchange, symbol)
            continue
        # onchain_lead=True → waive the CEX breakout/volume gates: the strong on-chain
        # buy pressure IS the lead confirmation. This is the genuine "buy DURING
        # accumulation, before the venue moves" door (the rest of _auto_enter's
        # capital protections — rug/anti-dump/forensic/risk/ceilings — still apply).
        if await _auto_enter(bot, cand, fsm_path=True, onchain_lead=True):
            entered = True
    if entered:
        _record_learning(symbol, "onchain_lead_entry", "paper", cand,
                         f"on-chain heat {info.get('heat')} (líder, pre-confirmación)")
        if _pipeline is not None:
            try:
                _pipeline.mark_entered(symbol, exchange)
            except Exception:
                logger.exception("onchain entry mark failed")


async def _onchain_loop() -> None:
    from . import dexscreener as _dx
    from . import onchain as _oc
    try:
        from . import etherscan as _es
    except Exception:
        _es = None
    es_key = bool(os.getenv("ETHERSCAN_API_KEY"))
    while True:
        try:
            if _pipeline is not None:
                try:
                    board = await asyncio.to_thread(_pipeline.board, 40)
                except Exception:
                    board = []
                watched = [r for r in board
                           if r.get("state") in ("watchlist", "monitor", "confirmation")][:ONCHAIN_MAX_SYMBOLS]
                for r in watched:
                    ex, sym = r["exchange"], r["symbol"]
                    base = sym.split("/")[0]
                    dex = await _dx.dex_activity(ex, base)
                    inflow = None
                    if _es is not None and es_key:
                        try:
                            inflow = await _es.exchange_inflows(ex, base, price=0.0)
                        except Exception:
                            inflow = None
                    if not (dex or inflow):
                        continue
                    heat = _onchain_heat_score(dex, inflow)
                    info = {"dex": dex, "inflow": inflow, "heat": heat,
                            "at": datetime.now(UTC).isoformat()}
                    # Contract security (GoPlus): honeypot / blacklist / cannot-sell /
                    # high sell-tax = rug INTENT → Dangerous_Signals. Only DEX-covered
                    # tokens (a contract exists to check); cached 2h so it's cheap per loop.
                    # On a CEX you still sell vs the venue book, so this is a scam-intent
                    # signal, not a can't-exit block.
                    if dex is not None:
                        try:
                            sec = await _oc.holder_concentration(ex, base)
                        except Exception:
                            sec = None
                        if sec:
                            info["security"] = sec
                            if sec.get("dangerous_contract"):
                                _mark_dangerous(ex, sym)
                    _onchain_heat[f"{ex}:{sym}"] = info
                    if heat >= ONCHAIN_HEAT_ALERT:
                        cand = _find_candidate(ex, sym)
                        if cand is not None:
                            sc = SimpleNamespace(accumulation=int(r.get("acc") or 0),
                                                 persistence=int(r.get("pers") or 0),
                                                 rug_risk=int(r.get("rug") or 0))
                            await _emit_signal_alert(cand, sc, tier="onchain", maturity=heat, extra=info)
                    # Entrada líder: calor FUERTE + FSM aún sin confirmar = compra ANTES.
                    if (ONCHAIN_AUTOENTRY and heat >= ONCHAIN_ENTRY_HEAT
                            and r.get("state") in ("watchlist", "monitor")):
                        await _try_onchain_entry(ex, sym, r, info)
        except Exception:
            logger.exception("onchain loop failed")
        await asyncio.sleep(ONCHAIN_LOOP_SECONDS)


COINBASE_POLL_SECONDS = int(os.getenv("PUMP_COINBASE_POLL_SECONDS", "120"))
COINBASE_LISTING_ENTER = os.getenv("PUMP_COINBASE_LISTING_ENTER", "false").lower() == "true"


async def _handle_coinbase_listing(ev: dict) -> None:
    """A Coinbase listing/relisting fired = high-conviction pump catalyst. ALERT
    always; RECORD it to the learning lab so MFE/MAE proves whether the edge is real
    in OUR universe BEFORE we risk paper capital on it. Auto-entry stays OFF by
    default (PUMP_COINBASE_LISTING_ENTER) — measure the edge first, then enable."""
    base = ev["base"]
    cand = None
    for ex in SUPPORTED_EXCHANGES:
        cand = _find_candidate(ex, f"{base}/USDT")
        if cand is not None:
            break
    label = "nuevo listing" if ev["kind"] == "new_listing" else "en vivo (listing day)"
    try:
        await notify.send_alert(format_alert(
            f"{base}/USDT", f"🟦 COINBASE · {label}",
            exchange=(cand.exchange if cand else "—"),
            liquidity_usd=(cand.liquidity_usd if cand else 0.0),
            reasons=["catalizador de pump (listing Coinbase)", "señal líder pública (gratis)",
                     ("cotiza en " + cand.exchange) if cand else "aún no en tus CEX"],
            odds="catalizador confirmado", importance="ALTA",
        ))
    except Exception:
        logger.exception("coinbase alert failed")
    await store.insert_bot_log("PUMP_SCANNER", "INFO",
                               f"Coinbase {ev['kind']}: {base} (catalizador de pump)")
    # Track the outcome (does it actually pump in our universe?) without trading yet.
    if cand is not None:
        try:
            _lab.record_alert(
                symbol=cand.symbol, exchange=cand.exchange, alert_price=cand.last_price,
                pump_score=90, cluster="long_pump", classification="coinbase_listing",
                signals={"source": "coinbase", "kind": ev["kind"],
                         "liquidity_usd": cand.liquidity_usd, "volume_spike": cand.volume_spike},
            )
        except Exception:
            logger.exception("coinbase record_alert failed")
        # Optional paper entry (off by default until the edge is proven).
        if COINBASE_LISTING_ENTER and current_mode() == ExecMode.paper:
            cand.cluster = "long_pump"
            for bot in all_bots():
                if bot.auto_entry and not bot.pm.has(cand.exchange, cand.symbol):
                    await _auto_enter(bot, cand, book="gainers", skip_gates=True,
                                      setup_hint="coinbase_listing")


async def _coinbase_loop() -> None:
    """Poll Coinbase's public products API; a NEW listing (or a relist going live) =
    the 'Coinbase effect' pump catalyst. Free, public, genuinely leading."""
    from . import coinbase_listings as cb
    while True:
        try:
            curr = await cb.fetch_products()
            if curr:
                raw = await store.get_state("coinbase_products")
                prev = json.loads(raw) if raw else {}
                if prev:   # skip the first boot — everything would look 'new'
                    for ev in cb.detect_events(prev, curr):
                        await _handle_coinbase_listing(ev)
                await store.set_state("coinbase_products", json.dumps(curr))
        except Exception:
            logger.exception("coinbase loop failed")
        await asyncio.sleep(COINBASE_POLL_SECONDS)


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
                # Recover the P&L bucket + entry phase from local bot_state (neither is
                # a Supabase column). Unknown → prepump/ruptura (older positions predate
                # the tagging). Keeps the honest badge across restarts.
                pos.book = await store.get_state(f"book:{bot.uid}:{pos.exchange}:{pos.symbol}") or "prepump"
                pos.entry_phase = (await store.get_state(f"phase:{bot.uid}:{pos.exchange}:{pos.symbol}")
                                   or ("momentum" if pos.book == "gainers" else "ruptura"))
                bot.pm.positions[bot.pm.key(pos.exchange, pos.symbol)] = pos
                total += 1
            except Exception:
                logger.exception("restore position failed for row %s", r)
        # Rehydrate this bot's equity curve so the chart isn't blank after restart.
        try:
            pts = await store.list_equity(200, user_id=bot.uid)
            if pts:
                # Filtro de outliers: un bot paper con stop de drawdown ~10% NO puede
                # caer a la mitad entre puntos. Los $100 sueltos eran glitches de
                # escritura que reventaban el eje Y del chart. < 50% de la mediana =
                # imposible → no se rehidrata. (Si todo es basura, deja el crudo.)
                _vals = sorted(float(p.get("v") or 0) for p in pts if float(p.get("v") or 0) > 0)
                _med = _vals[len(_vals) // 2] if _vals else 0.0
                _floor = _med * 0.5
                _clean = [{"t": p.get("t"), "v": float(p.get("v") or 0)}
                          for p in pts if float(p.get("v") or 0) >= _floor]
                bot.equity_history.clear()
                bot.equity_history.extend(_clean or [
                    {"t": p.get("t"), "v": float(p.get("v") or 0)} for p in pts])
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
        # Rehydrate the 'Recent Exits' feed so it survives a restart (pm.history is
        # session-only -> went blank every boot even though exits are persisted).
        try:
            rex = await store.list_recent_exits(user_id=bot.uid, limit=20)
            chron = sorted(rex, key=lambda r: r.get("at") or "")   # oldest -> newest
            hist: list[ExitEvent] = []
            for r in chron:
                ex, sym = r.get("exchange", ""), r.get("symbol", "")
                # `book` is NOT a Supabase column (stripped on insert → PostgREST-safe),
                # so a rehydrated exit defaulted to 'prepump' and EVERY gainers exit
                # vanished from the Gainers panel on restart ("no se registran los
                # gainers"). Recover the P&L bucket from local bot_state (same key the
                # open path writes + the position restore reads). Unknown → prepump.
                bk = await store.get_state(f"book:{bot.uid}:{ex}:{sym}") or "prepump"
                hist.append(ExitEvent(
                    symbol=sym, exchange=ex,
                    reason=r.get("reason", ""), sold_qty=float(r.get("sold_qty") or 0),
                    price=float(r.get("price") or 0), pnl=float(r.get("pnl") or 0),
                    fraction=float(r.get("fraction") or 1.0), closed=bool(r.get("closed", True)),
                    book=bk, at=r.get("at") or "",
                ))
            bot.pm.history = hist
        except Exception:
            logger.exception("recent exits rehydrate failed for %s", bot.uid)
        # Seed a baseline equity point so the curve never renders flat-zero after a
        # hard reset (which wipes equity_history). With no point the chart shows 0.
        if not bot.equity_history:
            pt = {"t": datetime.now(UTC).isoformat(), "v": bot.balance()}
            bot.equity_history.append(pt)
            try:
                await store.insert_equity({**pt, "user_id": bot.uid})
            except Exception:
                logger.exception("equity seed persist failed for %s", bot.uid)
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
    # NB: strip 'book' — it's an in-memory P&L bucket, NOT a DB column. Persisting it
    # would make PostgREST reject the whole row (unknown column) and exit history would
    # silently stop saving. Book is re-derived in memory; old exits default to prepump.
    store.enqueue(lambda r={k: v for k, v in event.__dict__.items() if k != "book"} | {"user_id": bot.uid}: store.insert_exit(r))
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
        # FSM: clear the 'entry' state so the closed trade stops showing as a live
        # buy on the board (stale 'entry' piled up: 17 shown vs 1 actually open).
        if _pipeline is not None:
            try:
                _pipeline.mark_closed(pos.symbol, pos.exchange)
            except Exception:
                logger.exception("pipeline mark_closed failed")
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


# --- P5: learning components -> scoring weights -----------------------------
# The LearningLab measures, per signal, the LIFT = mean(confirmed pumps) -
# mean(duds). A positive lift means that signal really separated winners, so it
# should weigh MORE in the scanner's score; a negative/flat one should weigh less.
# We normalise the lift within each cluster (signals are on different scales),
# average across clusters, and map to a bounded multiplier the scanner clamps.
_LEARN_KEYMAP = {
    "volume_spike": "volume_spike",
    "price_change_pct_24h": "price_change",
    "orderbook_imbalance": "imbalance",
    "liquidity_usd": "liquidity",
}


def _compute_learned_weights(components: dict) -> dict | None:
    """Turn LearningLab lift into per-signal weights, or None if no cluster ready."""
    acc: dict[str, list[float]] = {v: [] for v in _LEARN_KEYMAP.values()}
    any_ready = False
    for cluster in ("long_pump", "classic"):
        c = components.get(cluster) or {}
        if not c.get("ready"):
            continue
        contrib = c.get("contrib") or []
        maxabs = max((abs(x.get("lift", 0.0)) for x in contrib), default=0.0)
        if maxabs <= 0:
            continue
        any_ready = True
        for x in contrib:
            sk = _LEARN_KEYMAP.get(x.get("signal"))
            if sk:
                acc[sk].append(x.get("lift", 0.0) / maxabs)   # normalised [-1,1]
    if not any_ready:
        return None
    # weight = 1 + 0.3 * avg(normalised lift)  -> bounded ~[0.7,1.3] (scanner clamps)
    return {sk: round(1.0 + 0.3 * (sum(v) / len(v)), 3) if v else 1.0
            for sk, v in acc.items()}


def _learned_weights_str() -> str:
    """Compact 'vol×1.0 prc×1.1 imb×0.9 liq×1.0' for the autotune table."""
    w = LEARNED_WEIGHTS
    return (f"vol×{w['volume_spike']:.2f} prc×{w['price_change']:.2f} "
            f"imb×{w['imbalance']:.2f} liq×{w['liquidity']:.2f}")


def _apply_learned_weights() -> dict | None:
    """Recompute + push learned scoring weights from the LearningLab. Persisted so
    the learned edge survives restarts. Returns the applied weights (or None)."""
    try:
        w = _compute_learned_weights((_lab.metrics() or {}).get("components") or {})
        if w:
            set_learned_weights(w)
            store.enqueue(lambda: store.set_state("learned_weights", json.dumps(w)))
            logger.info("✅ P5 pesos de scoring aprendidos aplicados: %s", w)
        return w
    except Exception:
        logger.exception("learned-weights apply failed")
        return None


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
            # P3: break-even auto-tune. Arm at ~40% of the typical win so a trade
            # only locks no-loss AFTER a real move — never so low it scratches a live
            # pump on a tiny wiggle (that capped wins at ~$0.9). Looser band [2.5, 6]
            # so the trailing (5% giveback) does the profit-running, not break-even.
            avg_win = opt.get("avg_win") or 0.0
            be = max(2.5, min(6.0, round(avg_win * 0.4, 1))) if avg_win > 0 else None
            if be:
                os.environ["PUMP_BREAKEVEN_PCT"] = str(be)
            logger.info(f"✅ Trailing/Hard/BE optimizado: Trailing={trail}%, HardStop={opt['sl']}%, BreakEven={be}% | Avg Win: {opt['avg_win']:.1f}%, Avg Loss: {opt['avg_loss']:.1f}%")
    except Exception:
        logger.exception("Trailing/SL optimization failed")


async def _optimize_timeout() -> None:
    """Optimiza Timeout por BOOK (learning). gainers → PUMP_TIMEOUT_MINUTES (base/
    momentum, corto); prepump → PUMP_PREPUMP_TIMEOUT_MINUTES (acumulación, horas).
    Separados para que el lead de ~21h del prepump no infle el timeout base/gainers."""
    global _lab
    try:
        opt = await asyncio.to_thread(_lab.optimize_timeout)
        g = (opt.get("gainers") or {}).get("timeout")
        if g:
            os.environ["PUMP_TIMEOUT_MINUTES"] = str(g)
            logger.info(f"✅ Timeout gainers optimizado: {g} min (n={opt['gainers']['n_samples']})")
        p = (opt.get("prepump") or {}).get("timeout")
        if p:
            os.environ["PUMP_PREPUMP_TIMEOUT_MINUTES"] = str(p)
            logger.info(f"✅ Timeout prepump optimizado: {p} min (n={opt['prepump']['n_samples']})")
    except Exception:
        logger.exception("Timeout optimization failed")


OPTIMIZE_INTERVAL_SECONDS = int(os.getenv("PUMP_OPTIMIZE_INTERVAL_SECONDS", "3600"))  # 1h


async def _optimization_loop() -> None:
    """Auto-ajuste del bot: aplica SOLO las propuestas del learning (el usuario no
    toca nada). Corre cada hora (antes 24h). Todos los optimizadores clampean a
    bandas seguras y solo actúan con datos suficientes, así que la cadencia rápida
    no descarrila — solo hace el bot más reactivo."""
    global _adaptive_threshold, ALERT_MIN_PROBABILITY
    while True:
        await asyncio.sleep(OPTIMIZE_INTERVAL_SECONDS)
        try:
            await _optimize_tp_sl()
            await _optimize_timeout()
            await asyncio.to_thread(_apply_learned_weights)   # P5: retune scoring weights

            # AUTO-APLICA las propuestas del learning (raise/lower threshold). Antes
            # se MOSTRABAN como "acción sugerida" para que el usuario las hiciera a
            # mano; ahora el propio learning las ejecuta. Mismo criterio que _proposals.
            metrics = await asyncio.to_thread(_lab.metrics)
            applied = []
            for p in metrics.get("proposals", []):
                kind = p.get("kind")
                before = _adaptive_threshold
                if kind == "raise_threshold":
                    _adaptive_threshold = min(THRESHOLD_CEIL, _adaptive_threshold + 5)
                elif kind == "lower_threshold":
                    _adaptive_threshold = max(THRESHOLD_FLOOR, _adaptive_threshold - 5)
                else:
                    continue
                if _adaptive_threshold != before:
                    applied.append(f"{kind} {before:.0f}→{_adaptive_threshold:.0f}")
            if applied:
                _persist_threshold()
                for c in _candidates.values():
                    c.status = _status_for(c.pump_score)   # refleja el nuevo umbral ya
                logger.info("✅ Learning auto-aplicó: %s", "; ".join(applied))
                await store.insert_bot_log("PUMP_LEARNING", "INFO",
                                           f"Auto-ajuste umbral: {'; '.join(applied)}")

            # ALERT precision auto-tune: la precisión medida ajusta el filtro de
            # probabilidad de las alertas LÍDER ("antes del pump"). Baja precisión →
            # sube el piso (menos alertas, mejores). Alta precisión + lead corto →
            # baja el piso (avisa antes). Banda [0.25, 0.70]. Persiste como los pesos.
            prec = metrics.get("precision")
            if prec is not None and metrics.get("n_settled", 0) >= ALERT_MIN_SAMPLES:
                before_a = ALERT_MIN_PROBABILITY
                lead = metrics.get("avg_lead_secs") or 0
                if prec < 0.40:
                    ALERT_MIN_PROBABILITY = min(0.70, round(ALERT_MIN_PROBABILITY + 0.05, 2))
                elif prec > 0.65 and 0 < lead < 3600:
                    ALERT_MIN_PROBABILITY = max(0.25, round(ALERT_MIN_PROBABILITY - 0.05, 2))
                if ALERT_MIN_PROBABILITY != before_a:
                    os.environ["PUMP_ALERT_MIN_PROBABILITY"] = str(ALERT_MIN_PROBABILITY)
                    store.enqueue(lambda v=ALERT_MIN_PROBABILITY: store.set_state("alert_min_probability", str(v)))
                    logger.info("✅ Alert prob auto-tune: %.2f→%.2f (precisión %.0f%%)",
                                before_a, ALERT_MIN_PROBABILITY, prec * 100)
                    await store.insert_bot_log("PUMP_LEARNING", "INFO",
                                               f"Auto-ajuste alertas: piso prob {before_a:.2f}→{ALERT_MIN_PROBABILITY:.2f} (precisión {prec:.0%})")
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


async def _grid_token_for(request: Request) -> str | None:
    """Mint/return the logged-in user's GRVTBot JWT (cached 12h). Shared by /grid-sso
    AND the /grid proxy's auth-injection, so the embedded SPA is ALWAYS authenticated
    as this user without the grid login ever showing — incluso si el browser nunca
    guarda/manda el token. El password derivado nunca llega al browser."""
    import time

    user = getattr(request.state, "user", None) or {"id": "owner"}
    uid = str(user.get("id") or "owner")
    now = time.time()
    cached = _grid_token_cache.get(uid)
    if cached and (now - cached[1]) < 12 * 3600:
        return cached[0]

    email, password = _grid_creds(user)
    # El grvtbot puede ser un contenedor APARTE (grvtbot:3848) → usar el mismo backend
    # configurable que el proxy. Hardcodear 127.0.0.1 rompía el SSO en Docker.
    backend = f"http://{os.getenv('GRVT_BACKEND_HOST', '127.0.0.1:3848')}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{backend}/api/v2/auth/login",
                json={"email": email, "password": password},
            )
            if resp.status_code != 200:
                # First time for this user (or no account yet): create it, then
                # the returned token logs them straight in.
                resp = await client.post(
                    f"{backend}/api/v2/auth/signup",
                    json={"email": email, "password": password, "terms_lang": "es"},
                )
    except httpx.HTTPError:
        return cached[0] if cached else None

    if resp.status_code == 200:
        token = resp.json().get("token")
        if token:
            _grid_token_cache[uid] = (token, now)
        return token
    return cached[0] if cached else None


@app.get("/grid-sso")
async def grid_sso(request: Request):
    """Single sign-on for the embedded GRVTBot, scoped to the logged-in user. The
    TradeOS login is the only login the user sees; returns THIS user's GRVT JWT
    (cached 12h) for the iframe SPA. El password derivado nunca llega al browser."""
    token = await _grid_token_for(request)
    if token:
        return {"ok": True, "key": "grvt-grid-token", "token": token}
    return JSONResponse({"ok": False, "error": "grid_login_failed"}, status_code=502)


# El proxy /grid inyecta este token en /grid/api/v2/* cuando el browser no manda
# Authorization (arregla "No se pudieron cargar los bots"). Set aquí, tras definir la
# función, para evitar el import circular con grvt_proxy.
set_grid_token_provider(_grid_token_for)


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
    # Decision log (audit trail legible): cada rechazo/venta justificado por su filtro.
    decision_log.from_learning(symbol, action, detail, pump_score=pump_score,
                               classification=classification)
    if store.enabled():
        asyncio.create_task(store.insert_learning({
            "id": rec.id, "symbol": rec.symbol, "action": rec.action, "mode": rec.mode,
            "pump_score": rec.pump_score, "classification": rec.classification,
            "detail": rec.detail, "created_at": rec.created_at.isoformat(),
        }))


def _record_learning(symbol: str, action: str, mode: str, candidate: TokenCandidate, detail: str) -> None:
    _record_learning_raw(symbol, action, mode, candidate.pump_score, candidate.classification, detail)


def _learn_dangerous(candidate: TokenCandidate, reason: str) -> None:
    """Alimenta un token bloqueado por scam/rug al bucket Dangerous_Signals del
    aprendizaje (§4) → pump_probability penaliza ese perfil de señal. Best-effort."""
    try:
        _lab.record_dangerous(
            symbol=candidate.symbol, exchange=candidate.exchange,
            cluster=getattr(candidate, "cluster", "long_pump") or "long_pump",
            signals={"volume_spike": candidate.volume_spike,
                     "liquidity_usd": candidate.liquidity_usd,
                     "price_change_pct_24h": candidate.price_change_pct_24h},
            reason=reason,
        )
    except Exception:
        logger.exception("record_dangerous failed")


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
# Pre-pump score floor — rigidez: don't enter sub-quality calificaciones. Measured:
# HYPER entered @ score 36, ZKC @ 33 (junk) and bled; the winner DOGS scored 55. So a
# floor of 50 cuts the obvious noise without blocking the winners. Score is a WEAK
# separator on its own (DOGS 55 won, FTT 60 was a death-trap), so the liquidity floors
# do the heavy lifting — this just stops "cualquier cosa" sub-50 from entering.
PREPUMP_MIN_SCORE = float(os.getenv("PUMP_PREPUMP_MIN_SCORE", "50"))
# REGIME GATE: abstención cuando el régimen de mercado es "unknown" (sin tendencia
# clasificable). Medido: el bucket unknown rinde profit_factor 0.092 (38 trades, casi
# todo pérdida = ~36% del drawdown total) y hoy entra igual porque el régimen "nunca
# gatea". Bloquea SOLO trend=="unknown" (bull/bear/sideways pasan y se segmentan, no se
# vetan). Cold-start ("" sin clasificar todavía) pasa para no ahogar el arranque. Tunable.
PREPUMP_SKIP_UNKNOWN_REGIME = os.getenv("PUMP_SKIP_UNKNOWN_REGIME", "true").lower() == "true"
# EDGESCORE GATE — el ranker de rentabilidad (Fase 2 EdgeScore). Entre los criminal-pumps
# que pasaron TODAS las protecciones, opera solo los buckets con edge esperado real (MFE
# esperado del bucket, de outcomes propios; ver LearningLab.edge_score). El patrón
# criminal-pump sigue siendo el QUÉ; el EdgeScore decide el CUÁL. Cold-start (n <
# EDGE_MIN_SAMPLES) NO gatea: deja operar pa acumular los datos que el EdgeScore necesita
# (mejora con el tiempo — el motivo de elegir B). EDGE_MIN_SCORE = MFE % esperado mínimo.
EDGE_GATE_ENABLED = os.getenv("PUMP_EDGE_GATE", "true").lower() == "true"
# EDGE-WEIGHTED SIZING (gradual teeth): aplica el multiplicador de confianza (0.5–1.5×) al
# tamaño REAL — antes se calculaba pero quedaba en "simulación" y NUNCA se aplicaba, así
# que el bot apostaba IGUAL en bitget (gana, PF 2.05) que en binance (pierde, PF 0.29).
# Con confidence_for exchange-aware el capital fluye al edge medido por venue×setup. Gradual
# (no bloquea, escala) + auto-corrige (si un venue mejora, su tamaño vuelve a subir).
EDGE_SIZING_ENABLED = os.getenv("PUMP_EDGE_SIZING", "true").lower() == "true"
EDGE_MIN_SAMPLES = int(os.getenv("PUMP_EDGE_MIN_SAMPLES", "8"))
# Umbral bajo a propósito: la medición reveló que el edge derivado de ALERTAS es débil en
# TODO bucket (los que aplican a entradas reales, vol≥4×, dan MFE esperado ~0.4-1.0%). Un
# umbral alto (3%) detendría el bot entero. 0.5 gatea solo lo muerto sin frenar la
# operación; el EdgeScore-de-alertas afina con el tiempo. El gate FUERTE de rentabilidad
# vendrá del edge por TRADES reales (expectancy por exchange: bitget gana, binance pierde).
EDGE_MIN_SCORE = float(os.getenv("PUMP_EDGE_MIN_SCORE", "0.5"))
# Precio MÁXIMO para la tesis criminal-pump (¢→$1→$2): el multi-x grande está en tokens
# de precio bajo; uno ya caro tiene poco espacio. Enfoca el universo PRE-PUMP a precio
# bajo. 0 = desactivado. Solo PRE-PUMP (gainers es momentum, price-agnostic). Tunable.
PREPUMP_MAX_PRICE = float(os.getenv("PUMP_PREPUMP_MAX_PRICE", "1.0"))
# Veto por CONCENTRACIÓN de holders (rug-prone): un solo whale (>25%) o el top-10 (>70%)
# puede dumpear todo encima. Usa el dato on-chain ya calculado (holder_concentration).
# Solo dispara cuando HAY cobertura DEX (CEX-only sin contrato no tiene el dato → pasa).
PREPUMP_VETO_CONCENTRATED = os.getenv("PUMP_PREPUMP_VETO_CONCENTRATED", "true").lower() == "true"


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
    if not eng.trades:   # self-heal a transient startup-restore miss (was stuck at 0)
        try:
            rows = await store.list_trade_analytics()
            if rows:
                eng.load_rows(rows)
        except Exception:
            logger.debug("analytics lazy-reload failed", exc_info=True)
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


def _breakout_state(spark: list[float]) -> tuple[float, bool | None]:
    """Directional read of the price series (the FULL window provided).

    Returns (runup_pct, breaking):
      - runup_pct: % the last close is up from the window base (min over the WHOLE
        series). Big value = already ran = parabolic top (anti-TOP guard). Using the
        whole window (not just the last few bars) is what catches a MULTI-HOUR ramp:
        BTR entered at the top of a +40% climb because a 6-bar window looked 'flat at
        the top'. Each caller controls the horizon via how much spark it passes.
      - breaking: True if the last close is a fresh high over the last 3 bars (an
        up-break happening NOW); False if rolling over / flat (the MFE=0 trap);
        None when the series is too short to tell (don't block on no data)."""
    pts = [p for p in (spark or []) if p and p > 0]
    if len(pts) < 3:
        return 0.0, None
    base = min(pts)
    runup = (pts[-1] - base) / base * 100 if base > 0 else 0.0
    recent = pts[-3:]
    breaking = pts[-1] >= max(recent) - 1e-12   # fresh high over the last 3 bars
    return runup, breaking


async def _auto_enter(bot: UserBot, candidate: TokenCandidate, accel: float | None = None,
                      fsm_path: bool = False, book: str | None = None,
                      skip_gates: bool = False, setup_hint: str | None = None,
                      onchain_lead: bool = False) -> bool:
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
    # skip_gates: the GAINERS (momentum) path runs its OWN gates in _velocity_enter
    # (own anti-top ceiling, own liquidity floor) so the bot's prepump tuning never
    # bleeds into gainers and vice-versa. Capital protections (forensic + risk) below
    # STILL run for every path.
    if not fsm_path and not skip_gates:
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

    # Directional gate. anti-TOP applies to EVERY path (incl. pre-pump): never buy a
    # vertical spike. The anti-FLAT breakout + volume-break requirements apply ONLY to
    # the momentum/scan path. On the PRE-PUMP path they are SKIPPED on purpose: the
    # FSM already validated sustained accumulation/persistence over a window — that IS
    # the early signal, and the whole point is to enter AT accumulation (flat) BEFORE
    # the breakout, not wait for the run to confirm (which arrives late = LATE_ENTRY:
    # the alert said "about to rise" but the buy waited until it already rose). Capital
    # protections (anti-TOP here, ForensicFilter liquidity/spread/concentration +
    # RiskGuard below) still gate every entry.
    runup, breaking = _breakout_state(candidate.spark)
    if not skip_gates and runup >= ENTRY_MAX_RUNUP_PCT:
        _record_learning(candidate.symbol, "skip_parabolic", "paper", candidate,
                         f"+{runup:.0f}% en velas recientes (tope, no acumulación)")
        return False
    if not fsm_path and not skip_gates:
        # (b) anti-FLAT: require a CONFIRMED up-break so the momentum path doesn't buy
        # a flat base that breaks 50/50 (the MFE=+0.0% churn). Entry only in the band
        # [MIN_BREAKOUT .. MAX_RUNUP) AND making a fresh high.
        if breaking is not None and not (breaking and runup >= ENTRY_MIN_BREAKOUT_PCT):
            _record_learning(candidate.symbol, "skip_no_breakout", "paper", candidate,
                             f"sin ruptura alcista (+{runup:.1f}%, high={breaking}); plano, espera break")
            return False
        # Volume must back the break (momentum path). A fresh high on below-average
        # volume = no buyers = the fade/tail of a move (this is why MASK @0.6x entered late).
        entry_vol = accel if accel is not None else candidate.volume_spike
        if entry_vol is not None and entry_vol < ENTRY_MIN_BREAKOUT_VOL:
            _record_learning(candidate.symbol, "skip_no_volume_break", "paper", candidate,
                             f"ruptura sin volumen ({entry_vol:.1f}x < {ENTRY_MIN_BREAKOUT_VOL:.1f}x avg)")
            return False

    # (3) ForensicFilter — ALWAYS applies (capital integrity). Pre-pump uses a
    # lower liquidity floor (accumulation tokens have thinner books; rug already
    # vetted by the FSM rug_risk score).
    # On-chain heat (from _onchain_loop) is the only data that tells an explosive
    # low-cap apart from a rug with the same thin/concentrated book → feed it to the
    # forensic gate so verified buy-pressure can clear the liquidity/concentration block.
    _heat = int(_onchain_heat.get(f"{candidate.exchange}:{candidate.symbol}", {}).get("heat", 0))
    _onchain_ok = _heat >= FORENSIC_ONCHAIN_OVERRIDE_HEAT
    # PRECISION gate (pre-pump only) — the 3 levers combined to raise the 22% win-rate.
    if fsm_path:
        # (a-1) REGIME GATE — "si no estoy 100% seguro, me abstengo". El régimen
        # "unknown" (sin tendencia clasificable) midió profit_factor 0.092 = casi todo
        # pérdida. Sin contexto de mercado el detector no tiene edge → no opera. Solo
        # bloquea "unknown"; bull/bear/sideways pasan (se segmentan en analytics).
        if PREPUMP_SKIP_UNKNOWN_REGIME:
            _trend = (get_analytics().regime or "").split("/")[0]
            if _trend == "unknown":
                _record_learning(candidate.symbol, "skip_regime_unknown", "paper", candidate,
                                 f"régimen sin clasificar ({get_analytics().regime or 'n/a'}) — sin contexto, abstención")
                return False
        # (a0) SCORE FLOOR — rigidez: a sub-quality calificación never enters, even with
        # an on-chain lead. Cuts the measured junk (HYPER 36, ZKC 33) that bled the book.
        _pscore = int(getattr(candidate, "pump_score", 0) or 0)
        if _pscore < PREPUMP_MIN_SCORE:
            _record_learning(candidate.symbol, "skip_fsm_low_score", "paper", candidate,
                             f"score {_pscore} < {PREPUMP_MIN_SCORE:.0f} (calificación insuficiente)")
            return False
        # (a0b) PRICE FILTER — tesis criminal-pump = ¢→$1→$2. Un token ya caro tiene poco
        # espacio de multi-x; el universo pre-pump se enfoca en precio bajo.
        if PREPUMP_MAX_PRICE > 0 and candidate.last_price > PREPUMP_MAX_PRICE:
            _record_learning(candidate.symbol, "skip_price_high", "paper", candidate,
                             f"precio ${candidate.last_price:g} > ${PREPUMP_MAX_PRICE:g} (poco espacio multi-x)")
            return False
        # (a) CONFIRMED micro-breakout: don't buy a 100%-flat base (the MFE=+0.0%
        # churn). Require the first real push off the base, making a fresh high.
        # Solo piso de lift (no exige "nuevo máximo de ventana" — ese requisito de
        # timing mataba el 100% de candidatos). La confirmación real es el gate de
        # VOLUMEN (c) abajo: 4x = el movimiento arrancó de verdad (el edge medido).
        # onchain_lead waives the CEX-breakout requirement: strong on-chain BUYING
        # (DEX buys + CEX inflows, hours ahead of the CEX move) IS the confirmation
        # — that's the whole point of a LEADING entry, to buy DURING accumulation
        # BEFORE the price moves on the venue. Without this waiver the "early" door
        # routed back through the late gate and the bot could only ever enter post-breakout.
        # El VOLUMEN es la confirmación REAL de la ruptura (gate (c) abajo, 4x). Un
        # microcap en acumulación es PLANO por definición; exigir ADEMÁS +1.5% de precio
        # YA movido era un doble-gate que bloqueó 110 entradas (el #1 rechazo "Plano,
        # Sin Ruptura"). Si el volumen YA confirma el arranque, el precio-plano NO debe
        # doble-bloquear — el 4x ES la ruptura, el precio la sigue.
        # ENTRAR ANTES: el FSM confirmó la acumulación → ESA es la entrada. Sin gates de
        # breakout (eran la sobre-ingeniería que rechazaba el 98% de la tesis). Solo se veta
        # libro MUERTO (sin volumen vivo Y plano = ni acumulación es).
        _vol_now = accel if accel is not None else candidate.volume_spike
        _entry_vol = _vol_now
        if not onchain_lead and (_vol_now or 0) < FSM_CONFIRM_MIN_VOL and runup < FSM_MIN_BREAKOUT_PCT:
            _record_learning(candidate.symbol, "skip_dead_book", "paper", candidate,
                             f"libro muerto (vol {(_vol_now or 0):.1f}x < {FSM_CONFIRM_MIN_VOL:.1f}x y plano +{runup:.1f}%)")
            return False
        # Incluso un LEAD on-chain exige volumen MÍNIMO: un libro muerto (NIL @1.9x perdió,
        # FTT @0.9x perdió) no entra solo por heat. LAYER (ganador) tuvo 2.1x → el piso separa.
        if onchain_lead and _entry_vol is not None and _entry_vol < FSM_ONCHAIN_MIN_VOL_FLOOR:
            _record_learning(candidate.symbol, "skip_lead_no_volume", "paper", candidate,
                             f"lead on-chain pero volumen muerto ({_entry_vol:.1f}x < {FSM_ONCHAIN_MIN_VOL_FLOOR:.1f}x)")
            return False
        # (d) EDGESCORE GATE — entre los criminal-pumps ya protegidos, opera SOLO los
        # buckets con edge esperado real (MFE esperado, de outcomes propios). Cold-start
        # (n < EDGE_MIN_SAMPLES) NO gatea: deja operar pa acumular datos (mejora con el
        # tiempo). Un bucket que históricamente NO se mueve → no entra (ataca el 4% de
        # conversión: los duds dejan de drenar capital).
        if EDGE_GATE_ENABLED:
            _edge, _edge_n, _edge_dbg = _lab.edge_score(
                cluster=candidate.cluster, vol_spike=(_entry_vol or 0))
            if _edge_n >= EDGE_MIN_SAMPLES and _edge < EDGE_MIN_SCORE:
                _record_learning(candidate.symbol, "skip_low_edge", "paper", candidate,
                                 f"EdgeScore {_edge:.1f}% < {EDGE_MIN_SCORE:.1f}% esperado "
                                 f"(bucket {_edge_dbg['bucket']}, n={_edge_n})")
                return False
    # Differentiated learning (§4): a token already flagged dangerous (a prior scam/rug
    # tell) stays ACTIVELY avoided — redeemable only by strong on-chain buy pressure.
    if f"{candidate.exchange}:{candidate.symbol}" in _dangerous_signals and not _onchain_ok:
        _record_learning(candidate.symbol, "skip_dangerous", "paper", candidate,
                         "patrón peligroso previo (Dangerous_Signals) sin confirmación on-chain")
        return False
    # ANTI-RUG on-chain: si hay cobertura DEX y las VENTAS dominan el flujo (dump del
    # dev en curso), veta + marca peligroso. No bloquea CEX-only sin DEX (dex=None).
    _arg = _onchain_heat.get(f"{candidate.exchange}:{candidate.symbol}")
    _arg_dex = (_arg or {}).get("dex")
    if _arg_dex is not None:
        _br = _arg_dex.get("buy_ratio_h1")
        _flow = int(_arg_dex.get("buys_h1") or 0) + int(_arg_dex.get("sells_h1") or 0)
        if _br is not None and _flow >= ANTIRUG_MIN_FLOW and _br < ANTIRUG_MIN_BUY_RATIO:
            _mark_dangerous(candidate.exchange, candidate.symbol)
            _learn_dangerous(candidate, f"dump on-chain buy_ratio {_br:.2f}")
            _record_learning(candidate.symbol, "skip_dump_in_progress", "paper", candidate,
                             f"on-chain dumpeando: buy_ratio {_br:.2f} < {ANTIRUG_MIN_BUY_RATIO} ({_flow} txns/1h)")
            return False
    # HOLDER CONCENTRATION veto (rug-prone): un solo whale (>25%) o el top-10 (>70%) puede
    # dumpear todo encima. Dato on-chain ya calculado (holder_concentration); solo cuando
    # hay cobertura DEX (security se setea sólo si dex != None). Marca peligroso.
    if PREPUMP_VETO_CONCENTRATED:
        _sec = (_arg or {}).get("security")
        if _sec and _sec.get("concentrated"):
            _mark_dangerous(candidate.exchange, candidate.symbol)
            _learn_dangerous(candidate, f"holders concentrados top1 {_sec.get('top1_whale_pct', 0)}%")
            _record_learning(candidate.symbol, "skip_concentrated", "paper", candidate,
                             f"holders concentrados (top1 {_sec.get('top1_whale_pct', 0)}% / top10 {_sec.get('top10_pct', 0)}%) — rug-prone")
            return False
    # DATA-DRIVEN precision ceilings — SOLO tesis PREPUMP (microcaps acumulando).
    # GAINERS es momentum cap-agnóstico (volume>6x en cualquier token = el edge) →
    # entra con skip_gates=True y SALTA estos techos, usando SUS propios gates
    # (accel/chase/runup/floor). Lógica totalmente independiente, como debe ser.
    # (1) Liquidity ceiling — big-cap books never pumped; confirmed pumps are microcaps.
    if not skip_gates and candidate.liquidity_usd > ENTRY_MAX_LIQUIDITY_USD:
        _record_learning(candidate.symbol, "skip_too_liquid", "paper", candidate,
                         f"liquidez ${candidate.liquidity_usd:,.0f} > ${ENTRY_MAX_LIQUIDITY_USD:,.0f} (big-cap, no pumpea)")
        return False
    # (2) Imbalance ceiling — an extreme bid wall is a fake-wall / no-pump tell.
    if not skip_gates and candidate.orderbook_imbalance > ENTRY_MAX_IMBALANCE:
        _record_learning(candidate.symbol, "skip_fake_wall", "paper", candidate,
                         f"imbalance {candidate.orderbook_imbalance:.2f} > {ENTRY_MAX_IMBALANCE:.2f} (muro de bids falso)")
        return False
    # (3) MARKET-CAP ceiling — el techo de liquidez mide el LIBRO, no el tamaño real.
    # APE/AERO/STEEM tienen libro flaco en el venue pero son mid/big-caps ($100M+)
    # que NO pumpean. El market cap (CMC/CoinGecko) es el filtro microcap correcto.
    # Best-effort: solo bloquea si HAY dato y supera el techo (microcaps sin listing
    # quedan en None → pasan).
    if not skip_gates and ENTRY_MAX_MARKET_CAP_USD > 0:
        try:
            _mkt = await market_for_symbol(candidate.symbol.split("/")[0])
        except Exception:
            _mkt = None
        _mc = (_mkt or {}).get("market_cap_usd")
        if _mc and _mc > ENTRY_MAX_MARKET_CAP_USD:
            _record_learning(candidate.symbol, "skip_too_bigcap", "paper", candidate,
                             f"market cap ${_mc/1e6:,.0f}M > ${ENTRY_MAX_MARKET_CAP_USD/1e6:,.0f}M (no es microcap, no pumpea)")
            return False
    _book = book if book is not None else ("prepump" if fsm_path else "gainers")
    if fsm_path:
        _floor = PREPUMP_MIN_LIQUIDITY_USD
    elif _book == "gainers":
        _floor = GAINERS_MIN_LIQUIDITY_USD     # gainers tune their OWN liquidity floor
    else:
        _floor = None
    ok, reasons = forensic_check(
        spread_pct=candidate.spread_pct,
        liquidity_usd=candidate.liquidity_usd,
        top_book_share=candidate.top_book_share,
        min_liquidity_usd=_floor,
        onchain_heat=_heat,
    )
    if not ok:
        _record_learning(candidate.symbol, "forensic_block", "paper", candidate, "; ".join(reasons))
        # MANIPULATION_SUSPECT (thin + concentrated book) = a scam/rug tell → remember it.
        if any("MANIPULATION_SUSPECT" in r for r in reasons):
            _mark_dangerous(candidate.exchange, candidate.symbol)
            _learn_dangerous(candidate, "MANIPULATION_SUSPECT (libro flaco+concentrado)")
        await store.insert_bot_log(
            "PUMP_SCANNER", "INFO",
            f"ForensicFilter bloqueó {candidate.symbol}: {'; '.join(reasons)}",
        )
        logger.info("forensic block %s: %s", candidate.symbol, reasons)
        return False

    # EDGE-WEIGHTED position size: FIXED RISK base (risk a % of balance, sized so the
    # dynamic stop loses exactly that) SCALED by the setup×venue edge multiplier. The
    # multiplier (0.5–1.5×) was computed before but left in "simulation" — now APPLIED, so
    # capital flows to the measured edge (bitget up, binance/mexc down) instead of betting
    # equal everywhere. confidence_for is exchange-aware. Floor $10, never exceed balance.
    setup_type = setup_hint or ("accumulation" if fsm_path else ("velocity" if accel else "momentum"))
    _eng = get_analytics()
    confidence = _eng.confidence_for(setup_type, candidate.exchange)
    size_mult = _eng.sizing_multiplier(confidence) if EDGE_SIZING_ENABLED else 1.0
    risk_pct = float(os.getenv("PUMP_RISK_PER_TRADE_PCT", "1.0"))
    stop_pct = float(os.getenv("PUMP_DYNAMIC_STOP_PCT", "5.0"))
    balance = bot.balance()
    size = (balance * risk_pct / 100) / (stop_pct / 100) if (stop_pct > 0 and balance > 0) else bot.auto_entry_usd
    size = round(max(10.0, min(size * size_mult, balance or bot.auto_entry_usd)), 2)

    # Circuit-breaker inputs (capital protection): activan los gates daily-loss +
    # drawdown de risk.py que estaban en 0 = muertos. daily_loss = pérdida realizada
    # HOY (USD+ si vas abajo); drawdown = % bajo el pico de equity. Auto-recuperan:
    # daily resetea a medianoche UTC, drawdown al recobrar el pico.
    _today = datetime.now(UTC).date()
    _realized_today = 0.0
    for _e in bot.pm.history:
        try:
            if datetime.fromisoformat(_e.at).date() == _today:
                _realized_today += _e.pnl
        except Exception:
            continue
    _daily_loss = round(max(0.0, -_realized_today), 2)
    _eq_vals = [float(p.get("v") or 0) for p in bot.equity_history]
    _cur_eq = bot.balance()
    _peak_eq = max([*_eq_vals, _cur_eq]) if _eq_vals else _cur_eq
    _dd_pct = round((_peak_eq - _cur_eq) / _peak_eq * 100.0, 2) if _peak_eq > 0 else 0.0

    sw.mark("validation")  # gates + ForensicFilter + risk sizing done
    result = await bot.engine.act(
        symbol=candidate.symbol, side=Side.buy, reference_price=candidate.last_price,
        capital_usd=size, exchanges=[candidate.exchange],
        # Book-aware concurrency: prepump and gainers each get their OWN max-open
        # budget (the risk cap counts only same-book opens) so neither starves the other.
        open_trades=bot.open_count_book(_book),
        # Iceberg: liquidez del libro como proxy de profundidad. Si el tamaño
        # supera el 2% → entrada partida en 3 para no mover el precio (microcaps).
        book_depth_usd=candidate.liquidity_usd,
        daily_loss_usd=_daily_loss, current_drawdown_pct=_dd_pct,
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
    # setup_type / confidence / size_mult computed above (now APPLIED to live size,
    # edge-weighted by setup×venue). Recorded on the trade for the analytics ledger.
    opened_any = False
    book = _book
    # HONEST entry timing (mata la mentira del badge "PRE-PUMP"): lead = compró ANTES
    # del movimiento por presión on-chain; ruptura = el FSM esperó a que el arranque se
    # confirme (runup≥1.5% + vol≥4x) = entró EN la vela, NO antes; momentum = gainers.
    entry_phase = "lead" if onchain_lead else ("momentum" if _book == "gainers" else "ruptura")
    for fill in result.fills:
        bot.pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
            cluster=candidate.cluster, confidence=candidate.confidence_score,
            book=book, entry_phase=entry_phase, signal_at=candidate.updated_at,
        )
        opened = bot.pm.positions.get(bot.pm.key(fill.exchange, fill.symbol))
        if opened:
            opened_any = True
            # Persist the P&L bucket + entry phase to LOCAL bot_state (SQLite, REST-safe
            # → no Supabase column needed) so the Gainers tab + honest badge survive a restart.
            store.enqueue(lambda k=f"book:{bot.uid}:{fill.exchange}:{fill.symbol}", v=book: store.set_state(k, v))
            store.enqueue(lambda k=f"phase:{bot.uid}:{fill.exchange}:{fill.symbol}", v=entry_phase: store.set_state(k, v))
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
                "sizing_mode": "live" if EDGE_SIZING_ENABLED else "fixed", "sizing_multiplier": size_mult,
                "theoretical_size": round(size, 2), "user_id": bot.uid,
            })
            await _persist_position(bot, opened)
            # Forensics (Fase 7/8): captura el contexto de ENTRADA del trade.
            if _forensics is not None:
                try:
                    await asyncio.to_thread(_forensics.record_entry, bot.uid, candidate, opened, accel)
                except Exception:
                    logger.exception("forensics record_entry failed")
        _record_learning(candidate.symbol, "auto_entry", "paper", candidate, f"bought ${size:.0f} @ {fill.fill_price}")
        decision_log.write("BUY", candidate.symbol, exchange=fill.exchange,
                           reason=f"setup={setup_type} vol={(accel if accel is not None else candidate.volume_spike) or 0:.1f}x score={candidate.pump_score}",
                           usd=round(size, 2), price=fill.fill_price,
                           slices=getattr(fill, "slices", 1),
                           liquidity_usd=round(candidate.liquidity_usd, 0),
                           book=_book)
        await store.insert_bot_log(
            "PUMP_SCANNER", "TRADE_BUY",
            f"Auto-entry {candidate.symbol} ${size:.0f} @ {fill.fill_price}",
            volumen=candidate.volume_spike,
        )
        # Phase tag so the notification IS the investment + shows if it was early.
        if onchain_lead:
            _phase, _ptier = "🟢 PRE-PUMP · líder on-chain (antes del pump)", "onchain"
        elif fsm_path:
            _phase, _ptier = "🟡 PRE-PUMP · ruptura confirmada", "inminente"
        elif _book == "gainers":
            _phase, _ptier = "🔵 GAINERS · momentum", "ahora"
        else:
            _phase, _ptier = "MOMENTUM", "ahora"
        # Honest empirical P(sube) for this setup's bucket, shown on the investment.
        _ep, _en = _lab.pump_probability(
            cluster=candidate.cluster, tier=_ptier, vol_spike=candidate.volume_spike)
        await notify.send_entry(notify.format_entry(
            symbol=candidate.symbol, exchange=candidate.exchange, price=fill.fill_price,
            accel=accel if accel is not None else candidate.volume_spike,
            score=candidate.pump_score, classification=candidate.classification,
            flags=candidate.flags,
            dump_pct=float(os.getenv("PUMP_DUMP_TICK_PCT", "10")),
            timeout_min=float(os.getenv("PUMP_TIMEOUT_MINUTES", "8")),
            trail_arm=TRAIL_ARM_PCT, trail_give=TRAIL_GIVEBACK_PCT,
            confidence=candidate.confidence_score, setup=_phase,
            prob=_ep, prob_n=_en,
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


# --- UNIVERSO de detección (caza de criminal pumps) -------------------------------
# El radar (_candidates) ve TODO, pero solo los tokens con capacidad real de pump
# vertical entran al embudo FSM. Un criminal pump es un MICROCAP de precio BAJO con
# libro tradeable-pero-fino. Filtra aquí (admisión a la FSM) → EUL/big-caps planos ni
# se vigilan, y las alertas/compras se concentran en lo pumpeable. Todo tunable; el
# forensic en la entrada sigue siendo la última red.
UNIVERSE_FILTER = os.getenv("PUMP_UNIVERSE_FILTER", "true").lower() == "true"
UNIVERSE_MAX_PRICE = float(os.getenv("PUMP_UNIVERSE_MAX_PRICE", "1.0"))     # sub-$1 (tesis ¢→$1→$2)
UNIVERSE_MIN_LIQ = float(os.getenv("PUMP_UNIVERSE_MIN_LIQ", "20000"))       # no un libro fantasma
UNIVERSE_MAX_LIQ = float(os.getenv("PUMP_UNIVERSE_MAX_LIQ", "2000000"))     # no un big-cap (no pumpea)


def _is_pumpable_universe(c) -> bool:
    """¿Este token PUEDE hacer un criminal pump? (precio bajo + libro microcap). Best-
    effort: si falta liquidez (0/None) no bloquea por liquidez — el price sigue gateando
    y el forensic de entrada atrapa el libro fino igual."""
    if not UNIVERSE_FILTER:
        return True
    if UNIVERSE_MAX_PRICE > 0 and (c.last_price or 0) > UNIVERSE_MAX_PRICE:
        return False
    liq = c.liquidity_usd or 0
    if liq and (liq < UNIVERSE_MIN_LIQ or liq > UNIVERSE_MAX_LIQ):
        return False
    return True


async def _perform_scan(min_pump_score: int = 1, full: bool = False) -> ScanResponse:
    global _last_scan_at, _last_reeval
    scanned = await scan_markets(_scan_exchanges(), min_pump_score=min_pump_score, full=full)
    _prev_keys = set(_candidates.keys())   # what we held BEFORE this re-evaluation
    _candidates.clear()
    if len(_candidate_market) > 2000:
        _candidate_market.clear()   # cap simple: se repuebla en este mismo scan
    for item in scanned:
        candidate = _to_candidate(item)
        _candidates[f"{candidate.exchange}:{candidate.symbol}"] = candidate
        _candidate_market[f"{candidate.exchange.lower()}:{candidate.symbol.upper()}"] = {
            "score": candidate.pump_score, "cluster": candidate.cluster,
            "delta_24h": candidate.price_change_pct_24h, "spark": candidate.spark}
        # Momentum scan-path entry ELIMINADO (criminal-pump only). El scan solo mantiene
        # _candidates (radar interno) y alimenta el FSM. Las alertas + entradas salen
        # EXCLUSIVAMENTE del FSM de acumulación (_emit_signal_alert / velocity_ruptura).
        # NUNCA se compra un breakout perseguido (era el path "momentum" grade F).
    # Cross-exchange arbitrage detection (alert-only in paper).
    try:
        await _arbitrage_scan()
    except Exception:
        logger.exception("arbitrage scan failed")
    _last_scan_at = datetime.now(UTC)
    # Re-evaluation summary: diff the rebuilt set against the previous pass so the
    # discard is auditable (the bot re-checks EVERY scan and drops non-qualifiers).
    _new_keys = set(_candidates.keys())
    _discarded = _prev_keys - _new_keys
    _last_reeval = {
        "at": _last_scan_at.isoformat(),
        "scanned": len(scanned),
        "kept": len(_prev_keys & _new_keys),
        "discarded": len(_discarded),
        "new": len(_new_keys - _prev_keys),
        "active": len(_new_keys),
    }
    if _prev_keys:
        logger.info("re-eval: %d kept · %d descartados (ya no califican) · %d nuevos · %d activos",
                    _last_reeval["kept"], _last_reeval["discarded"], _last_reeval["new"], _last_reeval["active"])
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
            if not _is_pumpable_universe(c):
                continue          # no es microcap pumpeable → fuera del embudo de detección
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
        v = bot.balance()
        # Guard anti-glitch: bajo un stop de drawdown ~10% el equity no cae a la mitad
        # entre escaneos. Un punto <=0 o <50% del último es un error de cálculo (lo que
        # metía $100 sueltos al chart) → no se persiste. El lado alto (ganancias) libre.
        _last = bot.equity_history[-1]["v"] if bot.equity_history else v
        if v <= 0 or (_last > 0 and v < _last * 0.5):
            continue
        point = {"t": _last_scan_at.isoformat(), "v": v}
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
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,           # Update (cada 3 min)
        "discover_interval_seconds": DISCOVER_INTERVAL_SECONDS,   # Discover (1×/día)
        "last_discover_at": _last_discover_at.isoformat() if _last_discover_at else None,
        "exchanges": _scan_exchanges(),
        "last_scan_at": _last_scan_at.isoformat() if _last_scan_at else None,
        "candidate_count": len(_candidates),
        "reeval": _last_reeval,
        "kill_switch_active": bot.guard.kill_switch,
        "kill_switch_reason": bot.guard.kill_reason,   # manual / "auto: rate-limit storm" / "auto: caída de volumen"
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


def _alert_age_ago(exchange: str, symbol: str) -> str | None:
    """Real 'time since alerted' from the alert-emission map. The candidate's
    updated_at is refreshed every scan, so _ago(updated_at) always read 'ahora' for
    EVERY alert — this reads the actual moment the signal fired instead."""
    suffix = f":{exchange}:{symbol}"
    epochs = [v for k, v in _signal_alert_at.items() if k.endswith(suffix)]
    if not epochs:
        return None
    return _ago(datetime.fromtimestamp(max(epochs), tz=UTC))


def _recent_fired_alerts(limit: int = 6) -> list[dict]:
    """The alerts the bot ACTUALLY fired (same source that feeds Telegram), newest
    first. Reads the alert-emission map `_signal_alert_at` instead of the transient
    `waiting_confirmation` snapshot — the old source was almost always empty, so the
    in-app 'Alertas' panel read 'Sin alertas todavía' while Telegram kept notifying.
    Now the panel mirrors Telegram. Enriches with live market cluster/score."""
    by_sym: dict[tuple[str, str], float] = {}
    for k, ts in _signal_alert_at.items():
        parts = k.split(":")
        if len(parts) < 3:
            continue
        ex, sym = parts[1], parts[2]
        cur = by_sym.get((ex, sym))
        if cur is None or ts > cur:
            by_sym[(ex, sym)] = ts
    rows = sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out: list[dict] = []
    for (ex, sym), ts in rows:
        mk = _candidate_market.get(f"{ex.lower()}:{sym.upper()}")
        out.append({
            "symbol": sym,
            "cluster": (mk["cluster"] if mk else None) or "n/a",
            "score": (mk["score"] if mk else None) or 0,
            "ago": _ago(datetime.fromtimestamp(ts, tz=UTC)),
        })
    return out


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
            # Vista unificada: enriquece cada candidato pre-pump (FSM: estado/acc/pers/rug)
            # con su data de MERCADO (score/cluster/Δ24h/spark) del radar, por exchange:symbol.
            # Sin match → quedan en None (la UI muestra "—"). Así una sola tabla rica reemplaza
            # las dos vistas (pre-estallido + Mercado Live).
            for r in prepump:
                mk = _candidate_market.get(
                    f"{str(r.get('exchange','')).lower()}:{str(r.get('symbol','')).upper()}")
                r["score"] = mk["score"] if mk else None
                r["cluster"] = mk["cluster"] if mk else None
                r["delta_24h"] = mk["delta_24h"] if mk else None
                r["spark"] = mk["spark"] if mk else []
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
        "books": bot.book_split(),
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
        "latest_alerts": _recent_fired_alerts(6),
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
async def run_scan(min_pump_score: int = 1, full: bool = False) -> ScanResponse:
    """full=False = Update (chequeo de oportunidad ahora) · full=True = Discover
    (barrido COMPLETO de todos los tokens, arma el universo). El botón Discover manda
    full=true; Update manda full=false."""
    global _last_discover_at
    res = await _perform_scan(min_pump_score=min_pump_score, full=full)
    if full:
        _last_discover_at = datetime.now(UTC)
    return res


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
            cluster=candidate.cluster, confidence=candidate.confidence_score, signal_at=candidate.updated_at,
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
            "book": getattr(p, "book", "prepump"),   # prepump (FSM) | gainers (velocity)
            "entry_phase": getattr(p, "entry_phase", "ruptura"),  # lead | ruptura | momentum (badge honesto)
            "confidence": round(p.confidence, 0),
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


class CloseRequest(BaseModel):
    symbol: str | None = None     # "SYM" o "SYM/USDT"; vacío o "all" = cerrar TODAS las abiertas
    exchange: str | None = None   # opcional, para desambiguar el mismo símbolo en 2 venues


@app.post("/managed/close")
async def close_managed(request: Request, req: CloseRequest) -> dict:
    """Cierre MANUAL de posición(es) paper. Vende a último precio (≈flat) y corre el
    MISMO bookkeeping que una salida normal (store + forensics + pipeline.mark_closed),
    así no revive al reiniciar. symbol vacío / 'all' = cierra todas las abiertas."""
    bot = _req_bot(request)
    want_all = (not req.symbol) or req.symbol.lower() in ("all", "*", "todas")
    want_sym = (req.symbol or "").upper().split("/")[0]
    targets = []
    for key, p in list(bot.pm.positions.items()):
        if p.closed:
            continue
        if not want_all:
            if p.symbol.upper().split("/")[0] != want_sym:
                continue
            if req.exchange and p.exchange.lower() != req.exchange.lower():
                continue
        targets.append((key, p))
    closed = []
    for key, p in targets:
        for ev in bot.pm.reap(key, reason="manual_close"):
            await _handle_exit(bot, p, ev)
            closed.append({"symbol": p.symbol, "exchange": p.exchange,
                           "price": ev.price, "pnl": round(ev.pnl, 4)})
    return {"closed": closed, "count": len(closed)}


@app.get("/entry-rejections")
async def entry_rejections() -> dict:
    """Por qué el bot NO compró — prueba viva de que los filtros (anti-top,
    anti-chase, confianza, forensic) rechazan de verdad. Lee el log de aprendizaje
    en memoria (skip_* + forensic_block). Cada 'skip_parabolic' = un tope evitado."""
    # Etiqueta humana + tono por filtro (tone: danger=rug/peligro, warn=volumen,
    # info=estructura/timing, good=tope evitado, muted=calidad). Nada de snake_case
    # de cara al usuario. Cualquier filtro no mapeado cae a un label legible genérico.
    LABELS = {
        "skip_fsm_flat":          ("Plano, Sin Ruptura",          "info"),
        "skip_fsm_low_score":     ("Calificación Baja",           "muted"),
        "skip_price_high":        ("Precio Muy Alto",             "muted"),
        "skip_concentrated":      ("Holders Concentrados",        "danger"),
        "skip_lead_no_volume":    ("Volumen Muerto (Lead)",       "warn"),
        "skip_fsm_no_volume":     ("Sin Volumen Que Confirme",    "warn"),
        "skip_fsm_onchain_weak":  ("On-Chain Débil",              "muted"),
        "skip_regime_unknown":    ("Régimen Sin Contexto",        "muted"),
        "skip_low_edge":          ("Edge Esperado Bajo",          "muted"),
        "skip_parabolic":         ("Tope Parabólico Evitado",     "good"),
        "skip_exhausted":         ("Ya Corrido 24h",              "good"),
        "skip_no_breakout":       ("Sin Ruptura Alcista",         "info"),
        "skip_no_volume_break":   ("Ruptura Sin Volumen",         "warn"),
        "skip_low_confidence":    ("Confianza Baja",              "muted"),
        "skip_low_volume":        ("Volumen Bajo",                "warn"),
        "skip_too_liquid":        ("Demasiado Líquido (Big-Cap)", "muted"),
        "skip_too_bigcap":        ("No Es Microcap",              "muted"),
        "skip_fake_wall":         ("Muro De Bids Falso",          "danger"),
        "skip_dangerous":         ("Patrón Peligroso (Rug)",      "danger"),
        "skip_dump_in_progress":  ("Dumpeando On-Chain",          "danger"),
        "skip_gainers_full":      ("Gainers Llenos",              "muted"),
        "skip_gainers_slow":      ("Gainers Sin Aceleración",     "muted"),
        "skip_gainers_chase":     ("Gainers Ya Corrido",          "good"),
        "skip_gainers_ran":       ("Gainers Ya Despegó",          "good"),
        "forensic_block":         ("Libro Malo (Forensic)",       "danger"),
    }

    def _label(action: str) -> tuple[str, str]:
        if action in LABELS:
            return LABELS[action]
        return (action.replace("skip_", "").replace("_", " ").title(), "muted")

    skips = [r for r in _learning
             if r.action.startswith("skip_") or r.action == "forensic_block"]
    agg: dict[str, dict] = {}
    for r in skips:
        lbl, tone = _label(r.action)
        slot = agg.setdefault(r.action, {"label": lbl, "tone": tone, "count": 0})
        slot["count"] += 1
    by_reason = sorted(
        ({"key": k, **v} for k, v in agg.items()),
        key=lambda x: x["count"], reverse=True)
    recent = []
    for r in skips[-25:][::-1]:
        lbl, tone = _label(r.action)
        det = (r.detail or "").strip()
        det = det[:1].upper() + det[1:] if det else ""
        recent.append({"t": r.created_at.isoformat(), "symbol": r.symbol,
                       "label": lbl, "tone": tone, "detail": det})
    return {"total": len(skips), "by_reason": by_reason, "recent": recent}


@app.get("/learning/buckets")
async def learning_buckets() -> dict:
    """Esquema de aprendizaje diferenciado (Lead-Architect §4). NO se borra nada —
    cada decisión queda; aquí se reparten en 3 categorías honestas leídas de los
    records existentes (sin columnas nuevas → seguro para Supabase):
      successful = trade ejecutado (pasó todos los filtros)
      dangerous  = bloqueado por señal de estafa/rug (libro flaco+concentrado)
      failed     = no pudo entrar por causa benigna (liquidez, sin ruptura, débil)
    Los dangerous se EVITAN activamente después (set persistente)."""
    buckets: dict[str, list[dict]] = {"successful": [], "dangerous": [], "failed": []}
    for r in _learning:
        b = _learning_bucket(r.action, r.detail)
        buckets[b].append({"t": r.created_at.isoformat(), "symbol": r.symbol,
                           "action": r.action, "detail": r.detail})
    return {
        "counts": {k: len(v) for k, v in buckets.items()},
        "dangerous_active": sorted(_dangerous_signals),
        "successful": buckets["successful"][-20:][::-1],
        "dangerous": buckets["dangerous"][-20:][::-1],
        "failed": buckets["failed"][-20:][::-1],
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
            {"param": "Trailing (ratchet del pico)",
             "value": f"asegura {100 - envf('PUMP_TRAIL_GIVEBACK_PCT','10'):g}% · arma +{envf('PUMP_TRAIL_ARM_PCT','0.8'):g}% · piso {envf('PUMP_TRAIL_MIN_PCT','0.8'):g}%",
             "auto": False, "how": "ratchet fijo: devuelve solo giveback% del pico (10% en runs grandes), nunca baja"},
            {"param": "Hard stop (pérdida máx)", "value": f"{envf('PUMP_STOP_LOSS_PCT','8')}%",
             "auto": True, "how": "24h desde forensics"},
            {"param": "Timeout (corte por tiempo)", "value": f"{envf('PUMP_TIMEOUT_MINUTES','8')} min",
             "auto": True, "how": "24h desde lead-time del aprendizaje"},
            {"param": "Break-even (respaldo del trailing)", "value": f"{envf('PUMP_BREAKEVEN_PCT','4')}%",
             "auto": True, "how": "24h ~40% del win típico · banda [2.5,6] (P3) · secundario: el trailing arma antes"},
            {"param": "Pesos del scoring (señales)", "value": _learned_weights_str(),
             "auto": True, "how": "24h desde el lift confirmado-vs-dud del aprendizaje · banda [0.7,1.3] (P5)"},
            {"param": "Filtro de alertas (prob mínima)", "value": f"{round(ALERT_MIN_PROBABILITY*100)}%",
             "auto": True, "how": "según la precisión medida del learning · baja precisión sube el piso · banda [25,70]%"},
            {"param": "Dump detector (caída de 1 tick)", "value": f"{envf('PUMP_DUMP_TICK_PCT','10')}%",
             "auto": False, "how": "fijo"},
            {"param": "Tamaño por operación",
             "value": f"riesgo {envf('PUMP_RISK_PER_TRADE_PCT','1.0'):g}%/trade",
             "auto": False, "how": f"por riesgo: tamaño = riesgo$ ÷ stop {envf('PUMP_DYNAMIC_STOP_PCT','5.0'):g}% (NO fijo) · tope = balance"},
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
    st = _pipeline.status()
    # COMPRADO honesto: el conteo 'entry' del funnel = posiciones REALMENTE abiertas,
    # no las filas FSM 'entry' (que pueden quedar fantasma). reconcile_entries ya las
    # limpia en el loop; esto garantiza coherencia inmediata en la UI.
    real_open = sum(1 for bot in all_bots() for p in bot.pm.positions.values() if not p.closed)
    st.setdefault("states", {})["entry"] = real_open
    return {"enabled": True, **st}


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
    max_open_trades: int | None = Field(default=None, ge=0)   # 0 = sin límite
    # --- Ajustes por motor (live) ---
    gainers_accel: float | None = Field(default=None, ge=1, le=20)       # disparo gainers ×volumen
    pump_breakout_pct: float | None = Field(default=None, ge=0, le=20)   # ruptura mín. prepump (FSM)
    pump_vol_spike: float | None = Field(default=None, ge=1, le=20)      # volumen mín. prepump (FSM)


def _settings_payload(bot: UserBot, role: str = "operator") -> dict:
    return {
        # Shared brain (read-only for operators; only admin can tune it).
        "confirmation_threshold": round(_adaptive_threshold, 1),
        "threshold_editable": role == "admin",
        # Per-user trading preferences.
        "auto_entry": bot.auto_entry,
        "auto_entry_usd": bot.auto_entry_usd,
        "max_open_trades": bot.guard.limits.max_open_trades,   # 0 = sin límite
        "velocity_autoentry": VELOCITY_AUTOENTRY,              # gainers/momentum engine on/off
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "velocity_accel_factor": _velocity.status().get("accel_factor"),
        # --- Ajustes por motor (live-editables) ---
        "gainers_accel": _velocity.status().get("accel_factor"),  # gainers: disparo ×volumen
        "pump_breakout_pct": FSM_MIN_BREAKOUT_PCT,                # prepump: ruptura mín. %
        "pump_vol_spike": FSM_MIN_ENTRY_VOL_SPIKE,               # prepump: volumen mín. ×
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
    global _adaptive_threshold, FSM_MIN_BREAKOUT_PCT, FSM_MIN_ENTRY_VOL_SPIKE
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
    if req.max_open_trades is not None:
        # Per-user concurrency cap (0 = sin límite). Session-level — resets to the
        # PUMP_MAX_OPEN_TRADES env default on restart.
        bot.guard.limits.max_open_trades = int(req.max_open_trades)
    # --- Ajustes por motor (live; mutan el global del módulo + os.environ Y se
    # PERSISTEN al store para que sobrevivan un reinicio — antes eran session-level
    # y tu calibración se perdía en cada redeploy). ---
    if req.gainers_accel is not None:
        from . import velocity as _vel
        _vel.ACCEL_FACTOR = float(req.gainers_accel)           # acelerador ruptura FSM: en vivo
        os.environ["PUMP_VELOCITY_ACCEL_FACTOR"] = str(req.gainers_accel)
        store.enqueue(lambda v=float(req.gainers_accel): store.set_state("velocity_accel_factor", str(v)))
    if req.pump_breakout_pct is not None:
        FSM_MIN_BREAKOUT_PCT = float(req.pump_breakout_pct)     # prepump: ruptura mín.
        os.environ["PUMP_FSM_MIN_BREAKOUT_PCT"] = str(req.pump_breakout_pct)
        store.enqueue(lambda v=float(req.pump_breakout_pct): store.set_state("fsm_min_breakout_pct", str(v)))
    if req.pump_vol_spike is not None:
        FSM_MIN_ENTRY_VOL_SPIKE = float(req.pump_vol_spike)     # prepump: volumen mín.
        os.environ["PUMP_FSM_MIN_ENTRY_VOL_SPIKE"] = str(req.pump_vol_spike)
        store.enqueue(lambda v=float(req.pump_vol_spike): store.set_state("fsm_min_entry_vol_spike", str(v)))
    return _settings_payload(bot, user.get("role", "operator"))


@app.get("/pnl/breakdown")
async def pnl_breakdown(request: Request) -> dict:
    """Per-token P&L over the last 7d: realized exits + open unrealized, so the
    PNL 7D widget can show which tokens are winning/losing. All from real managed
    positions — nothing invented."""
    bot = _req_bot(request)
    cutoff = datetime.now(UTC).timestamp() - 7 * 86400
    by: dict[str, dict] = {}

    def _row(book: str, exchange: str, symbol: str) -> dict:
        k = f"{book}:{exchange}:{symbol}"
        return by.setdefault(k, {
            "symbol": symbol, "exchange": exchange, "book": book,
            "realized": 0.0, "unrealized": 0.0, "trades": 0, "open": False,
        })

    for e in bot.pm.history:
        try:
            ts = datetime.fromisoformat(e.at).timestamp()
        except Exception:
            ts = cutoff
        if ts < cutoff:
            continue
        d = _row(getattr(e, "book", "prepump") or "prepump", e.exchange, e.symbol)
        d["realized"] += e.pnl
        d["trades"] += 1
    for p in list(bot.pm.positions.values()):
        if p.closed or p.last_price <= 0:
            continue
        d = _row(getattr(p, "book", "prepump") or "prepump", p.exchange, p.symbol)
        d["unrealized"] += (p.last_price - p.entry_price) * p.qty
        d["open"] = True

    rows = []
    for d in by.values():
        d["total"] = round(d["realized"] + d["unrealized"], 2)
        d["realized"] = round(d["realized"], 2)
        d["unrealized"] = round(d["unrealized"], 2)
        rows.append(d)
    rows.sort(key=lambda r: r["total"], reverse=True)

    def _book_sum(bk: str) -> dict:
        sub = [r for r in rows if r["book"] == bk]
        return {
            "n": len(sub),
            "total": round(sum(r["total"] for r in sub), 2),
            "winners": sum(1 for r in sub if r["total"] > 0),
            "losers": sum(1 for r in sub if r["total"] < 0),
        }

    return {
        "rows": rows,
        "winners": sum(1 for r in rows if r["total"] > 0),
        "losers": sum(1 for r in rows if r["total"] < 0),
        "total": round(sum(r["total"] for r in rows), 2),
        "pnl_7d": bot.pnl_7d(),
        # P&L separado por motor (gainers vs scampump/pre-pump) para el panel.
        "by_book": {"gainers": _book_sum("gainers"), "prepump": _book_sum("prepump")},
    }


@app.get("/learning")
async def learning_snapshot() -> dict:
    """Feedback-loop analytics: did alerts fire before the pump, precision/recall,
    lead time, component contributions, and threshold proposals."""
    return _lab.snapshot()


@app.get("/edge-score")
async def edge_score_matrix() -> dict:
    """EdgeScore por bucket (cluster × volumen): MFE esperado de cada bucket desde los
    outcomes propios del bot. Es el ranker de rentabilidad que gatea la entrada — un
    bucket con edge esperado < EDGE_MIN_SCORE no opera (cuando ya hay datos suficientes)."""
    reps = {"<3x": 1.0, "3-6x": 4.0, ">=6x": 7.0}
    matrix: dict[str, dict] = {}
    for cl in ("classic", "long_pump", "accumulation"):
        row = {}
        for label, v in reps.items():
            e, n, dbg = _lab.edge_score(cluster=cl, vol_spike=v)
            row[label] = {"edge_pct": e, "n": n, "bucket": dbg["bucket"],
                          "gates": (n >= EDGE_MIN_SAMPLES and e < EDGE_MIN_SCORE)}
        matrix[cl] = row
    return {"enabled": EDGE_GATE_ENABLED, "min_score": EDGE_MIN_SCORE,
            "min_samples": EDGE_MIN_SAMPLES, "matrix": matrix}


@app.get("/notifications")
async def notifications_feed(since: int = 0) -> dict:
    """Feed de notificaciones IN-APP (paridad con Telegram). El dashboard hace poll
    con ?since=<último id visto> → trae solo las nuevas + el último id + total para
    la campana. Misma fuente que Telegram (notify.push_feed), nunca divergen."""
    return notify.feed_since(since)


@app.get("/alerts/performance")
async def alerts_performance() -> dict:
    """Early-detection PROOF: for each alert the bot fired, how far the token ran
    SINCE the alert (peak) and where it sits now. This is the 'lo detectó antes'
    evidence (like the source video showed for Collect/Pros/Play). Persisted, so it
    survives restarts. Reads real stored prices — never fabricated."""
    try:
        rows = await store.list_learning_outcomes(limit=200)
    except Exception:
        rows = []
    out = []
    for r in rows:
        ap = float(r.get("alert_price") or 0)
        if ap <= 0:
            continue
        peak = float(r.get("peak_price") or ap)
        last = float(r.get("last_price") or ap)
        out.append({
            "symbol": (r.get("symbol") or "").split("/")[0],
            "exchange": r.get("exchange") or "",
            "pump_score": r.get("pump_score") or 0,
            "cluster": r.get("cluster") or "n/a",
            "classification": r.get("classification") or "",
            "alert_price": ap,
            "last_price": last,
            "peak_price": peak,
            "move_pct": round((peak - ap) / ap * 100, 1),
            "now_pct": round((last - ap) / ap * 100, 1),
            "label": r.get("label") or "pending",
            "alert_at": r.get("alert_at"),
        })
    out.sort(key=lambda x: x.get("alert_at") or "", reverse=True)
    return {"alerts": out[:40]}


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
async def reset_my_bot(request: Request, hard: bool = False) -> dict:
    """Reset the logged-in user's OWN bot. Does not touch any other account.

    soft (default): close every open position (freeing the capital) and clear the
      in-memory equity curve. Realized P&L (carry) is KEPT.
    hard (?hard=true): also ZERO realized P&L so the paper balance returns to the
      base capital ($PUMP_PAPER_BALANCE) and the new strategy is measured from $0.
      Durably wipes THIS user's paper records (positions/exits/equity) in the DB so
      a restart doesn't resurrect the old P&L. Learning is KEPT."""
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
    if not hard:
        return {"ok": True, "closed": closed, "hard": False}
    # HARD: zero realized P&L in memory + durable wipe so balance = base capital.
    bot.pm.history.clear()
    bot.realized_carry = 0.0
    bot.carry_exits = []
    wiped = await store.wipe_paper(bot.uid)
    return {"ok": True, "closed": closed, "hard": True,
            "balance": bot.balance(), **wiped}

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
import sys
import traceback
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

from . import auth as auth_mod
from .auth import (
    COOKIE, LOGIN_HTML, MAX_AGE, auth_enabled, authenticate, make_token, read_token,
)

from . import grid_sync, store
from .account import real_balances
from .dashboard import DASHBOARD_HTML
from .executor import ExecMode, ExecutionEngine, Side, current_mode
from .grid import GridBot, backtest, fetch_1m_volume, fetch_ohlcv_for, fetch_price
from .grvt_proxy import register_grvt_proxy
from .market import market_for_symbol
from . import notify
from .notify import format_alert, send_telegram
from .position_manager import (
    BREAKEVEN_PCT, DUMP_TICK_PCT, TIMEOUT_MINUTES, ManagedPosition, PositionManager,
)
from .risk import RiskGuard
from .scanner import ScannedCandidate, fetch_token_detail, forensic_check, scan_markets
from .velocity import VelocityWatcher, watch_list_from_scores
from .learning import LearningLab
from .user_bot import PAPER_BALANCE, UserBot, all_bots, default_allocation, ensure_bots, get_bot

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
    spread_pct: float = 0.0
    top_book_share: float = 0.0
    manipulation_suspect: bool = False
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
# Adaptive confirmation threshold — the learning loop lowers it after late
# entries (be more sensitive to early moves) and raises it after false starts.
_adaptive_threshold = float(WAITING_CONFIRMATION_THRESHOLD)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    tasks = [
        asyncio.create_task(_auto_scan_loop()),
        asyncio.create_task(_grid_tick_loop()),
        asyncio.create_task(_monitor_loop()),
        asyncio.create_task(_velocity_loop()),
        asyncio.create_task(_account_loop()),
        asyncio.create_task(_grid_sync_loop()),
        asyncio.create_task(_daily_discover_loop()),
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
        await store.close()


async def _auto_scan_loop() -> None:
    """Run a scan on startup, then every SCAN_INTERVAL_SECONDS, forever."""
    while True:
        try:
            await _perform_scan()
            logger.info("auto-scan done: %d candidates", len(_candidates))
        except Exception as exc:
            logger.exception("auto-scan failed")
            await notify.send_error("Scan loop", repr(exc))
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _grid_tick_loop() -> None:
    """When the grid is running, fetch a live price and advance the grid."""
    while True:
        try:
            if _grid.running and _grid.grid:
                price = await fetch_price(_grid.pair)
                if price > 0:
                    _grid.step(price)
        except Exception as exc:
            logger.exception("grid tick failed")
            await notify.send_error("Grid tick", repr(exc))
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
    every user's bot (each account's positions are isolated)."""
    while True:
        try:
            # One 1m-volume read per distinct symbol per pass (feeds the
            # volume-aware time-stop). Cached so N users holding the same symbol
            # don't refetch it. Symbol volume is global, not per-user.
            vol_cache: dict[str, float] = {}
            for bot in all_bots():
                for key, pos in list(bot.pm.positions.items()):
                    if pos.closed:
                        continue
                    price = await fetch_price(pos.symbol, pos.exchange)
                    if price <= 0:
                        continue
                    vkey = f"{pos.exchange}:{pos.symbol}"
                    if vkey not in vol_cache:
                        vol_cache[vkey] = await fetch_1m_volume(pos.symbol, pos.exchange)
                    vol = vol_cache[vkey] or None
                    for event in bot.pm.step(key, price, volume=vol):
                        await _handle_exit(bot, pos, event)
            # Learning lab: track each alerted token's MFE/MAE/lead time vs live
            # price so we can tell whether alerts fire BEFORE the pump.
            for exch, sym in _lab.active_symbols():
                price = await fetch_price(sym, exch)
                if price > 0:
                    _lab.step(exch, sym, price)
            _lab.settle_due()
        except Exception as exc:
            logger.exception("monitor loop failed")
            await notify.send_error("Monitor loop (exits)", repr(exc))
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
                if current_mode() != ExecMode.paper:
                    continue
                candidate.last_price = t.price  # fire at the fresh trigger price
                _record_learning(
                    candidate.symbol, "velocity_trigger", "paper", candidate,
                    f"vol accel {t.accel:.1f}x @ {t.price}",
                )
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
    if total:
        logger.info("restored %d open positions across %d bots", total, len(all_bots()))
        await notify.send_system(f"🔄 <b>Estado recuperado</b> · {total} posiciones abiertas reconstruidas")


async def _handle_exit(bot: UserBot, pos, event) -> None:
    pct = round(event.fraction * 100)
    _record_learning_raw(
        pos.symbol, f"exit_{event.reason}", "paper", pos.pump_score, pos.classification,
        f"sold {pct}% @ {event.price} pnl {event.pnl:+.2f}",
    )
    await store.insert_exit({**event.__dict__, "user_id": bot.uid})
    await _persist_position(bot, pos)
    await store.insert_bot_log(
        "PUMP_SCANNER",
        "PANIC_SELL" if event.reason in ("dump", "hard_stop") else "TRADE_SELL",
        f"{event.reason} {pos.symbol} sold {pct}% @ {event.price}",
        pnl=event.pnl,
    )
    if event.closed:
        # Full close → close card with overall PnL%.
        cost = pos.entry_price * pos.initial_qty
        pnl_pct = (pos.realized_pnl / cost * 100) if cost > 0 else 0.0
        quality = bot.pm.entry_quality(pos)
        _record_learning_raw(
            pos.symbol, "trade_closed", "paper", pos.pump_score, pos.classification,
            f"realized {pos.realized_pnl:+.2f} · entry {quality}",
        )
        _apply_learning(quality)
        note = f"{quality.upper()} | THRESHOLD: {round(_adaptive_threshold)}"
        await notify.send_exit(
            notify.format_exit(pos.symbol, pos.exchange, event.price, pnl_pct, event.reason, note)
        )
    else:
        # Partial take-profit (rest keeps running).
        await notify.send_entry(
            notify.format_partial(pos.symbol, pos.exchange, event.price, pct, event.pnl, event.reason)
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

# Admin-only page to create/manage the per-user accounts. Same dark palette as
# the dashboard + login. Reached at /admin (gated to role=admin in _auth_gate).
ADMIN_USERS_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TradeOS AI · Cuentas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  *{box-sizing:border-box} body{margin:0;font-family:Geist,system-ui,sans-serif;background:#070a0f;color:#e6e9ef;padding:24px}
  .wrap{max-width:760px;margin:0 auto}
  .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
  h1{font-size:18px;margin:0;font-weight:600}
  a.back{color:#8b95a7;text-decoration:none;font-size:13px} a.back:hover{color:#ff5a86}
  .card{background:#0c1018;border:1px solid #1b2230;border-radius:14px;padding:18px;margin-bottom:16px}
  .card h2{font-size:13px;margin:0 0 12px;color:#b6bdcc;font-weight:600}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
  .fld{flex:1;min-width:140px} label{display:block;font-size:11px;color:#8b95a7;margin:0 0 5px}
  input,select{width:100%;background:#070a0f;border:1px solid #222b3a;border-radius:9px;color:#e6e9ef;padding:9px 11px;font-family:inherit;font-size:13px;outline:none}
  input:focus,select:focus{border-color:#3a4760}
  button{background:linear-gradient(90deg,#ff2f6e,#ff5a86);border:0;color:#fff;padding:9px 16px;border-radius:9px;font-weight:600;font-size:13px;cursor:pointer;font-family:inherit}
  button.ghost{background:transparent;border:1px solid #33405a;color:#b6bdcc}
  table{width:100%;border-collapse:collapse;font-size:13px} th{text-align:left;color:#8b95a7;font-weight:500;font-size:11px;padding:8px 10px;border-bottom:1px solid #1b2230}
  td{padding:10px;border-bottom:1px solid #131923}
  .tag{font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid #33405a;color:#b6bdcc}
  .tag.admin{color:#ff8fb0;border-color:#ff2f6e44} .tag.on{color:#43d39e;border-color:#43d39e44} .tag.off{color:#ff6b6b;border-color:#ff6b6b44}
  .msg{font-size:12px;margin-top:10px;min-height:14px} .msg.err{color:#ff6b6b} .msg.ok{color:#43d39e}
  .acts{display:flex;gap:6px;justify-content:flex-end}
</style></head><body><div class="wrap">
  <div class="top"><h1>Cuentas · TradeOS AI</h1><a class="back" href="/">← Volver al panel</a></div>
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


@app.get("/candidates", response_model=list[TokenCandidate])
async def list_candidates() -> list[TokenCandidate]:
    return sorted(_candidates.values(), key=lambda c: c.pump_score, reverse=True)


def _scan_exchanges() -> list[str]:
    raw = os.getenv("PUMP_SCAN_EXCHANGES", "binance,mexc,bitget")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


async def _auto_enter(bot: UserBot, candidate: TokenCandidate, accel: float | None = None) -> None:
    """Paper-only auto buy on a confirmed candidate into ONE user's bot; hand it
    to that bot's exit engine.

    Runs the ForensicFilter gate first (real CEX-sourced spread/liquidity/book
    checks). A blocked candidate is logged and skipped — never bought."""
    ok, reasons = forensic_check(
        spread_pct=candidate.spread_pct,
        liquidity_usd=candidate.liquidity_usd,
        top_book_share=candidate.top_book_share,
    )
    if not ok:
        _record_learning(candidate.symbol, "forensic_block", "paper", candidate, "; ".join(reasons))
        await store.insert_bot_log(
            "PUMP_SCANNER", "INFO",
            f"ForensicFilter bloqueó {candidate.symbol}: {'; '.join(reasons)}",
        )
        logger.info("forensic block %s: %s", candidate.symbol, reasons)
        return

    result = await bot.engine.act(
        symbol=candidate.symbol, side=Side.buy, reference_price=candidate.last_price,
        capital_usd=bot.auto_entry_usd, exchanges=[candidate.exchange],
        open_trades=bot.open_count(),
    )
    for fill in result.fills:
        bot.pm.open(
            symbol=fill.symbol, exchange=fill.exchange, entry_price=fill.fill_price,
            qty=fill.amount, pump_score=candidate.pump_score, classification=candidate.classification,
        )
        opened = bot.pm.positions.get(bot.pm.key(fill.exchange, fill.symbol))
        if opened:
            await _persist_position(bot, opened)
        _record_learning(candidate.symbol, "auto_entry", "paper", candidate, f"bought ${bot.auto_entry_usd:.0f} @ {fill.fill_price}")
        await store.insert_bot_log(
            "PUMP_SCANNER", "TRADE_BUY",
            f"Auto-entry {candidate.symbol} ${bot.auto_entry_usd:.0f} @ {fill.fill_price}",
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


# Cross-exchange arbitrage detection threshold (%). Detection only — executing
# arbitrage needs funded balances on BOTH venues + live keys, so paper mode
# alerts without trading (no fabricated cross-venue fills).
ARB_SPREAD_PCT = float(os.getenv("PUMP_ARB_SPREAD_PCT", "1.5"))


async def _arbitrage_scan() -> None:
    """Same symbol on 2+ scanned exchanges with a price gap >= ARB_SPREAD_PCT →
    alert. Real prices from the scan; no execution in paper."""
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
        if spread >= ARB_SPREAD_PCT:
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
            await notify.send_alert(format_alert(
                candidate.symbol, candidate.pump_score, candidate.classification,
                candidate.flags, cluster=candidate.cluster, exchange=candidate.exchange,
                liquidity_usd=candidate.liquidity_usd,
            ))
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
            if current_mode() == ExecMode.paper:
                # Each user's bot enters independently (own balance/caps), only if
                # that user has auto-entry enabled.
                for bot in all_bots():
                    if bot.auto_entry and not bot.pm.has(candidate.exchange, candidate.symbol):
                        await _auto_enter(bot, candidate)
    # Cross-exchange arbitrage detection (alert-only in paper).
    try:
        await _arbitrage_scan()
    except Exception:
        logger.exception("arbitrage scan failed")
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
        _adaptive_threshold = float(req.confirmation_threshold)
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

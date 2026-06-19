"""Persistence: Supabase (cloud) OR local SQLite — never lose data on restart.

Priority: if SUPABASE_URL + SUPABASE_SERVICE_KEY are set, the bot mirrors to
Supabase exactly as before (multi-tenant, RLS, realtime). If they are NOT set,
everything falls back to a local SQLite file so a single-box deploy still keeps
positions / equity / alerts / users across restarts.

Every write is best-effort: failures are swallowed and logged, never crash a
trading loop. The Supabase code path is unchanged; SQLite is a drop-in fallback
that goes through the same public functions, so callers don't care which backend
is active. SQLite auto-adds any column a row needs (lossless round-trip) and is
single-process safe (WAL + a threading lock).

See infrastructure/supabase/schema.sql for the cloud tables.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

import httpx

logger = logging.getLogger("pump-reader.store")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    """True = Supabase backend. False = local SQLite fallback (still persists)."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


# ============================================================================
#  Local SQLite backend (fallback when Supabase is not configured)
# ============================================================================

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SQLITE_PATH = os.getenv("PUMP_SQLITE_PATH", str(_DATA_DIR / "tradeos.db"))
_sqlite_conn: sqlite3.Connection | None = None
_sqlite_lock = threading.Lock()

# Tables + their conflict key. Columns are created lazily (auto-add on first
# write that needs them) so we never silently drop a field. PK/UNIQUE on the
# conflict column makes INSERT OR REPLACE behave as an upsert.
_DDL = """
CREATE TABLE IF NOT EXISTS managed_positions (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS candidates (exchange TEXT, symbol TEXT, UNIQUE(exchange, symbol));
CREATE TABLE IF NOT EXISTS equity_history (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS exit_events (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS learning_records (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS app_users (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS grid_state (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS grid_fills (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS allocation (user_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS account_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS token_market (symbol TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bot_logs (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS pump_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT);
"""


def _conn() -> sqlite3.Connection:
    global _sqlite_conn
    with _sqlite_lock:
        if _sqlite_conn is None:
            Path(SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.execute("PRAGMA busy_timeout=5000;")
            c.executescript(_DDL)
            c.commit()
            _sqlite_conn = c
        return _sqlite_conn


def _columns(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_columns(c: sqlite3.Connection, table: str, row: dict) -> None:
    have = _columns(c, table)
    for k in row:
        if k not in have:
            c.execute(f'ALTER TABLE {table} ADD COLUMN "{k}"')


def _sql_value(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v)
    return v


def _sqlite_write(table: str, row: dict, replace: bool) -> None:
    if not row:
        return
    c = _conn()
    with _sqlite_lock:
        _ensure_columns(c, table, row)
        cols = ",".join(f'"{k}"' for k in row)
        ph = ",".join("?" * len(row))
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        c.execute(f"{verb} INTO {table} ({cols}) VALUES ({ph})",
                  [_sql_value(v) for v in row.values()])
        c.commit()


def _sqlite_select(table: str, where: str = "", args: tuple = (),
                   select: str = "*", order: str = "", limit: int | None = None) -> list[dict]:
    c = _conn()
    sql = f"SELECT {select} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order:  # PostgREST-style "col.dir" -> SQL "col dir"
        col, _, direction = order.partition(".")
        sql += f" ORDER BY {col} {direction.upper() or 'ASC'}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _sqlite_lock:
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.row_factory = None
        return rows


# ============================================================================
#  Supabase backend (unchanged)
# ============================================================================

def _headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


async def _get_client() -> httpx.AsyncClient | None:
    global _client
    if not enabled():
        return None
    if _client is None:
        _client = httpx.AsyncClient(base_url=f"{SUPABASE_URL}/rest/v1", timeout=10.0)
    return _client


async def close() -> None:
    global _client, _sqlite_conn
    if _client is not None:
        await _client.aclose()
        _client = None
    if _sqlite_conn is not None:
        with _sqlite_lock:
            _sqlite_conn.close()
            _sqlite_conn = None


async def _insert(table: str, rows: list[dict] | dict) -> None:
    if enabled():
        client = await _get_client()
        if client is None:
            return
        try:
            await client.post(f"/{table}", headers=_headers(), json=rows)
        except Exception:
            logger.exception("supabase insert failed: %s", table)
        return
    try:
        for row in (rows if isinstance(rows, list) else [rows]):
            _sqlite_write(table, row, replace=False)
    except Exception:
        logger.exception("sqlite insert failed: %s", table)


async def _upsert(table: str, rows: list[dict] | dict, on_conflict: str) -> None:
    if enabled():
        client = await _get_client()
        if client is None:
            return
        try:
            await client.post(
                f"/{table}",
                headers=_headers({"Prefer": "resolution=merge-duplicates"}),
                params={"on_conflict": on_conflict},
                json=rows,
            )
        except Exception:
            logger.exception("supabase upsert failed: %s", table)
        return
    try:
        for row in (rows if isinstance(rows, list) else [rows]):
            _sqlite_write(table, row, replace=True)
    except Exception:
        logger.exception("sqlite upsert failed: %s", table)


# --- typed helpers (called from main.py at the right moments) ----------------

async def upsert_candidates(candidates: list[dict]) -> None:
    if not candidates:
        return
    await _upsert("candidates", candidates, on_conflict="exchange,symbol")


async def insert_learning(rec: dict) -> None:
    await _insert("learning_records", rec)


async def upsert_position(pos: dict) -> None:
    await _upsert("managed_positions", pos, on_conflict="key")


async def list_open_positions(user_id: str | None = None) -> list[dict]:
    """Still-open managed positions so the bot rebuilds its in-memory state on
    startup. With user_id, only that user's positions (Phase B isolation)."""
    if enabled():
        client = await _get_client()
        if client is None:
            return []
        try:
            params = {"closed": "eq.false", "select": "*"}
            if user_id is not None:
                params["user_id"] = f"eq.{user_id}"
            r = await client.get("/managed_positions", headers=_headers(), params=params)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("supabase read managed_positions failed")
            return []
    try:
        where, args = "closed = 0", ()
        if user_id is not None:
            where, args = "closed = 0 AND user_id = ?", (user_id,)
        return _sqlite_select("managed_positions", where, args, order="entry_at.asc")
    except Exception:
        logger.exception("sqlite read managed_positions failed")
        return []


async def insert_exit(event: dict) -> None:
    await _insert("exit_events", event)


async def insert_equity(point: dict) -> None:
    await _insert("equity_history", point)


async def list_equity(limit: int = 200, user_id: str | None = None) -> list[dict]:
    """Last `limit` equity points (oldest→newest) to rehydrate the curve."""
    if enabled():
        client = await _get_client()
        if client is None:
            return []
        try:
            params = {"select": "t,v", "order": "t.desc", "limit": str(limit)}
            if user_id is not None:
                params["user_id"] = f"eq.{user_id}"
            r = await client.get("/equity_history", headers=_headers(), params=params)
            r.raise_for_status()
            data = r.json()
            return list(reversed(data)) if isinstance(data, list) else []
        except Exception:
            logger.exception("supabase read equity_history failed")
            return []
    try:
        where, args = "", ()
        if user_id is not None:
            where, args = "user_id = ?", (user_id,)
        rows = _sqlite_select("equity_history", where, args, select="t,v",
                              order="t.desc", limit=limit)
        return list(reversed(rows))
    except Exception:
        logger.exception("sqlite read equity_history failed")
        return []


async def insert_alert(alert: dict) -> None:
    await _insert("alerts", alert)


# --- multi-user accounts (app_users) ----------------------------------------

async def list_users() -> list[dict]:
    if enabled():
        client = await _get_client()
        if client is None:
            return []
        try:
            r = await client.get("/app_users", headers=_headers(),
                                 params={"select": "id,username,role,active,created_at", "order": "created_at.asc"})
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("supabase read app_users failed")
            return []
    try:
        return _sqlite_select("app_users", select="id,username,role,active,created_at",
                              order="created_at.asc")
    except Exception:
        logger.exception("sqlite read app_users failed")
        return []


async def list_users_with_hash() -> list[dict]:
    if enabled():
        client = await _get_client()
        if client is None:
            return []
        try:
            r = await client.get("/app_users", headers=_headers(), params={"select": "*"})
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("supabase read app_users (with hash) failed")
            return []
    try:
        return _sqlite_select("app_users")
    except Exception:
        logger.exception("sqlite read app_users (with hash) failed")
        return []


async def insert_user(row: dict) -> dict | None:
    if enabled():
        client = await _get_client()
        if client is None:
            return None
        try:
            r = await client.post("/app_users", headers=_headers({"Prefer": "return=representation"}), json=row)
            r.raise_for_status()
            data = r.json()
            return data[0] if isinstance(data, list) and data else None
        except Exception:
            logger.exception("supabase insert app_users failed")
            return None
    try:
        _sqlite_write("app_users", row, replace=False)
        return row
    except Exception:
        logger.exception("sqlite insert app_users failed")
        return None


async def update_user(user_id: str, patch: dict) -> None:
    if enabled():
        client = await _get_client()
        if client is None:
            return
        try:
            await client.patch("/app_users", headers=_headers(),
                               params={"id": f"eq.{user_id}"}, json=patch)
        except Exception:
            logger.exception("supabase update app_users failed")
        return
    try:
        if not patch:
            return
        c = _conn()
        with _sqlite_lock:
            _ensure_columns(c, "app_users", patch)
            sets = ", ".join(f'"{k}" = ?' for k in patch)
            c.execute(f"UPDATE app_users SET {sets} WHERE id = ?",
                      [_sql_value(v) for v in patch.values()] + [user_id])
            c.commit()
    except Exception:
        logger.exception("sqlite update app_users failed")


async def upsert_grid(state: dict) -> None:
    await _upsert("grid_state", {**state, "id": "default"}, on_conflict="id")


async def insert_grid_fill(fill: dict) -> None:
    await _insert("grid_fills", fill)


async def upsert_allocation(alloc: dict, user_id: str = "owner") -> None:
    # One allocation row per user (Phase B): conflict on user_id.
    await _upsert("allocation", {**alloc, "user_id": user_id}, on_conflict="user_id")


async def insert_account_snapshot(snap: dict) -> None:
    await _insert("account_snapshots", snap)


async def upsert_token_market(market: dict) -> None:
    await _upsert("token_market", market, on_conflict="symbol")


# --- dashboard-control schema (bot_logs / pump_candidates) -------------------

async def insert_bot_log(bot_name: str, status: str, message: str,
                         pnl: float | None = None, volumen: float | None = None) -> None:
    await _insert("bot_logs", {
        "bot_name": bot_name, "status": status, "message": message,
        "pnl": pnl, "volumen": volumen,
    })


async def insert_pump_candidate(row: dict) -> None:
    await _insert("pump_candidates", row)

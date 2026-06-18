"""Supabase persistence (gated, best-effort).

The bot writes with the Supabase SERVICE ROLE key via PostgREST. Everything here
is OPTIONAL: if SUPABASE_URL / SUPABASE_SERVICE_KEY are not set, every function
is a no-op and the bot runs purely in-memory (as before). Persistence failures
are swallowed and logged — they must never crash the trading loops.

See infrastructure/supabase/schema.sql for the tables.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("pump-reader.store")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


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
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _insert(table: str, rows: list[dict] | dict) -> None:
    client = await _get_client()
    if client is None:
        return
    try:
        await client.post(f"/{table}", headers=_headers(), json=rows)
    except Exception:
        logger.exception("supabase insert failed: %s", table)


async def _upsert(table: str, rows: list[dict] | dict, on_conflict: str) -> None:
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
    """Read back the still-open managed positions so the bot can rebuild its
    in-memory state on startup (Phase 1/2 context survives a restart). With
    user_id, returns only that user's positions (Phase B per-user isolation)."""
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


async def insert_exit(event: dict) -> None:
    await _insert("exit_events", event)


async def insert_equity(point: dict) -> None:
    await _insert("equity_history", point)


async def list_equity(limit: int = 200, user_id: str | None = None) -> list[dict]:
    """Last `limit` equity points (oldest→newest) to rehydrate the curve on
    startup so it survives restarts. With user_id, only that user's curve."""
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


async def insert_alert(alert: dict) -> None:
    await _insert("alerts", alert)


# --- multi-user accounts (app_users) ----------------------------------------

async def list_users() -> list[dict]:
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
        # password_hash is needed for auth — fetch it in a second, scoped call so
        # the select above can stay narrow for the admin listing.
        logger.exception("supabase read app_users failed")
        return []


async def list_users_with_hash() -> list[dict]:
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


async def insert_user(row: dict) -> dict | None:
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


async def update_user(user_id: str, patch: dict) -> None:
    client = await _get_client()
    if client is None:
        return
    try:
        await client.patch("/app_users", headers=_headers(),
                           params={"id": f"eq.{user_id}"}, json=patch)
    except Exception:
        logger.exception("supabase update app_users failed")


async def upsert_grid(state: dict) -> None:
    await _upsert("grid_state", {**state, "id": "default"}, on_conflict="id")


async def insert_grid_fill(fill: dict) -> None:
    await _insert("grid_fills", fill)


async def upsert_allocation(alloc: dict, user_id: str = "owner") -> None:
    # One allocation row per user (Phase B): conflict on user_id, not the old
    # singleton id='default'. Legacy 'default' row carries user_id='owner'.
    await _upsert("allocation", {**alloc, "user_id": user_id}, on_conflict="user_id")


async def insert_account_snapshot(snap: dict) -> None:
    await _insert("account_snapshots", snap)


async def upsert_token_market(market: dict) -> None:
    await _upsert("token_market", market, on_conflict="symbol")


# --- dashboard-control schema (bot_controls / bot_logs / pump_candidates) -----
# These match the Next.js dashboard tables. Writes go through the service key.

async def insert_bot_log(bot_name: str, status: str, message: str,
                         pnl: float | None = None, volumen: float | None = None) -> None:
    await _insert("bot_logs", {
        "bot_name": bot_name, "status": status, "message": message,
        "pnl": pnl, "volumen": volumen,
    })


async def insert_pump_candidate(row: dict) -> None:
    await _insert("pump_candidates", row)

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


async def insert_exit(event: dict) -> None:
    await _insert("exit_events", event)


async def insert_equity(point: dict) -> None:
    await _insert("equity_history", point)


async def insert_alert(alert: dict) -> None:
    await _insert("alerts", alert)


async def upsert_grid(state: dict) -> None:
    await _upsert("grid_state", {**state, "id": "default"}, on_conflict="id")


async def insert_grid_fill(fill: dict) -> None:
    await _insert("grid_fills", fill)


async def upsert_allocation(alloc: dict) -> None:
    await _upsert("allocation", {**alloc, "id": "default"}, on_conflict="id")


async def insert_account_snapshot(snap: dict) -> None:
    await _insert("account_snapshots", snap)


async def upsert_token_market(market: dict) -> None:
    await _upsert("token_market", market, on_conflict="symbol")

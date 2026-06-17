"""Mirror the embedded GRVTBot's live state into Supabase (best-effort).

The real GRVTBot keeps its own SQLite engine (that's how the upstream bot works).
This poller copies a compact aggregate snapshot + the per-bot list into the
shared Supabase tables so the grid data lives in the SAME realtime store as the
pump side — without rewriting the bot's storage.

No-op when Supabase is disabled or the bot is unreachable. Reuses the existing
grid_state / equity_history / account_snapshots tables (no new DDL):
  - grid_state(id='default')  → aggregate + per-bot array (jsonb `grid`)
  - equity_history(t, v)      → total equity time series
  - account_snapshots         → full portfolio-summary JSON history (exchange='GRVT')
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx

from . import store

logger = logging.getLogger("pump-reader.grid-sync")

GRID_BACKEND = "http://127.0.0.1:3848"
OWNER_EMAIL = os.getenv("GRID_OWNER_EMAIL", "admin@tradeos.local")
OWNER_PASSWORD = os.getenv("GRID_OWNER_PASSWORD", "")

_token: str | None = None
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=GRID_BACKEND, timeout=10.0)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _login() -> str | None:
    global _token
    if not OWNER_PASSWORD:
        return None
    try:
        r = await _get_client().post(
            "/api/v2/auth/login",
            json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
        )
        if r.status_code == 200:
            _token = r.json().get("token")
            return _token
    except httpx.HTTPError:
        pass
    return None


async def _get(path: str):
    """GET /api/v2{path} with the owner JWT; re-login once on 401."""
    global _token
    client = _get_client()
    if _token is None and await _login() is None:
        return None
    for attempt in (1, 2):
        try:
            r = await client.get(f"/api/v2{path}", headers={"Authorization": f"Bearer {_token}"})
        except httpx.HTTPError:
            return None
        if r.status_code == 401 and attempt == 1:
            if await _login() is None:
                return None
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def _compact_bot(b: dict) -> dict:
    return {
        "id": b.get("id"),
        "pair": b.get("pair"),
        "status": b.get("status"),
        "direction": b.get("direction"),
        "investment": b.get("investment_usdt"),
        "pnl": b.get("total_pnl_usdt"),
        "grid_profit": b.get("grid_profit_usdt"),
        "position": b.get("position_size"),
        "lower": b.get("lower_price"),
        "upper": b.get("upper_price"),
        "grids": b.get("num_grids"),
        "leverage": b.get("leverage"),
    }


async def sync_once() -> bool:
    """Pull portfolio summary + bots from the GRVTBot and mirror to Supabase."""
    if not store.enabled():
        return False
    summary = await _get("/portfolio-summary")
    if summary is None:
        return False  # bot offline or v2 disabled — stay quiet

    bots_resp = await _get("/bots")
    if isinstance(bots_resp, dict):
        bots = bots_resp.get("bots") or bots_resp.get("results") or []
    elif isinstance(bots_resp, list):
        bots = bots_resp
    else:
        bots = []
    compact = [_compact_bot(b) for b in bots if isinstance(b, dict)]
    pairs = sorted({c["pair"] for c in compact if c.get("pair")})
    now = datetime.now(UTC).isoformat()
    equity = float(summary.get("totalEquity") or 0)

    await store.upsert_grid({
        "pair": pairs[0] if len(pairs) == 1 else ("MULTI" if pairs else "—"),
        "lower_price": 0,
        "upper_price": 0,
        "levels": int(summary.get("botCount") or 0),
        "capital": float(summary.get("totalInvested") or 0),
        "cash": equity,
        "position": float(summary.get("totalPositionUsdt") or 0),
        "realized": float(summary.get("totalRealized") or 0),
        "last_price": 0,
        "running": int(summary.get("runningCount") or 0) > 0,
        "grid": compact,
        "held": [],
        "qty": [],
        "updated_at": now,
    })

    if equity > 0:
        await store.insert_equity({"t": now, "v": equity})
        await store.insert_account_snapshot({
            "exchange": "GRVT",
            "total_usdt": equity,
            "balances": summary,
            "at": now,
        })
    return True

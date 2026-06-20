"""Self-migration — create the bot's Supabase (Postgres) tables on startup.

The REST API the bot normally uses (PostgREST) cannot run DDL, so the Phase-D
tables (learning_outcomes, exit_telemetry, trade_analytics, metrics_snapshots)
must be created over a real Postgres connection. If SUPABASE_DB_URL is set
(Supabase dashboard → Settings → Database → Connection string), the bot opens
that connection ONCE at boot and applies the idempotent DDL below — persistence
goes live with no manual SQL. No URL set → no-op (manual SQL / SQLite still work).

Fully fail-safe: any error is logged and startup continues. CREATE-only +
`if not exists` → never drops or alters existing data. RLS is enabled with no
policies so the anon key can't read these (the bot writes with the SERVICE key,
which bypasses RLS).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("pump-reader.db_migrate")

# Only the tables the REST path can't create. Pre-existing core tables
# (managed_positions, equity_history, exit_events, ...) are left untouched.
PHASE_D_DDL = """
create table if not exists public.learning_outcomes (
    id text primary key, symbol text, exchange text, source text,
    alert_at timestamptz, alert_price double precision, pump_score integer,
    cluster text, classification text, signals jsonb,
    peak_price double precision, peak_at timestamptz, peak_24h double precision,
    low_price double precision, last_price double precision,
    settled boolean default false, label text default 'pending'
);
create index if not exists learning_outcomes_alert_at_idx on public.learning_outcomes (alert_at desc);
alter table public.learning_outcomes enable row level security;

create table if not exists public.exit_telemetry (
    id bigint generated always as identity primary key,
    symbol text, exchange text, cluster text,
    signal_at timestamptz, entry_at timestamptz, exit_at timestamptz,
    exit_reason text, be_activated_at timestamptz, trail_activated_at timestamptz,
    holding_secs double precision, exit_source text,
    ws_reaction_delay_ms double precision, pnl double precision, user_id text,
    created_at timestamptz default now()
);
create index if not exists exit_telemetry_created_at_idx on public.exit_telemetry (created_at desc);
alter table public.exit_telemetry enable row level security;

create table if not exists public.trade_analytics (
    trade_id text primary key, symbol text, exchange text, setup_type text,
    signal_timestamp timestamptz, entry_timestamp timestamptz, exit_timestamp timestamptz,
    entry_price double precision, exit_price double precision, position_size double precision,
    pnl_pct double precision, pnl_usd double precision,
    mfe_pct double precision, mae_pct double precision,
    holding_seconds double precision, lead_time_seconds double precision,
    entry_slippage_pct double precision, exit_slippage_pct double precision,
    exit_reason text, confidence_score double precision, risk_used double precision,
    market_regime text, trade_quality_score double precision,
    sizing_mode text, sizing_multiplier double precision,
    theoretical_size double precision, theoretical_pnl_usd double precision,
    user_id text, created_at timestamptz default now()
);
create index if not exists trade_analytics_exit_at_idx on public.trade_analytics (exit_timestamp desc);
create index if not exists trade_analytics_setup_idx   on public.trade_analytics (setup_type);
create index if not exists trade_analytics_regime_idx  on public.trade_analytics (market_regime);
alter table public.trade_analytics enable row level security;

create table if not exists public.metrics_snapshots (
    id bigint generated always as identity primary key,
    at timestamptz, trades integer, win_rate double precision,
    expectancy double precision, profit_factor double precision,
    max_drawdown double precision, net_equity double precision,
    regime text, edge_status text, created_at timestamptz default now()
);
create index if not exists metrics_snapshots_at_idx on public.metrics_snapshots (at desc);
alter table public.metrics_snapshots enable row level security;
"""


async def run() -> dict:
    """Apply the Phase-D DDL over a direct Postgres connection. Returns a small
    status dict; never raises."""
    url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not url:
        logger.info("SUPABASE_DB_URL not set → skipping self-migration "
                    "(use manual SQL or SQLite). Set it to auto-create tables.")
        return {"ran": False, "reason": "no SUPABASE_DB_URL"}
    try:
        import asyncpg
    except Exception:
        logger.warning("asyncpg not installed → cannot self-migrate")
        return {"ran": False, "reason": "asyncpg missing"}
    conn = None
    try:
        # statement_cache_size=0 keeps it compatible with the pgbouncer pooler.
        conn = await asyncpg.connect(url, statement_cache_size=0, timeout=15)
        await conn.execute(PHASE_D_DDL)
        logger.info("self-migration OK — Phase-D tables ensured in Supabase")
        return {"ran": True}
    except Exception as exc:
        logger.exception("self-migration failed (continuing on REST/SQLite)")
        return {"ran": False, "reason": repr(exc)}
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

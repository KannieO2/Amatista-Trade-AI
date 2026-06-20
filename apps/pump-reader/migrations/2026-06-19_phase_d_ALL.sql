-- Phase D — ALL missing tables, consolidated. Run ONCE in the Supabase SQL
-- editor (Dashboard → SQL Editor → paste → Run) IF you are not using the bot's
-- automatic self-migration (SUPABASE_DB_URL). Idempotent + CREATE-only: safe to
-- run repeatedly, never drops or alters existing data.
--
-- These four tables are the only ones the bot's REST path cannot create itself:
--   learning_outcomes  — Learning Lab survives restarts (MFE/MAE/lead-time).
--   exit_telemetry     — per-exit reaction diagnostics.
--   trade_analytics    — permanent per-trade fact (expectancy/PF/quality/...).
--   metrics_snapshots  — headline-metric trend over time.
-- RLS is enabled with no policies: the anon key can't read them; the bot writes
-- with the SERVICE key, which bypasses RLS.

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

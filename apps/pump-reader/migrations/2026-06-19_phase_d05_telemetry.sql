-- Phase D-0.5 — execution upgrade. Two new tables:
--   learning_outcomes : persists the Learning Lab so MFE/MAE/lead-time and
--                       cluster metrics ACCUMULATE across restarts (the optimizer
--                       can only act once enough settled outcomes exist).
--   exit_telemetry    : per-exit diagnostics (signal/entry/exit timing, WS
--                       reaction delay, how the protective stops armed).
--
-- Idempotent. Run once in the Supabase SQL editor. The bot writes with the
-- SERVICE key (bypasses RLS), so no policies are required for it to work; add
-- your own RLS later if the dashboard ever reads these with the anon key.

create table if not exists public.learning_outcomes (
    id             text primary key,
    symbol         text,
    exchange       text,
    source         text,
    alert_at       timestamptz,
    alert_price    double precision,
    pump_score     integer,
    cluster        text,
    classification text,
    signals        jsonb,
    peak_price     double precision,
    peak_at        timestamptz,
    peak_24h       double precision,
    low_price      double precision,
    last_price     double precision,
    settled        boolean default false,
    label          text default 'pending'
);
create index if not exists learning_outcomes_alert_at_idx on public.learning_outcomes (alert_at desc);

create table if not exists public.exit_telemetry (
    id                   bigint generated always as identity primary key,
    symbol               text,
    exchange             text,
    cluster              text,
    signal_at            timestamptz,
    entry_at             timestamptz,
    exit_at              timestamptz,
    exit_reason          text,
    be_activated_at      timestamptz,
    trail_activated_at   timestamptz,
    holding_secs         double precision,
    exit_source          text,
    ws_reaction_delay_ms double precision,
    pnl                  double precision,
    user_id              text,
    created_at           timestamptz default now()
);
create index if not exists exit_telemetry_created_at_idx on public.exit_telemetry (created_at desc);

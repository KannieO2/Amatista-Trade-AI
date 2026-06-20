-- Phase D — quantitative intelligence layer. Two new tables:
--   trade_analytics  : one permanent fact per closed trade (Module 1). Every
--                      derived metric (expectancy, profit factor, ranking,
--                      drawdown, confidence, trade-quality, sizing simulation)
--                      is computed from these rows — they are the source of truth.
--   metrics_snapshots: periodic headline-metric snapshot so edge / expectancy /
--                      PF / drawdown TRENDS can be charted without recomputing
--                      from the full trade log.
--
-- Idempotent. Run once in the Supabase SQL editor. The bot writes with the
-- SERVICE key (bypasses RLS); add your own RLS later if the dashboard reads
-- these with the anon key. Pure analytics — no trading behaviour depends on them.

create table if not exists public.trade_analytics (
    trade_id             text primary key,
    symbol               text,
    exchange             text,
    setup_type           text,
    signal_timestamp     timestamptz,
    entry_timestamp      timestamptz,
    exit_timestamp       timestamptz,
    entry_price          double precision,
    exit_price           double precision,
    position_size        double precision,
    pnl_pct              double precision,
    pnl_usd              double precision,
    mfe_pct              double precision,
    mae_pct              double precision,
    holding_seconds      double precision,
    lead_time_seconds    double precision,
    entry_slippage_pct   double precision,
    exit_slippage_pct    double precision,
    exit_reason          text,
    confidence_score     double precision,
    risk_used            double precision,
    market_regime        text,
    trade_quality_score  double precision,
    sizing_mode          text,
    sizing_multiplier    double precision,
    theoretical_size     double precision,
    theoretical_pnl_usd  double precision,
    user_id              text,
    created_at           timestamptz default now()
);
create index if not exists trade_analytics_exit_at_idx on public.trade_analytics (exit_timestamp desc);
create index if not exists trade_analytics_setup_idx   on public.trade_analytics (setup_type);
create index if not exists trade_analytics_regime_idx  on public.trade_analytics (market_regime);

create table if not exists public.metrics_snapshots (
    id            bigint generated always as identity primary key,
    at            timestamptz,
    trades        integer,
    win_rate      double precision,
    expectancy    double precision,
    profit_factor double precision,
    max_drawdown  double precision,
    net_equity    double precision,
    regime        text,
    edge_status   text,
    created_at    timestamptz default now()
);
create index if not exists metrics_snapshots_at_idx on public.metrics_snapshots (at desc);

-- TradeOS AI Pump Reader — Supabase schema (idempotent).
--
-- Run this in the Supabase SQL editor (or `supabase db push`). Safe to re-run:
-- every object uses IF NOT EXISTS / DROP ... IF EXISTS.
--
-- SECURITY MODEL
-- The bot is a single backend process (hosted on the Oracle free VM). It writes
-- with the Supabase SERVICE ROLE key, which BYPASSES RLS. The browser never
-- talks to Supabase directly — the FastAPI app serves its own dashboard. So we:
--   * enable RLS on every table, and
--   * add NO anon/authenticated policies → the tables are fully locked to the
--     backend (service key only). Anon/public cannot read or write.
-- If you later want the browser to read live data straight from Supabase
-- (realtime), add the SELECT-for-anon policies at the bottom (left commented).

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- candidates: latest scan snapshot (one row per exchange+symbol, upserted)
-- ---------------------------------------------------------------------------
create table if not exists public.candidates (
  id                   uuid primary key default gen_random_uuid(),
  symbol               text not null,
  exchange             text not null,
  last_price           double precision not null default 0,
  quote_volume_24h     double precision not null default 0,
  price_change_pct_24h double precision not null default 0,
  volume_spike         double precision not null default 0,
  orderbook_imbalance  double precision not null default 0,
  liquidity_usd        double precision not null default 0,
  pump_score           integer not null default 0,
  confidence_score     integer not null default 0,
  classification       text not null default 'no_signal',
  cluster              text not null default 'long_pump',
  flags                jsonb not null default '[]'::jsonb,
  spark                jsonb not null default '[]'::jsonb,
  status               text not null default 'watching',
  updated_at           timestamptz not null default now(),
  unique (exchange, symbol)
);
create index if not exists candidates_score_idx on public.candidates (pump_score desc);

-- ---------------------------------------------------------------------------
-- learning_records: signal -> action -> result ledger (feeds threshold tuning)
-- ---------------------------------------------------------------------------
create table if not exists public.learning_records (
  id             uuid primary key default gen_random_uuid(),
  symbol         text not null,
  action         text not null,
  mode           text not null default 'paper',
  pump_score     integer not null default 0,
  classification text not null default 'n/a',
  detail         text not null default '',
  created_at     timestamptz not null default now()
);
create index if not exists learning_created_idx on public.learning_records (created_at desc);

-- ---------------------------------------------------------------------------
-- managed_positions: the exit engine's open/closed positions (upsert by key)
-- ---------------------------------------------------------------------------
create table if not exists public.managed_positions (
  key            text primary key,                 -- "exchange:symbol"
  symbol         text not null,
  exchange       text not null,
  entry_price    double precision not null,
  qty            double precision not null,
  initial_qty    double precision not null,
  phase          integer not null default 1,
  peak_price     double precision not null default 0,
  last_price     double precision not null default 0,
  realized_pnl   double precision not null default 0,
  closed         boolean not null default false,
  pump_score     integer not null default 0,
  classification text not null default 'n/a',
  entry_at       timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- exit_events: every partial/full exit (tp1, trailing, dump, hard_stop)
-- ---------------------------------------------------------------------------
create table if not exists public.exit_events (
  id        uuid primary key default gen_random_uuid(),
  symbol    text not null,
  exchange  text not null,
  reason    text not null,
  sold_qty  double precision not null default 0,
  price     double precision not null default 0,
  pnl       double precision not null default 0,
  fraction  double precision not null default 0,
  closed    boolean not null default false,
  at        timestamptz not null default now()
);
create index if not exists exit_at_idx on public.exit_events (at desc);

-- ---------------------------------------------------------------------------
-- equity_history: paper/live account equity over time (for the curve)
-- ---------------------------------------------------------------------------
create table if not exists public.equity_history (
  id bigint generated always as identity primary key,
  t  timestamptz not null default now(),
  v  double precision not null
);
create index if not exists equity_t_idx on public.equity_history (t desc);

-- ---------------------------------------------------------------------------
-- alerts: confirmation-threshold alerts (mirrors Telegram sends)
-- ---------------------------------------------------------------------------
create table if not exists public.alerts (
  id             uuid primary key default gen_random_uuid(),
  symbol         text not null,
  exchange       text not null,
  pump_score     integer not null default 0,
  classification text not null default 'n/a',
  flags          jsonb not null default '[]'::jsonb,
  created_at     timestamptz not null default now()
);
create index if not exists alerts_created_idx on public.alerts (created_at desc);

-- ---------------------------------------------------------------------------
-- grid_state: GRVT-style grid engine state (single row, upsert by id)
-- ---------------------------------------------------------------------------
create table if not exists public.grid_state (
  id           text primary key default 'default',
  pair         text not null default 'BTC/USDT',
  lower_price  double precision not null default 0,
  upper_price  double precision not null default 0,
  levels       integer not null default 0,
  capital      double precision not null default 0,
  cash         double precision not null default 0,
  position     double precision not null default 0,
  realized     double precision not null default 0,
  last_price   double precision not null default 0,
  running      boolean not null default false,
  grid         jsonb not null default '[]'::jsonb,
  held         jsonb not null default '[]'::jsonb,
  qty          jsonb not null default '[]'::jsonb,
  updated_at   timestamptz not null default now()
);

create table if not exists public.grid_fills (
  id    bigint generated always as identity primary key,
  side  text not null,
  price double precision not null,
  qty   double precision not null,
  pnl   double precision not null default 0,
  at    timestamptz not null default now()
);
create index if not exists grid_fills_at_idx on public.grid_fills (at desc);

-- ---------------------------------------------------------------------------
-- allocation: capital allocation (single row, upsert by id)
-- ---------------------------------------------------------------------------
create table if not exists public.allocation (
  id             text primary key default 'default',
  bot_total_usdt double precision not null default 0,
  splits         jsonb not null default '{}'::jsonb,
  updated_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- account_snapshots: REAL balance read from the user's exchange (read-only key)
-- ---------------------------------------------------------------------------
create table if not exists public.account_snapshots (
  id          bigint generated always as identity primary key,
  exchange    text not null,
  total_usdt  double precision not null default 0,
  balances    jsonb not null default '{}'::jsonb,
  at          timestamptz not null default now()
);
create index if not exists account_at_idx on public.account_snapshots (at desc);

-- ---------------------------------------------------------------------------
-- token_market: cached CoinGecko market data (FDV / market cap / supply)
-- ---------------------------------------------------------------------------
create table if not exists public.token_market (
  symbol            text primary key,             -- base asset, e.g. "ON"
  coingecko_id      text,
  name              text,
  market_cap_usd    double precision,
  fdv_usd           double precision,
  circulating_supply double precision,
  total_supply      double precision,
  price_usd         double precision,
  fetched_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Enable RLS on everything. No anon policies => backend (service key) only.
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array[
    'candidates','learning_records','managed_positions','exit_events',
    'equity_history','alerts','grid_state','grid_fills','allocation',
    'account_snapshots','token_market'
  ]
  loop
    execute format('alter table public.%I enable row level security;', t);
  end loop;
end $$;

-- OPTIONAL — only if you want the browser to read live data straight from
-- Supabase with the ANON key (e.g. realtime dashboard). Uncomment per table.
-- Writes stay backend-only (service key); these grant read-only public SELECT.
--
-- drop policy if exists candidates_anon_read on public.candidates;
-- create policy candidates_anon_read on public.candidates for select to anon using (true);
-- drop policy if exists alerts_anon_read on public.alerts;
-- create policy alerts_anon_read on public.alerts for select to anon using (true);

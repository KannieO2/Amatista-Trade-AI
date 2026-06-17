-- ===========================================================================
-- TradeOS AI — Dashboard control + telemetry schema (Supabase / PostgreSQL)
-- Next.js 14 dashboard (Supabase Auth) + GRVT bot + CEX Pump Scanner.
-- Idempotent: safe to re-run in the Supabase SQL Editor.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- bot_controls: UI-driven run state for each engine
-- ---------------------------------------------------------------------------
create table if not exists public.bot_controls (
  id          bigint generated always as identity primary key,
  bot_name    text not null unique,
  is_running  boolean not null default false,
  updated_at  timestamptz not null default now()
);

-- Seed the two engines, OFF by default (no-op if they already exist).
insert into public.bot_controls (bot_name, is_running)
values ('GRVT_BOT', false), ('PUMP_SCANNER', false)
on conflict (bot_name) do nothing;

-- ---------------------------------------------------------------------------
-- bot_logs: passive, async telemetry written by the bots
-- ---------------------------------------------------------------------------
create table if not exists public.bot_logs (
  id         bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  bot_name   text not null,
  status     text not null
             check (status in ('INFO','TRADE_BUY','TRADE_SELL','ERROR','PANIC_SELL')),
  pnl        numeric,
  volumen    numeric,
  message    text
);
create index if not exists bot_logs_created_idx on public.bot_logs (created_at desc);
create index if not exists bot_logs_bot_idx     on public.bot_logs (bot_name, created_at desc);

-- ---------------------------------------------------------------------------
-- pump_candidates: CEX scanner candidates
-- ---------------------------------------------------------------------------
create table if not exists public.pump_candidates (
  id                  bigint generated always as identity primary key,
  created_at          timestamptz not null default now(),
  symbol              text not null,
  exchange            text not null,
  current_spread      numeric,
  volume_acceleration numeric,
  status              text not null default 'MONITORING'
                      check (status in ('MONITORING','TRIGGERED','COMPLETED'))
);
create index if not exists pump_candidates_created_idx on public.pump_candidates (created_at desc);
create index if not exists pump_candidates_status_idx  on public.pump_candidates (status, created_at desc);

-- ===========================================================================
-- Row Level Security
-- Authenticated dashboard users (Supabase Auth) can read AND modify.
-- The server bots use the SERVICE ROLE key, which BYPASSES RLS automatically,
-- so log writes run at full speed without any policy on the hot path.
-- ===========================================================================
alter table public.bot_controls    enable row level security;
alter table public.bot_logs        enable row level security;
alter table public.pump_candidates enable row level security;

drop policy if exists bot_controls_auth_all on public.bot_controls;
create policy bot_controls_auth_all on public.bot_controls
  for all to authenticated using (true) with check (true);

drop policy if exists bot_logs_auth_all on public.bot_logs;
create policy bot_logs_auth_all on public.bot_logs
  for all to authenticated using (true) with check (true);

drop policy if exists pump_candidates_auth_all on public.pump_candidates;
create policy pump_candidates_auth_all on public.pump_candidates
  for all to authenticated using (true) with check (true);

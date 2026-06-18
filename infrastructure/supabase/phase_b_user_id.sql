-- TradeOS AI — Phase B: per-user bot isolation. Idempotent, ADDITIVE.
--
-- Adds user_id to the tables that hold a user's OWN trading state, so each
-- account becomes its own independent bot (own balance/positions/PnL). The
-- shared "brain" tables (candidates, learning_records, alerts, token_market)
-- stay GLOBAL — every account benefits from the same scam-pump intelligence.
--
-- Safe to run before the engine code is switched over: existing rows default to
-- 'owner', so the current single-tenant runtime keeps working unchanged until
-- the per-user engine ships.
--
-- user_id is TEXT (not a uuid FK) on purpose: the bootstrap admin uses the
-- sentinel 'owner', and app_users ids are uuids — text holds both.

-- managed_positions: open/closed positions per user.
alter table public.managed_positions  add column if not exists user_id text not null default 'owner';
create index if not exists idx_managed_positions_user on public.managed_positions(user_id);

-- exit_events: realized exits per user.
alter table public.exit_events        add column if not exists user_id text not null default 'owner';
create index if not exists idx_exit_events_user on public.exit_events(user_id);

-- equity_history: the equity curve per user.
alter table public.equity_history     add column if not exists user_id text not null default 'owner';
create index if not exists idx_equity_history_user on public.equity_history(user_id);

-- account_snapshots: real-account balance snapshots per user.
alter table public.account_snapshots  add column if not exists user_id text not null default 'owner';
create index if not exists idx_account_snapshots_user on public.account_snapshots(user_id);

-- allocation: was a single id='default' row; becomes one row per user. Keep the
-- old singleton working by giving it user_id='owner', and make user_id the
-- conflict target the per-user upserts will use.
alter table public.allocation         add column if not exists user_id text not null default 'owner';
create unique index if not exists uq_allocation_user on public.allocation(user_id);

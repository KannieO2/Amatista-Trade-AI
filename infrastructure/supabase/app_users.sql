-- TradeOS AI — multi-user accounts (app_users). Idempotent: safe to re-run.
--
-- Run this in the Supabase SQL editor once. Each row is an account the admin
-- creates from the dashboard (/admin). Passwords are stored pbkdf2_sha256 by the
-- backend; the table is locked to the service key (no anon/authenticated
-- policies), same as every other table — the browser never reads it directly.
--
-- role: 'admin'  -> can manage users + the single-account grid bot
--       'operator' -> runs their own independent pump bot (Phase B isolation)

create extension if not exists "pgcrypto";  -- gen_random_uuid()

create table if not exists public.app_users (
  id            uuid primary key default gen_random_uuid(),
  username      text unique not null,
  password_hash text not null,
  role          text not null default 'operator',
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);

alter table public.app_users enable row level security;

-- Keep role values sane.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'app_users_role_chk') then
    alter table public.app_users
      add constraint app_users_role_chk check (role in ('admin','operator'));
  end if;
end$$;

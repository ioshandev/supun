-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run
create table if not exists app_data (
  tg_id      bigint      not null,
  k          text        not null,
  v          jsonb,
  updated_at timestamptz not null default now(),
  primary key (tg_id, k)
);
alter table app_data enable row level security;  -- no policies: only the server (service_role key) can read/write

-- Optional: persist registered phones and their tool catalogs across server restarts.
-- Without this table the API keeps catalogs in memory only (phones re-register on
-- sign-in and when the app returns to the foreground). Safe to re-run.

create table if not exists public.devices (
  user_id      uuid not null references public.users (user_id) on delete cascade,
  device_id    text not null,
  platform     text not null default '',
  app_version  text not null default '',
  timezone     text not null default '',
  push_token   text,
  capabilities jsonb not null default '{}'::jsonb,
  tools        jsonb not null default '[]'::jsonb,   -- [{name, description, parameters}]
  updated_at   timestamptz not null default now(),
  primary key (user_id, device_id)
);

-- Only the API (service role) touches this table.
alter table public.devices enable row level security;

-- Baymon v2 tables. Run once in the Supabase SQL Editor. Safe to re-run.
-- Without them the API still works: attachments are kept in server memory (lost on
-- restart) and history search answers that history isn't set up.

-- Shared files: extracted text only (the bytes are never stored), kept 30 days.
create table if not exists public.attachments (
  id         text primary key,                 -- "att_" + 12 hex
  user_id    uuid not null references public.users (user_id) on delete cascade,
  kind       text not null,                    -- image | pdf | text | file
  name       text not null,
  mime       text not null,
  size       integer not null,
  text       text not null default '',
  created_at timestamptz not null default now()
);
create index if not exists attachments_user_created on public.attachments (user_id, created_at desc);
alter table public.attachments enable row level security;  -- API (service role) only

-- What the phone noticed (notifications, screens, calendar, places), for history_search.
create table if not exists public.signal_history (
  id      bigserial primary key,
  user_id uuid not null references public.users (user_id) on delete cascade,
  at      timestamptz not null,
  type    text not null,
  app     text,
  title   text,
  body    text,
  data    jsonb not null default '{}'::jsonb,
  tsv     tsvector generated always as (
            to_tsvector('simple', coalesce(app, '') || ' ' || coalesce(title, '') || ' ' || coalesce(body, ''))
          ) stored
);
create index if not exists signal_history_user_at on public.signal_history (user_id, at desc);
create index if not exists signal_history_tsv on public.signal_history using gin (tsv);
alter table public.signal_history enable row level security;  -- API (service role) only

-- Baymon schema. Safe to re-run (idempotent).
--
-- One row per user; memory is a JSON array of
-- {"id": str, "content": str, "category": str, "created_at": iso8601, "metadata"?: obj}

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  memory jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- The API uses the secret key (bypasses RLS) and does all writes.
-- Signed-in users may read their own row directly; no direct writes.
drop policy if exists "own profile" on public.profiles;
drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles
  for select to authenticated using ((select auth.uid()) = id);

-- Create a profile row for every new auth user.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id) values (new.id) on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Backfill users created before this trigger existed.
insert into public.profiles (id) select id from auth.users on conflict (id) do nothing;

-- Atomic memory operations (avoid lost updates from concurrent read-modify-write).

create or replace function public.append_memory(p_user_id uuid, p_items jsonb)
returns jsonb
language sql
set search_path = ''
as $$
  insert into public.profiles (id, memory) values (p_user_id, p_items)
  on conflict (id) do update
    set memory = public.profiles.memory || excluded.memory, updated_at = now()
  returning memory;
$$;

create or replace function public.update_memory(p_user_id uuid, p_memory_id text, p_patch jsonb)
returns jsonb
language sql
set search_path = ''
as $$
  update public.profiles
     set memory = (
           select jsonb_agg(case when m ->> 'id' = p_memory_id then m || p_patch else m end order by ord)
             from jsonb_array_elements(memory) with ordinality as t(m, ord)
         ),
         updated_at = now()
   where id = p_user_id
     and memory @> jsonb_build_array(jsonb_build_object('id', p_memory_id))
  returning (select m from jsonb_array_elements(memory) as m where m ->> 'id' = p_memory_id);
$$;

create or replace function public.remove_memory(p_user_id uuid, p_memory_id text)
returns boolean
language sql
set search_path = ''
as $$
  update public.profiles
     set memory = coalesce(
           (select jsonb_agg(m order by ord)
              from jsonb_array_elements(memory) with ordinality as t(m, ord)
             where m ->> 'id' <> p_memory_id),
           '[]'::jsonb),
         updated_at = now()
   where id = p_user_id
     and memory @> jsonb_build_array(jsonb_build_object('id', p_memory_id))
  returning true;
$$;

-- Only the API (service_role) may call the memory functions.
revoke execute on function public.append_memory(uuid, jsonb) from public, anon, authenticated;
revoke execute on function public.update_memory(uuid, text, jsonb) from public, anon, authenticated;
revoke execute on function public.remove_memory(uuid, text) from public, anon, authenticated;
grant execute on function public.append_memory(uuid, jsonb) to service_role;
grant execute on function public.update_memory(uuid, text, jsonb) to service_role;
grant execute on function public.remove_memory(uuid, text) to service_role;

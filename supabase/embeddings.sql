-- Vector search for Baymon memories. Run once in the Supabase SQL Editor. Safe to re-run.
-- Embeddings come from the `embed` Edge Function (gte-small, 384 dimensions).

create extension if not exists vector with schema extensions;

create table if not exists public.memory_embeddings (
  user_id      uuid not null references public.users (user_id) on delete cascade,
  memory_id    text not null,          -- id of the item in memory.memory -> 'items'
  content_hash text not null,          -- re-embed when the memory's text changes
  embedding    extensions.vector(384) not null,
  updated_at   timestamptz not null default now(),
  primary key (user_id, memory_id)
);

create index if not exists memory_embeddings_hnsw
  on public.memory_embeddings using hnsw (embedding extensions.vector_cosine_ops);

-- Only the API (service role) touches this table.
alter table public.memory_embeddings enable row level security;

create or replace function public.match_memories(
  p_user_id uuid,
  p_query extensions.vector(384),
  p_count int default 15
)
returns table (memory_id text, similarity float)
language sql
stable
set search_path = public, extensions
as $$
  select e.memory_id, 1 - (e.embedding <=> p_query) as similarity
  from public.memory_embeddings e
  where e.user_id = p_user_id
  order by e.embedding <=> p_query
  limit greatest(least(p_count, 100), 1);
$$;

revoke execute on function public.match_memories(uuid, extensions.vector, int) from public, anon, authenticated;
grant execute on function public.match_memories(uuid, extensions.vector, int) to service_role;

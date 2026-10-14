-- Baymon uses the existing public.memory table (one row per user, FK to users.user_id).
-- No extra functions are needed: writes use optimistic locking on the `version` column.
--
-- Shape of the `memory` JSONB column:
--   {"items": [{"id", "content", "category", "created_at", "updated_at"?, "metadata"?}, ...]}
-- Other top-level keys are preserved by the API.

-- Seed / reset memory for the test user:
insert into public.memory (user_id, memory)
values ('c50f52b0-3069-4b63-a51c-0b1f9269a085', '{"items": []}'::jsonb)
on conflict (user_id) do nothing;

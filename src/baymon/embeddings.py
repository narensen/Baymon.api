"""Relevant-memory retrieval with pgvector.

Vectors come from the ``embed`` Supabase Edge Function (gte-small, 384 dims) and
live in ``public.memory_embeddings`` (see supabase/embeddings.sql). They are
reconciled lazily at retrieval time: new or edited memories are embedded, and
rows for deleted memories are removed, so memory writes need no extra hooks.

Small memory sets skip all of this and are sent whole.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from typing import Any

import httpx

from baymon import services

log = logging.getLogger("baymon.embeddings")

TABLE = "memory_embeddings"
DIMENSIONS = 384
RELEVANT_COUNT = 15  # most similar memories per message
RECENT_COUNT = 5  # always include the newest, for continuity
BATCH = 64  # Edge Function limit per call


class EmbeddingError(Exception):
    pass


def _endpoint() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    secret = os.getenv("BAYMON_EMBED_SECRET", "")
    if not url or not secret:
        raise EmbeddingError("SUPABASE_URL or BAYMON_EMBED_SECRET is not set")
    return f"{url}/functions/v1/embed", secret


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the Edge Function, in batches."""
    url, secret = _endpoint()
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        try:
            resp = httpx.post(url, json={"input": texts[i : i + BATCH]}, headers={"x-embed-secret": secret}, timeout=30)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embed function unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"embed function returned {resp.status_code}: {resp.text[:200]}")
        vectors = resp.json().get("embeddings") or []
        if len(vectors) != len(texts[i : i + BATCH]) or any(len(v) != DIMENSIONS for v in vectors):
            raise EmbeddingError("embed function returned unexpected vectors")
        out.extend(vectors)
    return out


@lru_cache(maxsize=256)
def _embed_query(text: str) -> str:
    """Query vector as a pgvector literal. Cached: agent steps reuse the same message."""
    return json.dumps(embed([text[:2000]])[0])


def _hash(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()[:16]


def sync(user_id: str, items: list[dict[str, Any]]) -> None:
    """Make memory_embeddings match the user's current memories."""
    table = services._supabase().table(TABLE)
    rows = table.select("memory_id, content_hash").eq("user_id", user_id).execute().data or []
    have = {r["memory_id"]: r["content_hash"] for r in rows if isinstance(r, dict)}
    want = {m["id"]: _hash(m["content"]) for m in items if m.get("id") and m.get("content")}

    if stale := [mid for mid in have if mid not in want]:
        table.delete().eq("user_id", user_id).in_("memory_id", stale).execute()

    todo = [m for m in items if m.get("id") in want and have.get(m["id"]) != want[m["id"]]]
    if not todo:
        return
    vectors = embed([m["content"] for m in todo])
    table.upsert([
        {"user_id": user_id, "memory_id": m["id"], "content_hash": want[m["id"]], "embedding": json.dumps(v)}
        for m, v in zip(todo, vectors)
    ]).execute()


def relevant(user_id: str, items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """The memories most relevant to ``query``, plus the most recent ones."""
    if len(items) <= RELEVANT_COUNT + RECENT_COUNT:
        return items
    sync(user_id, items)
    matches = services._supabase().rpc(
        "match_memories", {"p_user_id": user_id, "p_query": _embed_query(query), "p_count": RELEVANT_COUNT}
    ).execute().data or []
    by_id = {m["id"]: m for m in items if m.get("id")}
    chosen = [by_id[r["memory_id"]] for r in matches if r.get("memory_id") in by_id]
    seen = {m["id"] for m in chosen}
    chosen += [m for m in items[-RECENT_COUNT:] if m.get("id") not in seen]
    return chosen

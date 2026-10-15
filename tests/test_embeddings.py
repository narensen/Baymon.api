"""Offline tests for vector memory retrieval (fake Supabase + fake embeddings)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from baymon import embeddings, services


class FakeQuery:
    def __init__(self, db: FakeDB, table: str) -> None:
        self.db, self.table, self.op, self.payload, self.filters = db, table, "select", None, []

    def select(self, *_: Any) -> FakeQuery:
        return self

    def delete(self) -> FakeQuery:
        self.op = "delete"
        return self

    def upsert(self, rows: list[dict[str, Any]]) -> FakeQuery:
        self.op, self.payload = "upsert", rows
        return self

    def eq(self, col: str, val: Any) -> FakeQuery:
        self.filters.append(lambda r: r[col] == val)
        return self

    def in_(self, col: str, vals: list[Any]) -> FakeQuery:
        self.filters.append(lambda r: r[col] in vals)
        return self

    def execute(self) -> Any:
        rows = self.db.rows
        if self.op == "upsert":
            for row in self.payload or []:
                rows[row["memory_id"]] = row
            return type("R", (), {"data": self.payload})
        match = [r for r in rows.values() if all(f(r) for f in self.filters)]
        if self.op == "delete":
            for r in match:
                del rows[r["memory_id"]]
        return type("R", (), {"data": match})


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.rpc_calls: list[dict[str, Any]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        self.rpc_calls.append(params)
        query = json.loads(params["p_query"])
        scored = sorted(self.rows.values(), key=lambda r: -sum(a * b for a, b in zip(json.loads(r["embedding"]), query)))
        data = [{"memory_id": r["memory_id"], "similarity": 0.0} for r in scored[: params["p_count"]]]
        return type("Q", (), {"execute": lambda self: type("R", (), {"data": data})})()


def fake_vector(text: str) -> list[float]:
    """One-hot-ish vectors: texts sharing a keyword point the same way."""
    v = [0.0] * embeddings.DIMENSIONS
    for word in text.lower().split():
        v[hash(word.strip(".,?")) % embeddings.DIMENSIONS] += 1.0
    return v


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    calls: list[list[str]] = []
    monkeypatch.setattr(services, "_supabase", lambda: fake)

    def fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    embeddings._embed_query.cache_clear()
    fake.embed_calls = calls  # type: ignore[attr-defined]
    return fake


def items(n: int) -> list[dict[str, Any]]:
    out = [{"id": f"m{i}", "content": f"User filler fact number {i}.", "category": "fact"} for i in range(n)]
    out[3]["content"] = "User is allergic to peanuts."
    return out


def test_small_memory_skips_vector_search(db: FakeDB) -> None:
    mem = items(10)
    assert embeddings.relevant("u1", mem, "anything") == mem
    assert db.embed_calls == [] and db.rpc_calls == []  # type: ignore[attr-defined]


def test_returns_relevant_plus_recent(db: FakeDB) -> None:
    mem = items(60)
    chosen = embeddings.relevant("u1", mem, "Can I eat peanuts?")
    ids = [m["id"] for m in chosen]
    assert "m3" in ids  # the relevant one, despite being old
    assert ids[-5:] == ["m55", "m56", "m57", "m58", "m59"] or set(ids) >= {"m55", "m56", "m57", "m58", "m59"}
    assert len(chosen) <= embeddings.RELEVANT_COUNT + embeddings.RECENT_COUNT


def test_sync_embeds_only_new_or_changed_and_removes_deleted(db: FakeDB) -> None:
    mem = items(30)
    embeddings.sync("u1", mem)
    assert len(db.rows) == 30
    db.embed_calls.clear()  # type: ignore[attr-defined]

    mem[0]["content"] = "User moved to Berlin."  # edited
    del mem[1]  # deleted
    mem.append({"id": "new", "content": "User started learning guitar.", "category": "task"})  # added
    embeddings.sync("u1", mem)
    assert db.embed_calls == [["User moved to Berlin.", "User started learning guitar."]]  # type: ignore[attr-defined]
    assert "m1" not in db.rows and "new" in db.rows and len(db.rows) == 30


def test_query_vector_is_cached_across_agent_steps(db: FakeDB) -> None:
    mem = items(40)
    embeddings.relevant("u1", mem, "peanuts")
    embeddings.relevant("u1", mem, "peanuts")
    query_calls = [c for c in db.embed_calls if c == ["peanuts"]]  # type: ignore[attr-defined]
    assert len(query_calls) == 1


def test_retrieve_memory_falls_back_when_embeddings_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = items(80)
    monkeypatch.setattr(services, "_load_memory", lambda uid: mem)

    def boom(*_: Any) -> Any:
        raise embeddings.EmbeddingError("function not deployed")

    monkeypatch.setattr(embeddings, "relevant", boom)
    got = services.retrieve_memory("u1", "peanuts")
    assert got == mem[-services.MEMORY_PROMPT_LIMIT:]
    assert services.retrieve_memory("u1") == mem[-services.MEMORY_PROMPT_LIMIT:]

"""Shared offline fixtures: an in-memory stand-in for the user's memory document."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from baymon import services


class MemoryStore:
    """Replaces the Supabase memory row: one document per user."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def load_row(self, user_id: str) -> tuple[dict[str, Any], int] | None:
        doc = self.docs.get(user_id)
        return (copy.deepcopy(doc), 1) if doc is not None else None

    def mutate(self, user_id: str, change):  # type: ignore[no-untyped-def]
        self.docs[user_id] = change(copy.deepcopy(self.docs.get(user_id, {})))
        return self.docs[user_id]


@pytest.fixture
def mem(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    store = MemoryStore()
    monkeypatch.setattr(services, "_load_row", store.load_row)
    monkeypatch.setattr(services, "mutate_doc", store.mutate)
    return store

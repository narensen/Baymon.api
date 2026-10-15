"""Offline tests for the voice_model setting (fake users table)."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from baymon import services
from baymon.services import ServiceError


class FakeUsers:
    def __init__(self, rows: dict[str, dict[str, Any]], missing_column: bool = False) -> None:
        self.rows, self.missing, self.reads = rows, missing_column, 0

    def table(self, name: str) -> FakeUsers:
        assert name == "users"
        self._op, self._update = "select", None
        return self

    def select(self, cols: str) -> FakeUsers:
        return self

    def update(self, values: dict[str, Any]) -> FakeUsers:
        self._op, self._update = "update", values
        return self

    def eq(self, col: str, val: str) -> FakeUsers:
        self._id = val
        return self

    def limit(self, n: int) -> FakeUsers:
        return self

    def execute(self) -> NS:
        if self.missing:
            raise Exception("{'message': 'column users.voice_model does not exist'}")
        row = self.rows.get(self._id)
        if self._op == "update" and row is not None:
            row.update(self._update or {})
        self.reads += self._op == "select"
        return NS(data=[row] if row is not None else [])


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> FakeUsers:
    fake = FakeUsers({"u1": {"user_id": "u1", "voice_model": None}, "u2": {"user_id": "u2", "voice_model": "pirate"}})
    monkeypatch.setattr(services, "_supabase", lambda: fake)
    services._voice_cache.clear()
    return fake


def test_unset_and_unknown_voices_read_as_none(users: FakeUsers) -> None:
    assert services.get_voice_model("u1") is None
    assert services.get_voice_model("u2") is None  # not a real voice -> ask the user again


def test_set_then_get_uses_cache(users: FakeUsers) -> None:
    assert services.set_voice_model("u1", "hannah") == "hannah"
    assert users.rows["u1"]["voice_model"] == "hannah"
    assert services.get_voice_model("u1") == "hannah" and users.reads == 0  # served from cache


def test_set_rejects_unknown_voice_and_unknown_user(users: FakeUsers) -> None:
    with pytest.raises(ServiceError) as bad:
        services.set_voice_model("u1", "pirate")
    assert bad.value.code == "invalid_request"
    with pytest.raises(ServiceError) as nouser:
        services.set_voice_model("nobody", "troy")
    assert nouser.value.code == "user_not_found"


def test_clear_voice(users: FakeUsers) -> None:
    services.set_voice_model("u1", "diana")
    assert services.set_voice_model("u1", None) is None and services.get_voice_model("u1") is None


def test_resolve_voice_order(users: FakeUsers) -> None:
    services.set_voice_model("u1", "autumn")
    assert services.resolve_voice("u1", "daniel") == "daniel"  # explicit request wins
    assert services.resolve_voice("u1") == "autumn"  # then the saved choice
    assert services.resolve_voice("u2") == services.TTS_VOICE  # then the default
    assert services.resolve_voice(None, "nonsense") == services.TTS_VOICE


def test_missing_column_is_a_clear_error_but_speech_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_supabase", lambda: FakeUsers({"u1": {}}, missing_column=True))
    services._voice_cache.clear()
    with pytest.raises(ServiceError) as err:
        services.get_voice_model("u1")
    assert err.value.code == "config_missing" and "voice_model" in err.value.message
    assert services.resolve_voice("u1") == services.TTS_VOICE


def test_every_voice_has_its_preview_clips() -> None:
    from pathlib import Path

    root = Path(services.__file__).parent / "static" / "voice_previews"
    for voice in services.VOICE_IDS:
        for n in range(1, len(services.PREVIEW_LINES) + 1):
            clip = (root / voice / f"{n}.wav").read_bytes()
            assert clip[:4] == b"RIFF" and len(clip) > 10_000, f"{voice}/{n}.wav"


def test_preview_endpoint_serves_cached_clips() -> None:
    from fastapi.testclient import TestClient

    from baymon.api import _previews, app

    client = TestClient(app)
    ok = client.get("/voice-previews/hannah/2.wav")
    assert ok.status_code == 200 and ok.headers["content-type"] == "audio/wav" and ok.content[:4] == b"RIFF"
    assert "max-age" in ok.headers["cache-control"]
    assert client.get("/voice-previews/pirate/1.wav").status_code == 404
    assert client.get("/voice-previews/hannah/9.wav").status_code == 404
    assert [p.url for p in _previews("troy")] == [f"/voice-previews/troy/{n}.wav" for n in range(1, 7)]

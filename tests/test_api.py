"""Integration tests against the real Supabase project and Groq.

Needs BAYMON_TEST_EMAIL / BAYMON_TEST_PASSWORD in .env (a confirmed test user).
Memory tests are skipped until supabase/schema.sql has been applied.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from supabase import create_client

from baymon.api import app

client = TestClient(app)


@pytest.fixture(scope="session")
def auth() -> dict[str, str]:
    email, password = os.getenv("BAYMON_TEST_EMAIL"), os.getenv("BAYMON_TEST_PASSWORD")
    if not (email and password):
        pytest.skip("BAYMON_TEST_EMAIL / BAYMON_TEST_PASSWORD not set")
    # Separate client: signing in must not change the service client's auth header.
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    session = sb.auth.sign_in_with_password({"email": email, "password": password}).session
    assert session is not None
    return {"Authorization": f"Bearer {session.access_token}"}


@pytest.fixture(scope="session")
def schema_ready(auth: dict[str, str]) -> None:
    resp = client.get("/v1/memory", headers=auth)
    if resp.status_code != 200 and "schema cache" in resp.text:
        pytest.skip("Memory table missing: run supabase/schema.sql in the Supabase SQL editor")
    assert resp.status_code == 200, resp.text


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}])
def test_rejects_missing_or_bad_token(headers: dict[str, str]) -> None:
    resp = client.get("/v1/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_me(auth: dict[str, str]) -> None:
    resp = client.get("/v1/me", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["email"] == os.environ["BAYMON_TEST_EMAIL"]


def test_validation_error_shape(auth: dict[str, str]) -> None:
    resp = client.post("/v1/chat", headers=auth, json={"message": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_chat_text(auth: dict[str, str]) -> None:
    resp = client.post("/v1/chat", headers=auth, json={"message": "Reply with just the word: pong"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "pong" in body["text"].lower()
    assert body["audio_base64"] is None


def test_chat_voice_and_mode_switch(auth: dict[str, str]) -> None:
    resp = client.post(
        "/v1/chat",
        headers=auth,
        json={"message": "Please switch me to typing mode.", "voice": True, "mode": "talk"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "type"
    if any(w["code"] == "groq_rate_limit" for w in body["warnings"]):
        pytest.skip("Groq TTS rate limit hit; audio not checked")
    assert body["audio_mime"] == "audio/wav" and body["audio_base64"]


def test_speak_and_transcribe_round_trip(auth: dict[str, str]) -> None:
    speech = client.post("/v1/speak", headers=auth, json={"text": "The quick brown fox.", "voice": "hannah"})
    if speech.status_code == 429:
        pytest.skip(speech.json()["error"]["message"])
    assert speech.status_code == 200
    assert speech.headers["content-type"] == "audio/wav"
    resp = client.post("/v1/transcribe", headers=auth, files={"file": ("fox.wav", speech.content, "audio/wav")})
    assert resp.status_code == 200, resp.text
    assert "fox" in resp.json()["text"].lower()


def test_memory_crud(auth: dict[str, str], schema_ready: None) -> None:
    created = client.post("/v1/memory", headers=auth, json={"content": "Test: likes green tea.", "category": "preference"})
    assert created.status_code == 201, created.text
    item = created.json()
    try:
        ids = [m["id"] for m in client.get("/v1/memory", headers=auth).json()["items"]]
        assert item["id"] in ids

        edited = client.patch(f"/v1/memory/{item['id']}", headers=auth, json={"content": "Test: likes oolong tea."})
        assert edited.status_code == 200, edited.text
        assert edited.json()["content"] == "Test: likes oolong tea."
        assert edited.json()["updated_at"]
    finally:
        assert client.delete(f"/v1/memory/{item['id']}", headers=auth).status_code == 204

    missing = client.delete(f"/v1/memory/{item['id']}", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "memory_not_found"


def test_memory_import(auth: dict[str, str], schema_ready: None) -> None:
    resp = client.post(
        "/v1/memory/import",
        headers=auth,
        json={"text": "About me: I'm allergic to peanuts and I'm learning the guitar."},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    try:
        text = " ".join(m["content"].lower() for m in items)
        assert "peanut" in text and "guitar" in text
    finally:
        for m in items:
            client.delete(f"/v1/memory/{m['id']}", headers=auth)


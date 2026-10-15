"""Integration tests against the real Supabase project and Groq.

Needs BAYMON_TEST_EMAIL / BAYMON_TEST_PASSWORD in .env (a confirmed test user).
Memory tests are skipped until supabase/schema.sql has been applied.
"""

from __future__ import annotations

import json
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



FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "trip.wav")


def _talk(auth: dict[str, str], **form: str):
    with open(FIXTURE, "rb") as f:
        return client.post("/v1/talk", headers=auth, files={"file": ("turn.wav", f, "audio/wav")}, data=form)


def test_talk_turn(auth: dict[str, str]) -> None:
    history = '[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hey! What are you planning?"}]'
    resp = _talk(auth, history=history, tts_voice="hannah")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "mountain" in body["transcript"].lower()
    assert body["text"] and "<" not in body["text"]
    caption = body["caption"]
    assert 0 < len(caption.split()) <= 40
    assert not any(c in caption for c in "<>*`#|") and "http" not in caption
    assert body["mode"] is None
    if body["audio_base64"] is None:
        assert any(w["code"] == "groq_rate_limit" for w in body["warnings"])
    else:
        assert body["audio_mime"] == "audio/wav"


def test_talk_rejects_bad_history(auth: dict[str, str]) -> None:
    resp = _talk(auth, history='[{"role": "system", "content": "x"}]')
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_talk_empty_audio(auth: dict[str, str]) -> None:
    resp = client.post("/v1/talk", headers=auth, files={"file": ("turn.m4a", b"", "audio/mp4")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "audio_empty"


def test_make_caption_limits() -> None:
    from baymon.services import limit_caption, strip_formatting

    assert limit_caption("One two. Three four. Five six.") == "One two. Three four."
    assert limit_caption(" ".join(["w"] * 60)).endswith("…")
    assert len(limit_caption(" ".join(["w"] * 60)).split()) == 40
    assert strip_formatting("**Hi** see [docs](https://x.y)\n<stats>\n[]\n</stats>") == "Hi see docs"


# ------------------------------------------------------------- CLI support


def _creds() -> dict[str, str]:
    email, password = os.getenv("BAYMON_TEST_EMAIL"), os.getenv("BAYMON_TEST_PASSWORD")
    if not (email and password):
        pytest.skip("BAYMON_TEST_EMAIL / BAYMON_TEST_PASSWORD not set")
    return {"email": email, "password": password}


def test_login_and_refresh() -> None:
    resp = client.post("/v1/auth/login", json=_creds())
    assert resp.status_code == 200, resp.text
    session = resp.json()
    assert session["user"]["email"] == os.environ["BAYMON_TEST_EMAIL"] and session["expires_at"]
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert me.status_code == 200
    refreshed = client.post("/v1/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert refreshed.status_code == 200 and refreshed.json()["access_token"]


def test_login_rejects_wrong_password() -> None:
    resp = client.post("/v1/auth/login", json={**_creds(), "password": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


def test_models(auth: dict[str, str]) -> None:
    models = client.get("/v1/models", headers=auth).json()
    keys = {m["key"] for m in models}
    assert {"gpt-oss-120b", "minimax-m2.7"} <= keys
    assert sum(m["default"] for m in models) == 1


def _agent_events(auth: dict[str, str], body: dict) -> list[dict]:
    with client.stream("POST", "/v1/agent/complete", headers=auth, json=body) as resp:
        assert resp.status_code == 200, resp.read()
        return [json.loads(line) for line in resp.iter_lines() if line.strip()]


def test_agent_tool_round_trip(auth: dict[str, str]) -> None:
    tool = {"type": "function", "function": {
        "name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
    messages = [{"role": "user", "content": "What does notes.txt say? Use the read_file tool."}]
    events = _agent_events(auth, {"messages": messages, "tools": [tool], "environment": {"cwd": "/tmp"}})
    calls = next(e["tool_calls"] for e in events if e["type"] == "tool_calls")
    assert calls[0]["function"]["name"] == "read_file" and "notes.txt" in calls[0]["function"]["arguments"]
    assert events[-1]["type"] == "done"

    messages += [
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": "The meeting moved to 4pm."},
    ]
    events = _agent_events(auth, {"messages": messages, "tools": [tool]})
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "4" in text and events[-1]["finish_reason"] == "stop"


def test_agent_unknown_and_unavailable_models(auth: dict[str, str]) -> None:
    body = {"messages": [{"role": "user", "content": "hi"}]}
    bad = client.post("/v1/agent/complete", headers=auth, json={**body, "model": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    mm = client.post("/v1/agent/complete", headers=auth, json={**body, "model": "minimax-m2.7"})
    assert mm.status_code in (200, 403)  # 403 model_unavailable until Groq enables the preview


def test_observe_is_accepted(auth: dict[str, str]) -> None:
    resp = client.post("/v1/memory/observe", headers=auth, json={"user_message": "what's 2+2", "reply": "4"})
    assert resp.status_code == 202 and resp.json() == {"queued": True}


def test_speak_reply_caption(auth: dict[str, str]) -> None:
    long = "Here's the plan:\n\n```python\nprint('x')\n```\n\n" + "More detail here. " * 30
    body = client.post("/v1/speak/reply", headers=auth, json={"text": long}).json()
    assert 0 < len(body["caption"].split()) <= 40 and "```" not in body["caption"]
    if body["audio_base64"] is None:
        assert body["warnings"], "missing audio must come with a warning"


def test_settings_voice_round_trip(auth: dict[str, str]) -> None:
    resp = client.get("/v1/settings", headers=auth)
    if resp.status_code == 500 and resp.json()["error"]["code"] == "config_missing":
        pytest.skip("users.voice_model column not created yet")
    assert resp.status_code == 200, resp.text
    before = resp.json()["voice_model"]
    assert {v["id"] for v in resp.json()["voices"]} == {"troy", "austin", "daniel", "autumn", "diana", "hannah"}
    try:
        saved = client.patch("/v1/settings", headers=auth, json={"voice_model": "hannah"}).json()
        assert saved["voice_model"] == "hannah"
        assert client.get("/v1/settings", headers=auth).json()["voice_model"] == "hannah"
        bad = client.patch("/v1/settings", headers=auth, json={"voice_model": "pirate"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    finally:
        client.patch("/v1/settings", headers=auth, json={"voice_model": before})

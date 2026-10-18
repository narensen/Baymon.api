"""Offline tests for API v2 (docs/api-v2.md in the app repo): triggers, routines, drafts,
feedback reasons, settings, attachments, web_read, history, commitments, digest, weekly review."""

from __future__ import annotations

import http.server
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from baymon import attachments, automations, history, nudges, phone_agent, server_tools, services
from baymon.api import NudgeFeedback, User, app, current_user
from baymon.automations import ToolFailure
from baymon.devices import Device
from baymon.services import ServiceError

UID = "u1"
SUNDAY_2PM = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)  # ISO weekday 7


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def api(mem, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # type: ignore[no-untyped-def]
    app.dependency_overrides[current_user] = lambda: User(id=UID, email="t@example.com")
    monkeypatch.setattr(services, "get_voice_model", lambda uid: None)
    monkeypatch.setattr(attachments, "_table_missing", True)
    monkeypatch.setattr(history, "_table_missing", True)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    state = {"now": SUNDAY_2PM}
    monkeypatch.setattr(nudges, "_now", lambda: state["now"])
    monkeypatch.setattr(automations, "_now", lambda: state["now"])
    return state


@pytest.fixture
def brain(mem, clock, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """nudges.check with a scripted decision model; returns (replies, contexts)."""
    replies: list[Any] = []
    contexts: list[str] = []

    def ask(context: str) -> Any:
        contexts.append(context)
        return replies.pop(0) if replies else {"nudges": [], "commitments": []}

    monkeypatch.setattr(nudges, "_ask_model", ask)
    monkeypatch.setattr(history, "_table_missing", True)
    mem.docs[UID] = {"items": [], "assistant": {"settings": {
        "quiet_start": None, "quiet_end": None, "morning_brief": None, "evening_wrap": None, "weekly_review": None}}}
    return replies, contexts


def notif(title: str, text: str, app: str = "WhatsApp", **extra: Any) -> dict[str, Any]:
    return {"type": "notification", "data": {"app": app, "title": title, "text": text, **extra}}


def settings(mem, **changes: Any) -> None:  # type: ignore[no-untyped-def]
    mem.docs[UID]["assistant"]["settings"].update(changes)


# ------------------------------------------------------------- 1. feedback


def test_feedback_reason_is_stored_and_shown(brain, mem, clock) -> None:  # type: ignore[no-untyped-def]
    replies, contexts = brain
    replies.append({"nudges": [{"kind": "insight", "topic": "x", "text": "A thought"}]})
    nid = nudges.check(UID, [notif("Ravi", "hi")], "UTC")[0]["id"]
    nudges.feedback(UID, nid, "dismissed", reason="not_useful")
    entry = next(n for n in mem.docs[UID]["assistant"]["log"] if n["id"] == nid)
    assert entry["status"] == "dismissed" and entry["reason"] == "not_useful"
    nudges.check(UID, [notif("Ravi", "again")], "UTC")
    assert "→ dismissed (not useful)" in contexts[-1] and "insight: 1× not useful" in contexts[-1]


def test_unknown_feedback_reason_rejected(brain) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        NudgeFeedback(action="dismissed", reason="meh")  # type: ignore[arg-type]
    with pytest.raises(ServiceError):
        nudges.feedback(UID, "ndg_x", "dismissed", reason="meh")


# ------------------------------------------------------------- 2. settings


def test_settings_new_keys_round_trip(api) -> None:  # type: ignore[no-untyped-def]
    got = api.get("/v1/settings").json()
    assert got["history"] == {"retention_days": 90}
    assert set(got["nudges"]) == {"enabled", "quiet_start", "quiet_end", "morning_brief", "evening_wrap",
                                  "digest", "weekly_review", "weekly_review_day"}
    assert got["nudges"]["digest"] is True and got["nudges"]["weekly_review_day"] == 7
    r = api.patch("/v1/settings", json={"nudges": {"digest": False, "weekly_review": None, "weekly_review_day": 6}})
    assert r.status_code == 200, r.text
    n = r.json()["nudges"]
    assert n["digest"] is False and n["weekly_review"] is None and n["weekly_review_day"] == 6
    assert n["quiet_start"] == "22:00"  # untouched
    assert api.patch("/v1/settings", json={"nudges": {"weekly_review_day": 8}}).status_code == 422
    assert api.patch("/v1/settings", json={"history": {"retention_days": 30}}).json()["history"]["retention_days"] == 30
    assert api.get("/v1/settings").json()["history"]["retention_days"] == 30
    assert api.patch("/v1/settings", json={"history": {"retention_days": 7}}).status_code == 422


# --------------------------------------------------------------- 4. drafts


def test_draft_reply_key_only_from_real_replyable_notification(brain) -> None:  # type: ignore[no-untyped-def]
    replies, _ = brain
    replies.append({"nudges": [
        {"kind": "reminder", "topic": "a", "text": "Priya asked", "draft": {"text": "Yes!", "to": "Priya", "reply_key": "k1"}},
        {"kind": "reminder", "topic": "b", "text": "Ravi asked", "draft": {"text": "Sure", "reply_key": "forged"}},
        {"kind": "reminder", "topic": "c", "text": "Empty", "draft": {"text": "  "}},
    ]})
    out = nudges.check(UID, [notif("Priya", "Joining at 5?", key="k1", can_reply=True),
                             notif("Ravi", "lunch?", key="k2", can_reply=False)], "UTC")
    drafts = {n["text"]: n["draft"] for n in out}
    assert drafts["Priya asked"] == {"text": "Yes!", "to": "Priya", "app": None, "reply_key": "k1"}
    assert drafts["Ravi asked"]["reply_key"] is None
    assert drafts["Empty"] is None
    assert {n["text"]: n["draft"] for n in nudges.active(UID)}["Priya asked"]["reply_key"] == "k1"


def test_long_signal_keeps_reply_key(brain) -> None:  # type: ignore[no-untyped-def]
    replies, _ = brain
    replies.append({"nudges": [{"kind": "reminder", "topic": "a", "text": "x", "draft": {"text": "ok", "reply_key": "kk"}}]})
    out = nudges.check(UID, [notif("Priya", "long " * 1000, key="kk", can_reply=True)], "UTC")
    assert out[0]["draft"]["reply_key"] == "kk"


# ------------------------------------------------------------- 6. triggers


@pytest.mark.parametrize("when,signal,expected", [
    ({"type": "place", "place": "Office", "on": "arrive"}, {"type": "location", "data": {"label": "office", "event": "enter", "saved": True}}, True),
    ({"type": "place", "place": "Office", "on": "arrive"}, {"type": "location", "data": {"label": "Office", "event": "exit", "saved": True}}, False),
    ({"type": "place", "place": "Office", "on": "leave"}, {"type": "location", "data": {"label": "Office", "event": "exit", "saved": True}}, True),
    ({"type": "place", "place": "Office", "on": "arrive"}, {"type": "location", "data": {"label": "Office", "event": "enter"}}, False),
    ({"type": "message", "from": "Priya", "app": None}, notif("Priya Sharma", "hey"), True),
    ({"type": "message", "from": "Priya", "app": None}, notif("Family group", "Priya: dinner?"), True),
    ({"type": "message", "from": "Priya", "app": "WhatsApp"}, notif("Priya", "hey", app="Telegram"), False),
    ({"type": "message", "from": "Priya", "app": None}, notif("Ravi", "about Priya"), False),
    ({"type": "app", "app": "Swiggy"}, {"type": "app_open", "data": {"app": "Swiggy", "package": "in.swiggy.android"}}, True),
    ({"type": "app", "app": "in.swiggy.android"}, {"type": "app_open", "data": {"app": "Swiggy", "package": "in.swiggy.android"}}, True),
    ({"type": "app", "app": "Swiggy"}, {"type": "app_open", "data": {"app": "Zomato", "package": "com.zomato"}}, False),
    ({"type": "notification", "app": "Slack", "contains": "deploy"}, notif("#eng", "Deploy finished", app="Slack"), True),
    ({"type": "notification", "app": "Slack", "contains": "deploy"}, notif("#eng", "lunch", app="Slack"), False),
    ({"type": "notification", "app": None, "contains": "OTP"}, notif("Bank", "Your otp is 1234", app="Messages"), True),
])
def test_trigger_matching(when: dict, signal: dict, expected: bool) -> None:
    assert automations.matches(when, signal) is expected


def test_trigger_validation() -> None:
    with pytest.raises(ToolFailure):
        automations.validate_when({"type": "weather"})
    with pytest.raises(ToolFailure):
        automations.validate_when({"type": "notification"})
    assert automations.validate_when({"type": "message", "from": " Priya "})["from"] == "Priya"


def test_once_trigger_fires_with_fallback_and_is_deleted(brain, mem) -> None:  # type: ignore[no-untyped-def]
    replies, contexts = brain
    t = automations.create_trigger(UID, {"type": "message", "from": "Priya", "app": "WhatsApp"}, "Ask Priya about the invoice.")
    out = nudges.check(UID, [notif("Priya", "hi!", key="k", can_reply=True)], "UTC")  # model returns nothing
    assert len(out) == 1 and out[0]["text"] == "Ask Priya about the invoice."
    assert "You asked me to remind you when Priya messages you on WhatsApp" in out[0]["detail"]
    assert out[0]["suggestions"][:2] == ["Done", "Remind me in an hour"]
    assert f"id {t['id']}" in contexts[-1]
    assert automations.list_triggers(UID) == []  # one-off: gone


def test_triggers_bypass_quiet_hours_repeat_window_and_digest(brain, mem, clock) -> None:  # type: ignore[no-untyped-def]
    replies, _ = brain
    settings(mem, quiet_start="00:00", quiet_end="23:59", evening_wrap="21:00", digest=True)
    t = automations.create_trigger(UID, {"type": "app", "app": "Swiggy"}, "Use the coupon.", repeat=True)
    topic = f"trigger:{t['id']}"
    for _ in range(2):
        replies.append({"nudges": [{"kind": "reminder", "urgency": "low", "topic": topic, "text": "Coupon time!"},
                                   {"kind": "insight", "urgency": "normal", "topic": "noise", "text": "Not now"}]})
        out = nudges.check(UID, [{"type": "app_open", "data": {"app": "Swiggy"}}], "UTC")
        assert [n["text"] for n in out] == ["Coupon time!"]  # quiet hours drop the other one
        clock["now"] += timedelta(minutes=11)  # past the repeat cooldown
    assert automations.list_triggers(UID)[0]["last_fired_at"] is not None


def test_repeat_trigger_cooldown(brain, clock) -> None:  # type: ignore[no-untyped-def]
    automations.create_trigger(UID, {"type": "notification", "app": "Slack", "contains": "deploy"}, "Check the deploy.", repeat=True)
    assert len(nudges.check(UID, [notif("#eng", "deploy 1", app="Slack")], "UTC")) == 1
    clock["now"] += timedelta(minutes=5)
    assert nudges.check(UID, [notif("#eng", "deploy 2", app="Slack")], "UTC") == []
    clock["now"] += timedelta(minutes=6)
    assert len(nudges.check(UID, [notif("#eng", "deploy 3", app="Slack")], "UTC")) == 1


def test_trigger_limit_and_routine_offer(brain, mem) -> None:  # type: ignore[no-untyped-def]
    automations.save_routine(UID, "Leaving work", ["Text Home on my way"])
    t = automations.create_trigger(UID, {"type": "place", "place": "Office", "on": "leave"}, "Head home?", routine="leaving work")
    out = nudges.check(UID, [{"type": "location", "data": {"label": "Office", "event": "exit", "saved": True}}], "UTC")
    assert "Run Leaving work" in out[0]["suggestions"]
    with pytest.raises(ToolFailure) as missing:
        automations.create_trigger(UID, {"type": "app", "app": "X"}, "x", routine="nope")
    assert missing.value.code == "not_found"
    mem.docs[UID]["assistant"]["triggers"] = [{**t, "id": f"trg_{i}"} for i in range(automations.MAX_TRIGGERS)]
    with pytest.raises(ToolFailure) as full:
        automations.create_trigger(UID, {"type": "app", "app": "X"}, "x")
    assert full.value.code == "limit_reached"


def test_trigger_endpoints(api, mem) -> None:  # type: ignore[no-untyped-def]
    t = automations.create_trigger(UID, {"type": "message", "from": "Priya", "app": None}, "Ask about the invoice.")
    items = api.get("/v1/triggers").json()["items"]
    assert items[0]["when"] == {"type": "message", "from": "Priya", "app": None}  # "from", not "from_"
    assert set(items[0]) == {"id", "when", "say", "repeat", "routine_id", "created_at", "last_fired_at"}
    assert api.delete(f"/v1/triggers/{t['id']}").status_code == 204
    gone = api.delete(f"/v1/triggers/{t['id']}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "trigger_not_found"
    assert api.get("/v1/triggers").json() == {"items": []}


# ------------------------------------------------------------- 7. routines


def test_routines_upsert_run_delete(api, mem) -> None:  # type: ignore[no-untyped-def]
    a = automations.save_routine(UID, "Leaving work", ["Text Home", "Open Maps"])
    b = automations.save_routine(UID, "leaving WORK", ["Turn on DND"])
    assert a["id"] == b["id"] and b["steps"] == ["Turn on DND"] and len(automations.list_routines(UID)) == 1
    automations.save_routine(UID, "Arrive home", ["Lights on"])
    assert [r["name"] for r in api.get("/v1/routines").json()["items"]] == ["Arrive home", "leaving WORK"]

    no_phone = server_tools.run("routine_run", {"name": "leaving work"}, server_tools.ToolContext(UID))
    assert no_phone["error"]["code"] == "not_supported"
    ran = server_tools.run("routine_run", {"name": "Leaving Work"}, server_tools.ToolContext(UID, device=object()))
    assert ran["steps"] == ["Turn on DND"] and "phone tools" in ran["instruction"]
    assert automations.find_routine(UID, "leaving work")["last_run_at"] is not None

    t = automations.create_trigger(UID, {"type": "app", "app": "Maps"}, "x", routine="Leaving work")
    assert api.delete(f"/v1/routines/{a['id']}").status_code == 204
    assert automations.list_triggers(UID)[0]["routine_id"] is None and t["routine_id"] == a["id"]
    assert api.delete(f"/v1/routines/{a['id']}").json()["error"]["code"] == "routine_not_found"


# --------------------------------------------------------- 5. server tools


class FakeModel:
    def __init__(self, script: list[NS]) -> None:
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs: Any) -> NS:
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return NS(choices=[NS(message=self.script.pop(0))])


def tool_call(name: str, args: dict, cid: str) -> NS:
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)), type="function")


def reply(content: str | None = None, *calls: NS) -> NS:
    msg = NS(content=content, tool_calls=list(calls) or None)
    msg.model_dump = lambda include=None, exclude_none=True: {  # type: ignore[attr-defined]
        "role": "assistant", "content": content,
        **({"tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name,
                                                                         "arguments": c.function.arguments}} for c in calls]}
           if calls else {})}
    return msg


def test_server_tools_in_generate_over_several_rounds(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeModel([
        reply(None, tool_call("routine_save", {"name": "Gym", "steps": ["Start playlist"]}, "c1")),
        reply(None, tool_call("trigger_create", {"when": {"type": "place", "place": "Gym", "on": "arrive"},
                                                  "say": "Start the playlist.", "routine": "Gym"}, "c2")),
        reply("Done — I'll remind you at the gym."),
    ])
    monkeypatch.setattr(services, "_groq", lambda: fake)
    text, _ = services._generate("Remind me at the gym to start my playlist", {"memory": []}, UID)
    assert text.startswith("Done")
    created = json.loads(fake.requests[2]["messages"][-1]["content"])
    assert created["created"]["when"]["place"] == "Gym" and "location_places" in created["note"]
    assert automations.list_triggers(UID)[0]["routine_id"] == automations.find_routine(UID, "gym")["id"]
    assert any(t["function"]["name"] == "history_search" for t in fake.requests[0]["tools"])


def test_generate_stops_after_round_limit(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    script = [reply(None, tool_call("trigger_list", {}, f"c{i}")) for i in range(services.SERVER_TOOL_ROUNDS)]
    fake = FakeModel(script + [reply("Here are your reminders.")])
    monkeypatch.setattr(services, "_groq", lambda: fake)
    text, _ = services._generate("list", {"memory": []}, UID)
    assert text == "Here are your reminders." and fake.requests[-1]["tool_choice"] == "none"


def test_server_tools_in_phone_loop(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    device = Device(UID, "p1", timezone="Asia/Kolkata", capabilities={"screen_control": True},
                    tools=[{"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}
                           for n in ("screen_read", "sms_compose", "maps_directions")])
    automations.save_routine(UID, "Leaving work", ["Text Home on my way", "Open Maps home"])
    fake = FakeModel([
        reply(None, tool_call("routine_run", {"name": "leaving work"}, "c1")),
        reply(None, tool_call("sms_compose", {"to": "Home"}, "c2")),
    ])
    monkeypatch.setattr(services, "_groq", lambda: fake)
    monkeypatch.setattr(services, "build_system_prompt", lambda *a, **k: "SYSTEM")
    routed: list[str] = []

    def route(msg: str, hist: list, available: set) -> set:
        routed.append(msg)
        return set() if len(routed) == 1 else {"communication", "web"}

    monkeypatch.setattr(phone_agent, "route", route)
    step = phone_agent.start(UID, device, "Run my “Leaving work” routine.", [], None)
    assert step.actions[0].tool == "sms_compose"  # routine steps were run with phone tools
    assert "Text Home on my way" in routed[1]  # its steps were routed to load the right tools
    names = {t["function"]["name"] for t in fake.requests[1]["tools"]}
    assert {"sms_compose", "maps_directions", "routine_run"} <= names
    assert json.loads(fake.requests[1]["messages"][-1]["content"])["run"] == "Leaving work"


# ----------------------------------------------------------- 8. attachments


def make_pdf(text: str) -> bytes:
    content = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_attachment_kinds_and_extraction(api, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import io

    from PIL import Image

    vision = FakeModel([reply("<think>hmm</think>IndiGo 6E 5123\n\nA boarding pass for BLR to DEL.")])
    monkeypatch.setattr(services, "_groq", lambda: vision)
    img = io.BytesIO()
    Image.new("RGB", (3000, 1000), "white").save(img, "PNG")
    r = api.post("/v1/attachments", files={"file": ("1739-pass.png", img.getvalue(), "image/png")},
                 data={"name": "boarding-pass.png", "mime": "image/png"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "image" and body["name"] == "boarding-pass.png" and body["id"].startswith("att_")
    assert body["text"] == "IndiGo 6E 5123\n\nA boarding pass for BLR to DEL."
    sent = vision.requests[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert sent.startswith("data:image/jpeg;base64,")

    pdf = api.post("/v1/attachments", files={"file": ("x.pdf", make_pdf("Invoice total 4200 INR"), "application/octet-stream")},
                   data={"name": "invoice.pdf"}).json()
    assert pdf["kind"] == "pdf" and pdf["mime"] == "application/pdf" and "Invoice total 4200" in pdf["text"]
    txt = api.post("/v1/attachments", files={"file": ("notes.txt", b"buy milk", "text/plain")}).json()
    assert txt["kind"] == "text" and txt["text"] == "buy milk"
    bin_ = api.post("/v1/attachments", files={"file": ("a.bin", b"\x00\x01\x02", "application/octet-stream")}).json()
    assert bin_["kind"] == "file" and bin_["text"] == ""
    assert set(body) == {"id", "kind", "name", "mime", "size", "text", "created_at"}


def test_attachment_size_limit(api, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(attachments, "MAX_BYTES", 10)
    r = api.post("/v1/attachments", files={"file": ("big.txt", b"x" * 11, "text/plain")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


def test_chat_checks_attachment_ownership_and_caps_text(api, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    mine = attachments.save(UID, b"A" * 50, "a.txt", "text/plain")
    theirs = attachments.save("someone-else", b"secret", "b.txt", "text/plain")
    r = api.post("/v1/chat", json={"message": "what is this", "attachments": [theirs["id"]]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "attachment_not_found"

    monkeypatch.setattr(attachments, "CHAT_TEXT_CAP", 20)
    injected = attachments.inject("Summarise", attachments.get_many(UID, [mine["id"]]))
    assert injected.startswith("Summarise\n\n[Attached: a.txt (text)]\n<<<\n" + "A" * 20)
    assert "[… truncated]" in injected


# --------------------------------------------------------------- web_read


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/slow":
            time.sleep(1.5)
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret")
            self.end_headers()
            return
        body = (b"<html><head><title>Post title</title><script>evil()</script></head><body><nav>Menu</nav>"
                b"<article><p>Main text here.</p></article><footer>foot</footer></body></html>")
        if self.path == "/big":
            body = b"<html><body><article>" + b"x" * 5000 + b"</article></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site():  # type: ignore[no-untyped-def]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_web_read_refuses_private_and_non_http(site) -> None:  # type: ignore[no-untyped-def]
    for url in (site + "/", "http://localhost/", "http://10.0.0.5/", "file:///etc/passwd", "ftp://example.com"):
        with pytest.raises(ToolFailure) as err:
            server_tools.web_read(url)
        assert err.value.code == "invalid_args"


def test_web_read_extracts_and_limits(site, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    public = {"127.0.0.1"}
    monkeypatch.setattr(server_tools, "_public_host", lambda host: host in public)
    page = server_tools.web_read(site + "/post")
    assert page["title"] == "Post title" and page["text"] == "Main text here."
    monkeypatch.setattr(server_tools, "WEB_MAX_BYTES", 1000)
    assert len(server_tools.web_read(site + "/big")["text"]) < 1000
    monkeypatch.setattr(server_tools, "WEB_TIMEOUT", 0.5)
    with pytest.raises(ToolFailure) as slow:
        server_tools.web_read(site + "/slow")
    assert slow.value.code == "timeout"


# ---------------------------------------------------------------- history


def test_history_row_mapping() -> None:
    rows = {s["type"]: history.row_for(UID, {**s, "at": "2026-10-18T10:00:00+05:30"}) for s in [
        notif("Arjun", "Send the Q3 report", app="Slack"),
        {"type": "screen", "data": {"app": "Swiggy", "text": "Order #1"}},
        {"type": "calendar", "data": {"title": "Team sync", "start": "17:00", "location": "Meet", "change": "changed"}},
        {"type": "location", "data": {"label": "Office", "event": "enter", "saved": True}},
        {"type": "app_open", "data": {"app": "Swiggy"}},
    ]}
    assert (rows["notification"]["app"], rows["notification"]["title"], rows["notification"]["body"]) == ("Slack", "Arjun", "Send the Q3 report")
    assert (rows["screen"]["app"], rows["screen"]["body"]) == ("Swiggy", "Order #1")
    assert rows["calendar"]["title"] == "Team sync" and "Meet" in rows["calendar"]["body"]
    assert (rows["location"]["title"], rows["location"]["body"]) == ("Office", "enter")
    assert rows["app_open"]["app"] == "Swiggy"
    assert history.row_for(UID, {"type": "battery", "data": {"level": 5}}) is None
    assert history.row_for(UID, {"type": "foreground", "data": {}}) is None


class Recorder:
    """Fake Supabase table: records the PostgREST call chain."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.calls: list[tuple] = []
        self.rows = rows or []

    def table(self, name: str) -> Recorder:
        self.calls.append(("table", name))
        return self

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def record(*args: Any, **kwargs: Any) -> Recorder:
            self.calls.append((name, *args))
            return self
        return record

    def execute(self) -> NS:
        return NS(data=self.rows)


def test_history_writes_nothing_when_off(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder()
    monkeypatch.setattr(services, "_supabase", lambda: rec)
    monkeypatch.setattr(history, "_table_missing", False)
    assert history.record(UID, [notif("A", "b")], 0) == 0 and rec.calls == []
    assert history.record(UID, [notif("A", "b"), {"type": "battery", "data": {}}], 90) == 1
    assert ("insert", [history.row_for(UID, notif("A", "b"))]) in rec.calls


def test_history_search_filters_and_local_times(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder([{"at": "2026-10-14T10:52:05+00:00", "type": "notification", "app": "Slack", "title": "Arjun",
                     "body": "Can you send the Q3 report?"}])
    monkeypatch.setattr(services, "_supabase", lambda: rec)
    monkeypatch.setattr(history, "_table_missing", False)
    out = history.search(UID, "Asia/Kolkata", query="report", person="Arjun, (x)", app="Slack",
                         since="2026-10-01T00:00:00+05:30", limit=99)
    assert out["results"][0]["at"] == "2026-10-14T16:22:05+05:30" and out["retention_days"] == 90
    assert out["results"][0]["text"] == "Can you send the Q3 report?"
    assert ("filter", "tsv", "wfts(simple)", "report") in rec.calls
    assert ("or_", "title.ilike.*Arjun    x*,body.ilike.*Arjun    x*") in rec.calls  # no filter-syntax injection
    assert ("ilike", "app", "%Slack%") in rec.calls and ("limit", 30) in rec.calls
    history.update_settings(UID, 0)
    assert history.search(UID, None, query="x") == {"results": [], "retention_days": 0, "note": "History is off in Settings."}


def test_delete_history_endpoint(api, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder()
    monkeypatch.setattr(services, "_supabase", lambda: rec)
    monkeypatch.setattr(history, "_table_missing", False)
    assert api.delete("/v1/history").status_code == 204
    assert ("delete",) in rec.calls and ("eq", "user_id", UID) in rec.calls


# ------------------------------------------------------------ commitments


def test_commitment_extraction_builds_metadata(mem, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    prompts: list[str] = []

    def chat_json(system: str, user: str) -> dict:
        prompts.append(user)
        return {"memories": [
            {"content": "User will send the report to Arjun by 6pm.", "category": "other", "commitment": True,
             "due_at": "2026-10-18T18:00:00+05:30", "person": "Arjun"},
            {"content": "User likes chai.", "category": "preference"},
        ]}

    monkeypatch.setattr(services, "_chat_json", chat_json)
    items = services.extract_memories(UID, "User: I'll send it to Arjun by 6", {"source": "text"}, "Asia/Kolkata")
    assert prompts[0].startswith("Now: ") and "(Asia/Kolkata)" in prompts[0]
    commitment = items[0]
    assert commitment["category"] == "task"
    assert commitment["metadata"] == {"kind": "commitment", "status": "open", "due_at": "2026-10-18T18:00:00+05:30",
                                      "person": "Arjun", "source": "chat"}
    assert items[1]["metadata"] == {"source": "text"}


def test_patch_memory_metadata_only_merges(api, mem) -> None:  # type: ignore[no-untyped-def]
    item = services.add_memory(UID, "User will call the bank.", "task",
                               {"kind": "commitment", "status": "open", "person": None, "source": "chat"})
    r = api.patch(f"/v1/memory/{item['id']}", json={"metadata": {"status": "done"}})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"] == {"kind": "commitment", "status": "done", "person": None, "source": "chat"}
    assert r.json()["updated_at"] and r.json()["content"] == "User will call the bank."
    assert api.patch(f"/v1/memory/{item['id']}", json={}).json()["error"]["code"] == "invalid_memory"


def test_overdue_commitments_reach_the_nudge_model(brain, mem) -> None:  # type: ignore[no-untyped-def]
    _, contexts = brain
    services.add_memory(UID, "User will send the deck to Ravi.", "task",
                        {"kind": "commitment", "status": "open", "due_at": "2026-10-18T09:00:00+00:00", "person": "Ravi"})
    services.add_memory(UID, "User will water the plants.", "task", {"kind": "commitment", "status": "done"})
    nudges.check(UID, [], "UTC")
    assert "OVERDUE since" in contexts[-1] and "deck to Ravi" in contexts[-1] and "with Ravi" in contexts[-1]
    assert "water the plants" not in contexts[-1].split("Open commitments:")[1].split("\n\n")[0]


def test_model_commitments_from_signals_are_stored(brain, mem) -> None:  # type: ignore[no-untyped-def]
    replies, _ = brain
    commitment = {"content": "User will send the slides to Meera.", "due_at": None, "person": "Meera", "source": "screen"}
    replies.append({"nudges": [], "commitments": [commitment, commitment]})
    nudges.check(UID, [{"type": "screen", "data": {"app": "WhatsApp", "text": "Me: I'll send the slides tonight"}}], "UTC")
    stored = [m for m in services.list_memory(UID) if "slides" in m["content"]]
    assert len(stored) == 1 and stored[0]["metadata"]["source"] == "screen" and stored[0]["category"] == "task"


# ------------------------------------------------- 11. digest & weekly review


def test_digest_holds_low_urgency_until_the_wrap(brain, mem, clock) -> None:  # type: ignore[no-untyped-def]
    replies, contexts = brain
    settings(mem, evening_wrap="21:00", digest=True)
    replies.append({"nudges": [{"kind": "insight", "urgency": "low", "topic": "steps", "text": "You walked a lot today."},
                               {"kind": "event", "urgency": "normal", "topic": "sync", "text": "Sync at 5."}]})
    out = nudges.check(UID, [notif("Cal", "sync")], "UTC")
    assert [n["text"] for n in out] == ["Sync at 5."]
    assert any(n["status"] == "held" for n in mem.docs[UID]["assistant"]["log"])
    clock["now"] = SUNDAY_2PM.replace(hour=21, minute=5)
    nudges.check(UID, [], "UTC")
    assert "Held for the wrap-up" in contexts[-1] and "You walked a lot today." in contexts[-1]
    assert [n["status"] for n in mem.docs[UID]["assistant"]["log"] if n["topic"] == "steps"] == ["digested"]


def test_digest_off_returns_low_urgency_now(brain, mem) -> None:  # type: ignore[no-untyped-def]
    replies, _ = brain
    settings(mem, evening_wrap="21:00", digest=False)
    replies.append({"nudges": [{"kind": "insight", "urgency": "low", "topic": "x", "text": "Low but shown"}]})
    assert [n["text"] for n in nudges.check(UID, [notif("a", "b")], "UTC")] == ["Low but shown"]


def test_weekly_review_due_once_per_iso_week(brain, mem, clock) -> None:  # type: ignore[no-untyped-def]
    replies, contexts = brain
    s = {"weekly_review": "18:00", "weekly_review_day": 7}
    sunday_6pm = datetime(2026, 10, 18, 18, 30)
    assert "weekly review" in nudges.due_checkins(sunday_6pm, s, {})
    assert "weekly review" not in nudges.due_checkins(sunday_6pm, s, {"weekly_review": "2026-W42"})
    assert "weekly review" not in nudges.due_checkins(datetime(2026, 10, 18, 17, 0), s, {})
    assert "weekly review" not in nudges.due_checkins(datetime(2026, 10, 19, 18, 30), s, {})

    settings(mem, weekly_review="18:00", weekly_review_day=7)
    clock["now"] = SUNDAY_2PM.replace(hour=18, minute=30)
    nudges.check(UID, [], "UTC")
    assert "For the weekly review:" in contexts[-1]
    assert mem.docs[UID]["assistant"]["checkins"]["weekly_review"] == "2026-W42"
    clock["now"] += timedelta(hours=4)
    nudges.check(UID, [], "UTC")
    assert "Check-ins due now: none" in contexts[-1]

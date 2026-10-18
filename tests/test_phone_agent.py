"""Offline tests for the phone tool loop, with a scripted fake model."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from baymon import phone_agent, server_tools, services
from baymon.devices import Device
from baymon.services import ServiceError


def call(name: str, args: Any, cid: str) -> NS:
    raw = args if isinstance(args, str) else json.dumps(args)
    return NS(id=cid, function=NS(name=name, arguments=raw))


class FakeModel:
    """Returns scripted assistant messages and records what it was sent."""

    def __init__(self, script: list[NS]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs: Any) -> NS:
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return NS(choices=[NS(message=self.script.pop(0))])


def msg(content: str | None = None, *calls: NS) -> NS:
    return NS(content=content, tool_calls=list(calls) or None)


@pytest.fixture
def device() -> Device:
    tools = [{"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}
             for n in ("screen_read", "screen_tap", "app_install", "alarm_set", "notifications_list")]
    return Device("u1", "phone-1", capabilities={"screen_control": True, "notification_access": False}, tools=tools)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(script: list[NS]) -> FakeModel:
        fake = FakeModel(script)
        monkeypatch.setattr(services, "_groq", lambda: fake)
        return fake

    monkeypatch.setattr(services, "build_system_prompt", lambda *a, **k: "SYSTEM")
    monkeypatch.setattr(phone_agent, "route", lambda msg, hist, available: set(available))  # all groups
    return install


def test_actions_then_results_then_final(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    fake = model([
        msg("Opening the Play Store.", call("app_install", {"app": "Instagram"}, "c1")),
        msg(None, call("screen_read", {}, "c2")),
        msg("Installed."),
    ])
    step = phone_agent.start("u1", device, "Install Instagram", [], None)
    assert not step.final and [(a.id, a.tool, a.args) for a in step.actions] == [("act_1", "app_install", {"app": "Instagram"})]
    assert step.text == "Opening the Play Store."
    assert fake.requests[0]["tools"][0]["type"] == "function"

    step, user_message = phone_agent.submit_results("u1", step.turn_id, "phone-1",
                                                   [{"action_id": "act_1", "status": "done", "output": {"opened": "play_store"}}])
    assert user_message == "Install Instagram" and step.actions[0].id == "act_2"
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and '"done"' in tool_msg["content"]

    step, _ = phone_agent.submit_results("u1", step.turn_id, "phone-1",
                                         [{"action_id": "act_2", "status": "failed",
                                           "error": {"code": "not_found", "message": "nope"}}])
    assert step.final and step.text == "Installed."
    assert '"not_found"' in fake.requests[2]["messages"][-1]["content"]
    with pytest.raises(ServiceError) as err:  # finished turns are dropped
        phone_agent.submit_results("u1", step.turn_id, "phone-1", [])
    assert err.value.code == "turn_not_found"


def test_permission_missing_is_answered_by_backend(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    fake = model([msg(None, call("notifications_list", {}, "c1")), msg("Please allow notification access.")])
    step = phone_agent.start("u1", device, "Read my notifications", [], None)
    assert step.final and step.actions == []
    content = json.loads(fake.requests[1]["messages"][-1]["content"])
    assert content["error"]["code"] == "permission_missing" and "Notification access" in content["error"]["message"]


def test_unknown_tool_and_bad_args_go_back_to_model(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    fake = model([
        msg(None, call("launch_rockets", {}, "c1"), call("alarm_set", "{oops", "c2")),
        msg("Sorry, I can't."),
    ])
    step = phone_agent.start("u1", device, "do things", [], None)
    assert step.final
    errors = [json.loads(m["content"])["error"]["code"] for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert errors == ["not_supported", "invalid_args"]


def test_switch_mode_handled_on_server(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    fake = model([msg(None, call("switch_mode", {"mode": "type"}, "c1")), msg("Switched to typing.")])
    step = phone_agent.start("u1", device, "let me type", [], "talk")
    assert step.final and step.mode == "type"
    assert any(t["function"]["name"] == "switch_mode" for t in fake.requests[0]["tools"])


def test_missing_results_are_reported_as_failed(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    fake = model([msg(None, call("screen_read", {}, "c1"), call("alarm_set", {}, "c2")), msg("ok")])
    step = phone_agent.start("u1", device, "x", [], None)
    phone_agent.submit_results("u1", step.turn_id, "phone-1", [{"action_id": "act_1", "status": "done", "output": {}}])
    tool_msgs = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert '"done"' in tool_msgs[0]["content"] and "No result was reported" in tool_msgs[1]["content"]


def test_turn_checks_owner_and_device(model, device: Device) -> None:  # type: ignore[no-untyped-def]
    model([msg(None, call("screen_read", {}, "c1"))])
    step = phone_agent.start("u1", device, "x", [], None)
    with pytest.raises(ServiceError) as other_user:
        phone_agent.submit_results("u2", step.turn_id, "phone-1", [])
    assert other_user.value.code == "turn_not_found"
    with pytest.raises(ServiceError) as other_device:
        phone_agent.submit_results("u1", step.turn_id, "phone-2", [])
    assert other_device.value.code == "invalid_request"


def test_round_cap_forces_a_final_answer(model, device: Device, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(phone_agent, "MAX_ROUNDS", 2)
    fake = model([
        msg(None, call("screen_read", {}, "c1")),
        msg("I stopped here.", call("screen_read", {}, "c2")),  # tool calls ignored on the last round
    ])
    step = phone_agent.start("u1", device, "loop forever", [], None)
    step, _ = phone_agent.submit_results("u1", step.turn_id, "phone-1", [{"action_id": "act_1", "status": "done"}])
    assert step.final and step.text == "I stopped here."
    assert fake.requests[1]["tool_choice"] == "none"


# ---------------------------------------------------------------- routing


def test_group_of_maps_catalog_prefixes() -> None:
    expect = {
        "screen_tap": "screen", "apps_list": "apps", "app_install": "apps", "contacts_search": "communication",
        "share": "communication", "reminders_list": "time", "calendar_add": "time", "photos_recent": "files",
        "clipboard_write": "files", "web_search": "web", "location_current": "web", "settings_open": "device",
        "media_control": "device", "notification_reply": "notifications", "mystery_tool": "other",
    }
    assert {n: phone_agent.group_of(n) for n in expect} == expect


def test_routed_turn_gets_subset_plus_request_tools(model, device: Device, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    fake = model([
        msg(None, call("request_tools", {"groups": ["screen"]}, "c1")),
        msg(None, call("screen_read", {}, "c2")),
    ])
    monkeypatch.setattr(phone_agent, "route", lambda msg, hist, available: {"time"})
    step = phone_agent.start("u1", device, "set an alarm", [], None)
    first = [t["function"]["name"] for t in fake.requests[0]["tools"] if t["function"]["name"] not in server_tools.NAMES]
    assert first == ["alarm_set", "request_tools"]
    meta = next(t["function"] for t in fake.requests[0]["tools"] if t["function"]["name"] == "request_tools")
    assert "screen" in meta["parameters"]["properties"]["groups"]["items"]["enum"]
    second = {t["function"]["name"] for t in fake.requests[1]["tools"]}
    assert {"screen_read", "screen_tap", "app_install"} <= second  # screen brings apps along
    assert step.actions[0].tool == "screen_read"


def test_calling_an_unrouted_tool_loads_it(model, device: Device, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    fake = model([msg(None, call("screen_read", {}, "c1")), msg(None, call("screen_read", {}, "c2"))])
    monkeypatch.setattr(phone_agent, "route", lambda msg, hist, available: set())
    step = phone_agent.start("u1", device, "x", [], None)
    assert "wasn't loaded yet" in fake.requests[1]["messages"][-1]["content"]
    assert step.actions[0].tool == "screen_read"


def test_routing_failure_sends_all_tools(model, device: Device, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    fake = model([msg("hi")])
    monkeypatch.setattr(phone_agent, "route", lambda msg, hist, available: None)
    phone_agent.start("u1", device, "x", [], None)
    phone = [t for t in fake.requests[0]["tools"] if t["function"]["name"] not in server_tools.NAMES]
    assert len(phone) == len(device.tools)

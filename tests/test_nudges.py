"""Offline tests for proactive nudges (fake memory document, scripted model)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from baymon import history, nudges, services


class Store:
    def __init__(self) -> None:
        self.doc: dict[str, Any] = {"items": [{"id": "m1", "content": "User must call the bank today.", "category": "task"}]}

    def load(self, user_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.doc)

    def mutate(self, user_id: str, change):  # type: ignore[no-untyped-def]
        self.doc = change(copy.deepcopy(self.doc))
        return self.doc


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    store = Store()
    calls: list[str] = []
    replies: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(services, "load_doc", store.load)
    monkeypatch.setattr(services, "mutate_doc", store.mutate)
    monkeypatch.setattr(services, "retrieve_memory", lambda uid, q=None: store.doc["items"])
    monkeypatch.setattr(services, "list_memory", lambda uid: store.doc["items"])
    monkeypatch.setattr(history, "record", lambda uid, signals, days: 0)
    monkeypatch.setattr(history, "purge", lambda uid, days: None)
    monkeypatch.setattr(history, "week_stats", lambda uid: {"apps": [], "people": []})

    def ask(context: str) -> list[dict[str, Any]]:
        calls.append(context)
        return replies.pop(0) if replies else []

    monkeypatch.setattr(nudges, "_ask_model", ask)
    # Fixed clock: 14:00 UTC on a Sunday; settings without quiet hours/check-ins unless a test sets them.
    clock = {"now": datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(nudges, "_now", lambda: clock["now"])
    store.doc["assistant"] = {"settings": {"quiet_start": None, "quiet_end": None, "morning_brief": None,
                                           "evening_wrap": None, "weekly_review": None}}
    return store, calls, replies, clock


def nudge(topic: str, urgency: str = "normal", kind: str = "reminder") -> dict[str, Any]:
    return {"kind": kind, "urgency": urgency, "topic": topic, "text": f"About {topic}", "detail": "More", "suggestions": ["ok"]}


def test_quiet_hours_wrap_midnight() -> None:
    s = {"quiet_start": "22:00", "quiet_end": "08:00"}
    at = lambda h, m=0: datetime(2026, 10, 18, h, m)  # noqa: E731
    assert nudges.in_quiet_hours(at(23), s) and nudges.in_quiet_hours(at(7, 59), s)
    assert not nudges.in_quiet_hours(at(8), s) and not nudges.in_quiet_hours(at(14), s)
    assert not nudges.in_quiet_hours(at(23), {"quiet_start": None, "quiet_end": None})


def test_due_checkins_once_per_day() -> None:
    s = {"morning_brief": "08:00", "evening_wrap": "21:00"}
    at = lambda h: datetime(2026, 10, 18, h, 0)  # noqa: E731
    assert nudges.due_checkins(at(7), s, {}) == []
    assert nudges.due_checkins(at(9), s, {}) == ["morning brief"]
    assert nudges.due_checkins(at(9), s, {"morning_brief": "2026-10-18"}) == []
    assert nudges.due_checkins(at(13), s, {}) == []  # missed the morning window
    assert nudges.due_checkins(at(21), s, {}) == ["evening wrap"]


def test_gate_skips_model_when_nothing_changed(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    nudges.check("u", [], "UTC")  # first look ever → asks
    assert len(calls) == 1
    clock["now"] += timedelta(minutes=5)
    assert nudges.check("u", [{"type": "foreground"}], "UTC") == [] and len(calls) == 1
    nudges.check("u", [{"type": "notification", "data": {"app": "Slack", "text": "hi"}}], "UTC")
    assert len(calls) == 2 and "NEW" in calls[-1] and "Slack" in calls[-1]
    clock["now"] += timedelta(hours=4)
    nudges.check("u", [], "UTC")  # periodic look
    assert len(calls) == 3


def test_topics_are_not_repeated_and_quiet_hours_filter(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    replies.append([nudge("bank"), nudge("bank")])
    first = nudges.check("u", [{"type": "calendar", "data": {"title": "x"}}], "UTC")
    assert [n["text"] for n in first] == ["About bank"]
    replies.append([nudge("bank")])
    assert nudges.check("u", [{"type": "calendar", "data": {"title": "y"}}], "UTC") == []

    store.doc["assistant"]["settings"].update({"quiet_start": "13:00", "quiet_end": "15:00"})
    replies.append([nudge("low"), nudge("fire", urgency="high")])
    quiet = nudges.check("u", [{"type": "battery", "data": {"level": 5}}], "UTC")
    assert [n["urgency"] for n in quiet] == ["high"]


def test_snooze_resurfaces_without_model(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    replies.append([nudge("bank")])
    nid = nudges.check("u", [{"type": "calendar", "data": {}}], "UTC")[0]["id"]
    nudges.feedback("u", nid, "snoozed", 30)
    assert nudges.active("u") == []
    clock["now"] += timedelta(minutes=10)
    assert nudges.check("u", [], "UTC") == []
    clock["now"] += timedelta(minutes=25)
    again = nudges.check("u", [], "UTC")
    assert [n["id"] for n in again] == [nid] and len(calls) == 1
    assert [n["id"] for n in nudges.active("u")] == [nid]


def test_checkin_marked_done_once_considered(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    store.doc["assistant"]["settings"]["evening_wrap"] = "13:30"
    replies.append([nudge("wrap", kind="checkin")])
    assert nudges.check("u", [], "UTC")[0]["kind"] == "checkin" and "evening wrap" in calls[-1]
    clock["now"] += timedelta(minutes=20)
    nudges.check("u", [{"type": "notification", "data": {}}], "UTC")
    assert "Check-ins due now: none" in calls[-1]


def test_disabled_stores_signals_but_never_speaks(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    store.doc["assistant"]["settings"]["enabled"] = False
    assert nudges.check("u", [{"type": "notification", "data": {"text": "x"}}], "UTC") == []
    assert calls == [] and store.doc["assistant"]["signals"][-1]["type"] == "notification"


def test_activity_feeds_followups(env) -> None:  # type: ignore[no-untyped-def]
    store, calls, replies, clock = env
    nudges.check("u", [], "UTC")
    nudges.record_activity("u", "Install Instagram", "Started the download")
    clock["now"] += timedelta(minutes=5)
    nudges.check("u", [], "UTC")
    assert len(calls) == 2 and "NEW asked: Install Instagram" in calls[-1]


def test_feedback_unknown_nudge(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(services.ServiceError) as err:
        nudges.feedback("u", "ndg_missing", "opened")
    assert err.value.code == "nudge_not_found"

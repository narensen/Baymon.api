"""Reminders tied to context ("remind me when…") and named routines.

Both live in the user's memory document under ``assistant``:
    triggers  at most 50: {id, when, say, repeat, routine_id, created_at, last_fired_at}
    routines  at most 30: {id, name, steps, created_at, last_run_at}

Triggers fire inside ``nudges.check`` when a *new* phone signal matches them.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from baymon import services
from baymon.services import ServiceError

STATE_KEY = "assistant"
MAX_TRIGGERS = 50
MAX_ROUTINES = 30
REPEAT_COOLDOWN = timedelta(minutes=10)


class ToolFailure(Exception):
    """A server tool failed; becomes {"status": "failed", "error": {code, message}}."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(doc: dict[str, Any]) -> dict[str, Any]:
    return dict(doc.get(STATE_KEY) or {})


def _list(doc: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [x for x in (_state(doc).get(key) or []) if isinstance(x, dict)]


def _put(doc: dict[str, Any], key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {**doc, STATE_KEY: {**_state(doc), key: items}}


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------


def _clean_str(value: Any, limit: int) -> str | None:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:limit] or None


def validate_when(raw: Any) -> dict[str, Any]:
    """Normalize a trigger condition, or raise ToolFailure(invalid_args)."""
    if not isinstance(raw, dict):
        raise ToolFailure("invalid_args", "when must be an object with a type.")
    kind = raw.get("type")
    if kind == "place":
        place = _clean_str(raw.get("place"), 40)
        on = raw.get("on", "arrive")
        if not place or on not in ("arrive", "leave"):
            raise ToolFailure("invalid_args", 'place triggers need {"type":"place","place":"Office","on":"arrive"|"leave"}.')
        return {"type": "place", "place": place, "on": on}
    if kind == "message":
        sender = _clean_str(raw.get("from"), 80)
        if not sender:
            raise ToolFailure("invalid_args", 'message triggers need {"type":"message","from":"Priya","app":"WhatsApp"?}.')
        return {"type": "message", "from": sender, "app": _clean_str(raw.get("app"), 60)}
    if kind == "app":
        app = _clean_str(raw.get("app"), 60)
        if not app:
            raise ToolFailure("invalid_args", 'app triggers need {"type":"app","app":"Swiggy"}.')
        return {"type": "app", "app": app}
    if kind == "notification":
        app, contains = _clean_str(raw.get("app"), 60), _clean_str(raw.get("contains"), 80)
        if not (app or contains):
            raise ToolFailure("invalid_args", "notification triggers need app or contains.")
        return {"type": "notification", "app": app, "contains": contains}
    raise ToolFailure("invalid_args", "when.type must be place, message, app or notification.")


def describe_when(when: dict[str, Any]) -> str:
    """The condition in words, as the app phrases it."""
    kind = when.get("type")
    if kind == "place":
        return f"when you {'get to' if when.get('on') != 'leave' else 'leave'} {when.get('place')}"
    if kind == "message":
        return f"when {when.get('from')} messages you" + (f" on {when['app']}" if when.get("app") else "")
    if kind == "app":
        return f"next time you open {when.get('app')}"
    parts = [f"{when['app']}" if when.get("app") else "a notification"]
    if when.get("contains"):
        parts.append(f"mentions “{when['contains']}”")
    return "when " + " ".join(parts)


def _has(haystack: Any, needle: str) -> bool:
    return isinstance(haystack, str) and needle.lower() in haystack.lower()


def matches(when: dict[str, Any], signal: dict[str, Any]) -> bool:
    data = signal.get("data") or {}
    kind, stype = when.get("type"), signal.get("type")
    if kind == "place":
        if stype != "location" or not data.get("saved"):
            return False
        wanted_event = "enter" if when.get("on", "arrive") == "arrive" else "exit"
        return str(data.get("label", "")).lower() == str(when.get("place", "")).lower() and data.get("event") == wanted_event
    if kind == "message":
        if stype != "notification":
            return False
        sender = str(when.get("from", ""))
        from_title = _has(data.get("title"), sender)
        from_group = isinstance(data.get("text"), str) and data["text"].lower().startswith(f"{sender.lower()}:")
        app_ok = not when.get("app") or _has(data.get("app"), when["app"])
        return (from_title or from_group) and app_ok
    if kind == "app":
        if stype != "app_open":
            return False
        app = str(when.get("app", ""))
        return _has(data.get("app"), app) or str(data.get("package", "")).lower() == app.lower()
    if kind == "notification":
        if stype != "notification":
            return False
        app_ok = not when.get("app") or _has(data.get("app"), when["app"])
        contains = when.get("contains")
        text_ok = not contains or _has(data.get("title"), contains) or _has(data.get("text"), contains)
        return app_ok and text_ok
    return False


def list_triggers(user_id: str) -> list[dict[str, Any]]:
    """Newest first."""
    items = _list(services.load_doc(user_id), "triggers")
    return sorted(items, key=lambda t: t.get("created_at", ""), reverse=True)[:MAX_TRIGGERS]


def create_trigger(user_id: str, when: Any, say: Any, repeat: bool = False, routine: str | None = None) -> dict[str, Any]:
    cond = validate_when(when)
    text = _clean_str(say, 160)
    if not text:
        raise ToolFailure("invalid_args", "say must be one short sentence.")
    routine_id = None
    if routine:
        found = find_routine(user_id, routine)
        if found is None:
            raise ToolFailure("not_found", f"There's no routine called “{routine}”.")
        routine_id = found["id"]
    trigger = {
        "id": f"trg_{secrets.token_hex(6)}", "when": cond, "say": text, "repeat": bool(repeat),
        "routine_id": routine_id, "created_at": _now().isoformat(), "last_fired_at": None,
    }

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        items = _list(doc, "triggers")
        if len(items) >= MAX_TRIGGERS:
            raise ToolFailure("limit_reached", f"You already have {MAX_TRIGGERS} reminders. Delete one first.")
        return _put(doc, "triggers", items + [trigger])

    services.mutate_doc(user_id, change)
    return trigger


def delete_trigger(user_id: str, trigger_id: str) -> None:
    found = False

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        nonlocal found
        items = _list(doc, "triggers")
        found = any(t.get("id") == trigger_id for t in items)
        return _put(doc, "triggers", [t for t in items if t.get("id") != trigger_id])

    services.mutate_doc(user_id, change)
    if not found:
        raise ServiceError("No such reminder.", "trigger_not_found")


def fire(triggers: list[dict[str, Any]], new_signals: list[dict[str, Any]], now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(trigger, matching signal) for every trigger a new signal sets off (respecting the repeat cooldown)."""
    fired = []
    for trigger in triggers:
        last = trigger.get("last_fired_at")
        if trigger.get("repeat") and last:
            try:
                if now - datetime.fromisoformat(last) < REPEAT_COOLDOWN:
                    continue
            except ValueError:
                pass
        signal = next((s for s in new_signals if matches(trigger.get("when") or {}, s)), None)
        if signal is not None:
            fired.append((trigger, signal))
    return fired


def after_firing(doc: dict[str, Any], fired_ids: set[str], now: datetime) -> dict[str, Any]:
    """Set last_fired_at; delete one-off triggers that fired."""
    items = []
    for t in _list(doc, "triggers"):
        if t.get("id") in fired_ids:
            if not t.get("repeat"):
                continue
            t = {**t, "last_fired_at": now.isoformat()}
        items.append(t)
    return _put(doc, "triggers", items)


# --------------------------------------------------------------------------
# Routines
# --------------------------------------------------------------------------


def list_routines(user_id: str) -> list[dict[str, Any]]:
    """Sorted by name."""
    return sorted(_list(services.load_doc(user_id), "routines"), key=lambda r: str(r.get("name", "")).lower())


def find_routine(user_id: str, name_or_id: str) -> dict[str, Any] | None:
    key = str(name_or_id).strip().lower()
    return next((r for r in _list(services.load_doc(user_id), "routines")
                 if r.get("id") == name_or_id or str(r.get("name", "")).lower() == key), None)


def save_routine(user_id: str, name: Any, steps: Any) -> dict[str, Any]:
    clean_name = _clean_str(name, 40)
    if not clean_name:
        raise ToolFailure("invalid_args", "A routine needs a name (up to 40 characters).")
    if not isinstance(steps, list):
        raise ToolFailure("invalid_args", "steps must be a list of 1–12 short instructions.")
    clean_steps = [s for s in (_clean_str(x, 200) for x in steps) if s]
    if not 1 <= len(clean_steps) <= 12:
        raise ToolFailure("invalid_args", "steps must be a list of 1–12 short instructions.")
    saved: dict[str, Any] = {}

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        items = _list(doc, "routines")
        existing = next((r for r in items if str(r.get("name", "")).lower() == clean_name.lower()), None)
        if existing is None and len(items) >= MAX_ROUTINES:
            raise ToolFailure("limit_reached", f"You already have {MAX_ROUTINES} routines. Delete one first.")
        routine = {**(existing or {"id": f"rtn_{secrets.token_hex(6)}", "created_at": _now().isoformat(),
                                    "last_run_at": None}), "name": clean_name, "steps": clean_steps}
        saved.update(routine)
        others = [r for r in items if r is not existing]
        return _put(doc, "routines", others + [routine])

    services.mutate_doc(user_id, change)
    return saved


def delete_routine(user_id: str, name_or_id: str, not_found_code: str = "routine_not_found") -> dict[str, Any]:
    """Delete by id or name. Triggers pointing at it get routine_id = None."""
    removed: dict[str, Any] = {}

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        key = str(name_or_id).strip().lower()
        items = _list(doc, "routines")
        target = next((r for r in items if r.get("id") == name_or_id or str(r.get("name", "")).lower() == key), None)
        if target is None:
            return doc
        removed.update(target)
        triggers = [{**t, "routine_id": None} if t.get("routine_id") == target["id"] else t for t in _list(doc, "triggers")]
        return _put(_put(doc, "routines", [r for r in items if r is not target]), "triggers", triggers)

    services.mutate_doc(user_id, change)
    if not removed:
        if not_found_code == "routine_not_found":
            raise ServiceError("No such routine.", "routine_not_found")
        raise ToolFailure("not_found", f"There's no routine called “{name_or_id}”.")
    return removed


def mark_routine_run(user_id: str, routine_id: str) -> None:
    now = _now().isoformat()
    services.mutate_doc(user_id, lambda doc: _put(doc, "routines", [
        {**r, "last_run_at": now} if r.get("id") == routine_id else r for r in _list(doc, "routines")]))

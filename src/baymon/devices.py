"""Registered phones and the tool catalogs they provide.

The phone owns its tool catalog and re-sends it on sign-in and whenever the app
returns to the foreground. The latest catalog per (user, device) is kept in
process memory and, if the optional ``public.devices`` table exists (see
supabase/devices.sql), persisted there so it survives server restarts.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from baymon import services

log = logging.getLogger("baymon.devices")

TABLE = "devices"

# Which capability flag a tool depends on. When the flag is off, the backend answers the
# call itself with permission_missing instead of sending it to the phone.
CAPABILITY_FOR_PREFIX = {
    "screen_": ("screen_control", "Screen control is off. Ask the user to turn on Baymon in Settings → Accessibility."),
    "notification": ("notification_access", "Notification access is off. Ask the user to allow Baymon in Settings → Notifications → Device & app notifications / Notification access."),
}


@dataclass
class Device:
    user_id: str
    device_id: str
    platform: str = ""
    app_version: str = ""
    timezone: str = ""
    push_token: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)  # [{name, description, parameters}]
    updated_at: str = ""

    def function_tools(self) -> list[dict[str, Any]]:
        """Catalog as OpenAI-style function definitions, unchanged."""
        return [{"type": "function", "function": t} for t in self.tools]

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    def blocked(self, tool: str) -> str | None:
        """The permission_missing message if this tool can't run right now, else None."""
        for prefix, (flag, message) in CAPABILITY_FOR_PREFIX.items():
            if tool.startswith(prefix) and self.capabilities.get(flag) is False:
                return message
        return None


_cache: dict[tuple[str, str], Device] = {}
_lock = threading.Lock()
_table_missing = False  # set after the first "table doesn't exist" error, to stop retrying


def _db_error(exc: Exception) -> None:
    global _table_missing
    if "PGRST205" in str(exc) or "schema cache" in str(exc):
        if not _table_missing:
            log.warning("public.devices table not found; device catalogs are kept in memory only "
                        "(run supabase/devices.sql to persist them)")
        _table_missing = True
    else:
        log.warning("device storage error: %s", exc)


def register(device: Device) -> bool:
    """Store the latest catalog. Returns True if it was persisted to the database."""
    device.updated_at = datetime.now(timezone.utc).isoformat()
    with _lock:
        _cache[(device.user_id, device.device_id)] = device
    if _table_missing:
        return False
    try:
        services._supabase().table(TABLE).upsert({
            "user_id": device.user_id,
            "device_id": device.device_id,
            "platform": device.platform,
            "app_version": device.app_version,
            "timezone": device.timezone,
            "push_token": device.push_token,
            "capabilities": device.capabilities,
            "tools": device.tools,
            "updated_at": device.updated_at,
        }).execute()
        return True
    except Exception as exc:
        _db_error(exc)
        return False


def get(user_id: str, device_id: str) -> Device | None:
    with _lock:
        if device := _cache.get((user_id, device_id)):
            return device
    if _table_missing:
        return None
    try:
        rows = (
            services._supabase().table(TABLE).select("*")
            .eq("user_id", user_id).eq("device_id", device_id).limit(1).execute().data
        )
    except Exception as exc:
        _db_error(exc)
        return None
    if not rows:
        return None
    row: Any = rows[0]
    device = Device(
        user_id=user_id, device_id=device_id, platform=row.get("platform") or "",
        app_version=row.get("app_version") or "", timezone=row.get("timezone") or "",
        push_token=row.get("push_token"), capabilities=row.get("capabilities") or {},
        tools=row.get("tools") or [], updated_at=row.get("updated_at") or "",
    )
    with _lock:
        _cache[(user_id, device_id)] = device
    return device

"""Searchable history of what the phone noticed (``public.signal_history``).

Signals from ``/v1/nudges/check`` are written here (when retention > 0) so the
model can answer questions about the past with ``history_search``. Retention
(0/30/90/365 days) lives in the memory document under
``assistant.history_settings``. If the table doesn't exist yet (supabase/v2.sql),
writing is skipped and searches say history isn't set up.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from baymon import services
from baymon.services import ServiceError

log = logging.getLogger("baymon.history")

TABLE = "signal_history"
STATE_KEY = "assistant"
RETENTIONS = (0, 30, 90, 365)
DEFAULT_RETENTION = 90
NOT_STORED = {"battery", "foreground", "heartbeat", "app_foreground"}

_table_missing = False


def _missing(exc: Exception) -> bool:
    global _table_missing
    if "PGRST205" in str(exc) or "schema cache" in str(exc):
        if not _table_missing:
            log.warning("public.signal_history not found; run supabase/v2.sql to enable history")
        _table_missing = True
        return True
    return False


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def get_settings(user_id: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = services.load_doc(user_id) if doc is None else doc
    stored = (doc.get(STATE_KEY) or {}).get("history_settings") or {}
    days = stored.get("retention_days", DEFAULT_RETENTION)
    return {"retention_days": days if days in RETENTIONS else DEFAULT_RETENTION}


def update_settings(user_id: str, retention_days: int) -> dict[str, Any]:
    if retention_days not in RETENTIONS:
        raise ServiceError("retention_days must be 0, 30, 90 or 365.", "invalid_request")

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        state = dict(doc.get(STATE_KEY) or {})
        state["history_settings"] = {**(state.get("history_settings") or {}), "retention_days": retention_days}
        return {**doc, STATE_KEY: state}

    services.mutate_doc(user_id, change)
    purge(user_id, retention_days)  # lowering retention deletes the excess now
    return {"retention_days": retention_days}


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def row_for(user_id: str, signal: dict[str, Any]) -> dict[str, Any] | None:
    """The signal_history row for a signal, or None if this type isn't kept."""
    stype, data = signal.get("type", ""), signal.get("data") or {}
    if stype in NOT_STORED:
        return None
    row: dict[str, Any] = {"user_id": user_id, "at": signal.get("at"), "type": stype,
                           "app": None, "title": None, "body": None, "data": data}
    if stype == "notification":
        row.update(app=data.get("app"), title=data.get("title"), body=data.get("text"))
    elif stype == "screen":
        row.update(app=data.get("app"), body=data.get("text"))
    elif stype == "calendar":
        when = " – ".join(str(data[k]) for k in ("start", "end") if data.get(k))
        row.update(title=data.get("title"), body=" · ".join(x for x in (when, data.get("location"), data.get("change")) if x))
    elif stype == "location":
        row.update(title=data.get("label"), body=data.get("event") or data.get("area"))
    elif stype == "app_open":
        row.update(app=data.get("app"))
    else:
        row.update(app=data.get("app"), title=data.get("title"), body=data.get("text"))
    for key in ("app", "title", "body"):
        if row[key] is not None:
            row[key] = str(row[key])[:2000]
    return row


def record(user_id: str, signals: list[dict[str, Any]], retention_days: int) -> int:
    """Store signals; returns how many rows were written."""
    if retention_days <= 0 or _table_missing:
        return 0
    rows = [r for r in (row_for(user_id, s) for s in signals) if r is not None]
    if not rows:
        return 0
    try:
        services._supabase().table(TABLE).insert(rows).execute()
    except Exception as exc:
        if not _missing(exc):
            log.warning("history write failed: %s", exc)
        return 0
    return len(rows)


def purge(user_id: str, retention_days: int) -> None:
    """Delete rows older than the retention (all rows when it's 0)."""
    if _table_missing:
        return
    try:
        q = services._supabase().table(TABLE).delete().eq("user_id", user_id)
        if retention_days > 0:
            q = q.lt("at", (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat())
        q.execute()
    except Exception as exc:
        if not _missing(exc):
            log.warning("history purge failed: %s", exc)


def delete_all(user_id: str) -> None:
    if _table_missing:
        return
    try:
        services._supabase().table(TABLE).delete().eq("user_id", user_id).execute()
    except Exception as exc:
        if not _missing(exc):
            raise ServiceError(f"Couldn't delete history: {exc}", "history_write") from exc


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _local(ts: str, tz: str | None) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).isoformat() if tz else dt.isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return ts


def _like(value: str) -> str:
    # PostgREST filter syntax: keep it to plain words (no commas, parentheses or wildcards).
    return re.sub(r"[,()*%\\\"']", " ", value).strip()


def search(user_id: str, tz: str | None, query: str | None = None, person: str | None = None,
           app: str | None = None, since: str | None = None, until: str | None = None, limit: int = 15) -> dict[str, Any]:
    retention = get_settings(user_id)["retention_days"]
    if retention == 0:
        return {"results": [], "retention_days": 0, "note": "History is off in Settings."}
    if _table_missing:
        return {"results": [], "retention_days": retention, "note": "History storage isn't set up on the server yet."}
    q = (services._supabase().table(TABLE).select("at, type, app, title, body")
         .eq("user_id", user_id).order("at", desc=True).limit(max(1, min(int(limit or 15), 30))))
    if query and query.strip():
        q = q.filter("tsv", "wfts(simple)", query.strip())  # websearch_to_tsquery('simple', …)
    if person and (p := _like(person)):
        q = q.or_(f"title.ilike.*{p}*,body.ilike.*{p}*")
    if app and (a := _like(app)):
        q = q.ilike("app", f"%{a}%")
    if since:
        q = q.gte("at", since)
    if until:
        q = q.lte("at", until)
    try:
        rows = q.execute().data or []
    except Exception as exc:
        if _missing(exc):
            return {"results": [], "retention_days": retention, "note": "History storage isn't set up on the server yet."}
        raise
    results = [{"at": _local(r["at"], tz), "type": r["type"], "app": r.get("app"), "title": r.get("title"),
                "text": r.get("body")} for r in rows if isinstance(r, dict)]
    return {"results": results, "retention_days": retention}


def week_stats(user_id: str) -> dict[str, list[tuple[str, int]]]:
    """Most frequent apps and people over the last 7 days (for the weekly review)."""
    if _table_missing:
        return {"apps": [], "people": []}
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        rows = (services._supabase().table(TABLE).select("type, app, title").eq("user_id", user_id)
                .gte("at", since).limit(1000).execute().data or [])
    except Exception as exc:
        _missing(exc)
        return {"apps": [], "people": []}
    apps = Counter(r["app"] for r in rows if isinstance(r, dict) and r.get("app"))
    people = Counter(r["title"] for r in rows if isinstance(r, dict) and r.get("type") == "notification" and r.get("title"))
    return {"apps": apps.most_common(5), "people": people.most_common(5)}

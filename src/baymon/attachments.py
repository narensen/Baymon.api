"""Shared files (screenshots, photos, PDFs, text): extract text once, reuse by id.

``POST /v1/attachments`` stores ``{id, kind, name, mime, size, text, created_at}``
in ``public.attachments`` (supabase/v2.sql) for 30 days; the bytes are never kept.
Without the table, attachments live in server memory (lost on restart).
"""

from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import groq

from baymon import services
from baymon.services import ServiceError

log = logging.getLogger("baymon.attachments")

TABLE = "attachments"
MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 40_000
CHAT_TEXT_CAP = 30_000
KEEP_DAYS = 30
VISION_MODEL = os.getenv("BAYMON_VISION_MODEL", "qwen/qwen3.8-27b")
VISION_PROMPT = (
    "Transcribe all text in this image exactly. Then describe what it is in 1–3 sentences; for tickets, "
    "receipts and bookings include dates, times, places, amounts and reference numbers. Reply with the "
    "transcription, a blank line, then the description. No preamble."
)
TEXT_MIMES = {"application/json", "application/xml", "application/javascript", "application/x-yaml",
              "application/yaml", "application/csv", "application/x-sh"}

_memory_store: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_table_missing = False


# --------------------------------------------------------------------------
# Kind detection and extraction
# --------------------------------------------------------------------------


def sniff_mime(data: bytes, name: str, declared: str | None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def kind_of(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/") or mime in TEXT_MIMES:
        return "text"
    return "file"


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def extract_image(data: bytes) -> str:
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ServiceError("That image couldn't be opened.", "extraction_failed") from exc
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    try:
        resp = services._groq().chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            max_completion_tokens=4096,
        )
    except groq.GroqError as exc:
        err = services._wrap_groq_error(exc, "read the image")
        raise ServiceError(err.message, "groq_rate_limit" if err.code == "groq_rate_limit" else "extraction_failed") from exc
    return _strip_think(resp.choices[0].message.content or "")[:MAX_TEXT]


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:30]:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > MAX_TEXT:
                break
    except (PdfReadError, ValueError, OSError) as exc:
        raise ServiceError("That PDF couldn't be read.", "extraction_failed") from exc
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip()[:MAX_TEXT]


def extract(kind: str, data: bytes) -> str:
    if kind == "image":
        return extract_image(data)
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "text":
        return data.decode("utf-8", errors="replace")[:MAX_TEXT]
    return ""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _missing(exc: Exception) -> bool:
    global _table_missing
    if "PGRST205" in str(exc) or "schema cache" in str(exc):
        if not _table_missing:
            log.warning("public.attachments not found; keeping attachments in memory (run supabase/v2.sql)")
        _table_missing = True
        return True
    return False


def save(user_id: str, data: bytes, name: str | None, mime: str | None) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise ServiceError("That file is over 20 MB.", "too_large")
    name = (name or "file").strip()[:200] or "file"
    mime = sniff_mime(data, name, mime)
    kind = kind_of(mime)
    record = {
        "id": f"att_{secrets.token_hex(6)}", "user_id": user_id, "kind": kind, "name": name, "mime": mime[:100],
        "size": len(data), "text": extract(kind, data), "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not _table_missing:
        try:
            table = services._supabase().table(TABLE)
            table.insert(record).execute()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
            table.delete().eq("user_id", user_id).lt("created_at", cutoff).execute()
            return record
        except Exception as exc:
            if not _missing(exc):
                raise ServiceError(f"Couldn't save the attachment: {exc}", "attachment_write") from exc
    with _lock:
        _memory_store[record["id"]] = record
    return record


def get_many(user_id: str, ids: list[str]) -> list[dict[str, Any]]:
    """The user's attachments, in the order given. 404 attachment_not_found if any is missing."""
    if not ids:
        return []
    found: dict[str, dict[str, Any]] = {}
    if not _table_missing:
        try:
            rows = (services._supabase().table(TABLE).select("*").eq("user_id", user_id)
                    .in_("id", list(dict.fromkeys(ids))).execute().data or [])
            found = {r["id"]: r for r in rows if isinstance(r, dict)}
        except Exception as exc:
            if not _missing(exc):
                raise ServiceError(f"Couldn't load attachments: {exc}", "attachment_read") from exc
    with _lock:
        for i in ids:
            if i not in found and (rec := _memory_store.get(i)) and rec["user_id"] == user_id:
                found[i] = rec
    missing = [i for i in ids if i not in found]
    if missing:
        raise ServiceError(f"Attachment not found: {missing[0]}", "attachment_not_found")
    return [found[i] for i in ids]


def inject(message: str, attachments: list[dict[str, Any]]) -> str:
    """This turn's user message as the model sees it: the text plus each attachment's content."""
    if not attachments:
        return message
    budget = CHAT_TEXT_CAP
    blocks = []
    for att in attachments:
        text = att.get("text") or ""
        if not text:
            note = "(no readable text: it may be a scan or an unsupported file)"
            blocks.append(f"[Attached: {att['name']} ({att['kind']})]\n{note}")
            continue
        piece = text[:budget]
        budget -= len(piece)
        if len(piece) < len(text):
            piece += "\n[… truncated]"
        blocks.append(f"[Attached: {att['name']} ({att['kind']})]\n<<<\n{piece}\n>>>")
    return message + "\n\n" + "\n\n".join(blocks)

"""Core runtime functions for the personal agent.

Auth is handled outside (JS layer); callers pass in the authenticated user_id.
Everything here is stateless except the explicit memory read/write in Supabase.

Memory lives in a JSONB column (``memory.memory -> 'items'``, one row per user) as a list of
``{"content", "category", "created_at"}`` objects. See ``supabase/schema.sql``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any, BinaryIO, Callable

import groq
from dotenv import load_dotenv
from groq import Groq
from supabase import Client, create_client

from baymon.formatting import FORMATTING_PROMPT, TALK_FORMATTING_PROMPT

load_dotenv()

log = logging.getLogger("baymon.services")

TEXT_MODEL = os.getenv("BAYMON_TEXT_MODEL", "openai/gpt-oss-120b")
STT_MODEL = os.getenv("BAYMON_STT_MODEL", "whisper-large-v3-turbo")
TTS_MODEL = os.getenv("BAYMON_TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.getenv("BAYMON_TTS_VOICE", "troy")
# Orpheus English voices: (id, label, gender)
VOICES = (
    ("troy", "Troy", "male"),
    ("austin", "Austin", "male"),
    ("daniel", "Daniel", "male"),
    ("autumn", "Autumn", "female"),
    ("diana", "Diana", "female"),
    ("hannah", "Hannah", "female"),
)
VOICE_IDS = tuple(v[0] for v in VOICES)
# Pre-recorded per voice by scripts/generate_voice_previews.py (served as static files).
PREVIEW_LINES = (
    "Hi, I'm Baymon. I'll be the voice that helps you through your day.",
    "Your gym alarm is set for six thirty tomorrow morning.",
    "Done! I found the document and sent it to your class group.",
    "Good morning! You've got two meetings today, and it might rain later, so grab an umbrella.",
    "Hmm, that build failed. Want me to look at the error and try a fix?",
    "Nice work today. Get some rest, and I'll remind you about the dentist tomorrow.",
)
MEMORY_TABLE = "memory"
USER_ID_COLUMN = "user_id"
MEMORY_COLUMN = "memory"
MEMORY_CATEGORIES = ("preference", "fact", "task", "decision", "other")
MEMORY_MAX_CHARS = 500
MEMORY_WRITE_RETRIES = 5

# Orpheus on Groq accepts at most ~200 characters per request.
TTS_MAX_CHARS = 200
# Cap on how many memory items are injected into the prompt.
MEMORY_PROMPT_LIMIT = 50
# Cap on history turns sent to the model.
HISTORY_LIMIT = 20

DEFAULT_SYSTEM_PROMPT = """\
You are Baymon, a personal AI companion for a single user. You are warm, direct, \
and genuinely helpful. Use what you remember about the user naturally; do not \
recite memories back unless relevant. Keep answers concise unless the user asks \
for depth. Use Markdown when it helps (code blocks, lists)."""

MODE_PROMPTS = {
    "talk": "The user is in talk mode: they are speaking to you hands-free and your "
    "reply will be read aloud, so keep it conversational.\n"
    "Reply in plain spoken English: no Markdown, no lists, no UI tags, no URLs, no code.\n"
    "Keep it to one or two sentences unless the user explicitly asks for more detail.",
    "type": "The user is in type mode: they are typing and reading your replies on screen.",
}

SWITCH_MODE_TOOL = {
    "type": "function",
    "function": {
        "name": "switch_mode",
        "description": "Switch how the user talks to you. Call ONLY when the user explicitly "
        "asks to change mode, e.g. 'switch to typing', 'let me type', 'go to voice mode'.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["talk", "type"],
                    "description": "talk = hands-free voice conversation; type = keyboard input, text replies",
                }
            },
            "required": ["mode"],
        },
    },
}

SPEECH_DECISION_PROMPT = """\
You prepare text for a text-to-speech voice. Given an assistant reply, decide \
whether it can be read aloud as-is or needs a spoken version.

Use "summary" when the reply contains code, tables, long lists, URLs, file paths, \
heavy Markdown, or is longer than a few sentences. The spoken version should be \
short, natural conversational speech in the assistant's own first-person voice \
(e.g. "Here's a function that..." — never "The reply shows..."). Point the user \
to the screen for details, never read code, symbols or <tags> (or their JSON) aloud, no Markdown. \
Otherwise use "same".

Respond with JSON only: {"mode": "same" | "summary", "speech": "<text to speak>"}
For "same", "speech" is the reply with any Markdown formatting removed."""

MEMORY_EXTRACTION_PROMPT = """\
You manage long-term memory for a personal AI companion. Read the content (a \
conversation turn, or notes the user shared about themselves) and decide whether anything is worth remembering long-term about the user: \
preferences, personal facts, relationships, goals, ongoing tasks/projects, \
decisions, or commitments.

Do NOT store: small talk, one-off questions, general knowledge, things the \
assistant said that the user did not confirm, or anything already in existing \
memory (unless it updates it).

Each memory must be a single self-contained sentence about the user, written in \
the third person (e.g. "User prefers Python over JavaScript.").

Respond with JSON only:
{"memories": [{"content": "...", "category": "preference" | "fact" | "task" | "decision" | "other"}]}
Return {"memories": []} if nothing is worth storing."""


class ServiceError(Exception):
    """Error with a short, user-presentable message.

    ``code`` is a stable identifier the mobile app can map to notifications.
    """

    def __init__(self, message: str, code: str = "service_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class TurnResult:
    text: str
    speech_text: str | None = None
    audio: bytes | None = None
    memory_stored: bool = False
    mode: str | None = None  # input mode the user asked to switch to ("talk" | "type")
    errors: list[ServiceError] = field(default_factory=list)


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or "YOUR-PROJECT-REF" in value:
        raise ServiceError(f"{name} is not configured. Set it in .env.", "config_missing")
    return value


@cache
def _groq() -> Groq:
    return Groq(api_key=_require_env("GROQ_API_KEY"))


@cache
def _supabase() -> Client:
    return create_client(_require_env("SUPABASE_URL"), _require_env("SUPABASE_SECRET_KEY"))


def _wrap_groq_error(exc: Exception, action: str) -> ServiceError:
    if isinstance(exc, groq.AuthenticationError):
        return ServiceError("Groq rejected the API key.", "groq_auth")
    if isinstance(exc, groq.RateLimitError):
        wait = exc.response.headers.get("retry-after", "")
        when = f"in {int(wait) // 60}m {int(wait) % 60}s" if wait.isdigit() else "shortly"
        scope = "daily" if "per day" in exc.message else "rate"
        return ServiceError(f"Hit Groq's {scope} limit while trying to {action}. Try again {when}.", "groq_rate_limit")
    if isinstance(exc, groq.APIConnectionError):
        return ServiceError(f"Couldn't reach Groq to {action}. Check your connection.", "groq_unreachable")
    if isinstance(exc, groq.APIStatusError):
        return ServiceError(f"Groq failed to {action} ({exc.status_code}): {exc.message}", "groq_api")
    return ServiceError(f"Failed to {action}: {exc}", "groq_unknown")


def _chat_json(system: str, user: str) -> dict[str, Any]:
    """Run a small JSON-mode completion used for internal decisions."""
    try:
        resp = _groq().chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            reasoning_effort="low",
            temperature=0.2,
        )
    except groq.GroqError as exc:
        raise _wrap_groq_error(exc, "process the reply") from exc
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError as exc:
        raise ServiceError("Model returned malformed JSON.", "bad_model_output") from exc


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------


def listen(
    audio_source: str | Path | bytes | BinaryIO,
    filename: str = "audio.wav",
    prompt: str | None = None,
) -> str:
    """Transcribe speech to text.

    ``audio_source`` may be a file path, raw audio bytes, or a binary file
    object. Supported formats: flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm.
    ``filename`` hints the format when passing bytes / file objects.
    ``prompt`` gives Whisper prior context (e.g. the preceding transcript).
    """
    if isinstance(audio_source, (str, Path)):
        path = Path(audio_source)
        if not path.is_file():
            raise ServiceError(f"Audio file not found: {path}", "audio_not_found")
        payload = (path.name, path.read_bytes())
    elif isinstance(audio_source, bytes):
        payload = (filename, audio_source)
    else:
        payload = (getattr(audio_source, "name", filename), audio_source.read())

    if not payload[1]:
        raise ServiceError("No audio was captured.", "audio_empty")

    try:
        result = _groq().audio.transcriptions.create(
            file=payload,
            model=STT_MODEL,
            response_format="json",
            temperature=0.0,
            # Groq rejects prompts over 896 chars; keep the most recent context.
            **({"prompt": prompt[-896:]} if prompt else {}),
        )
    except groq.GroqError as exc:
        raise _wrap_groq_error(exc, "transcribe audio") from exc

    text = (result.text or "").strip()
    if not text:
        raise ServiceError("Couldn't hear anything in that recording.", "transcription_empty")
    return text


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


def _load_row(user_id: str) -> tuple[dict[str, Any], int] | None:
    """Return (memory JSON object, version) for the user, or None if no row exists."""
    try:
        resp = (
            _supabase()
            .table(MEMORY_TABLE)
            .select(f"{MEMORY_COLUMN}, version")
            .eq(USER_ID_COLUMN, user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise ServiceError(f"Couldn't load memory: {exc}", "memory_read") from exc
    if not resp.data:
        return None
    row: Any = resp.data[0]
    memory = row.get(MEMORY_COLUMN)
    return (memory if isinstance(memory, dict) else {}), int(row.get("version") or 1)


def _items(memory: dict[str, Any]) -> list[dict[str, Any]]:
    items = memory.get("items")
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _load_memory(user_id: str) -> list[dict[str, Any]]:
    row = _load_row(user_id)
    return _items(row[0]) if row else []


def mutate_doc(user_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Apply ``change`` to the user's whole memory JSON with optimistic locking on ``version``.

    Retries if another write landed between the read and the update. Returns the new document.
    """
    table = _supabase().table(MEMORY_TABLE)
    for _ in range(MEMORY_WRITE_RETRIES):
        row = _load_row(user_id)
        try:
            if row is None:
                doc = change({})
                table.insert({USER_ID_COLUMN: user_id, MEMORY_COLUMN: doc}).execute()
                return doc
            memory, version = row
            doc = change(dict(memory))
            resp = (
                table.update({MEMORY_COLUMN: doc, "version": version + 1})
                .eq(USER_ID_COLUMN, user_id)
                .eq("version", version)
                .execute()
            )
        except ServiceError:
            raise
        except Exception as exc:
            text = str(exc)
            if "23505" in text:  # concurrent insert won the race; retry as an update
                continue
            if "23503" in text:
                raise ServiceError("No users row for this account.", "user_not_found") from exc
            raise ServiceError(f"Couldn't save memory: {exc}", "memory_write") from exc
        if resp.data:
            return doc
    raise ServiceError("Memory is being updated elsewhere; try again.", "memory_conflict")


def load_doc(user_id: str) -> dict[str, Any]:
    """The user's whole memory JSON ({} if none yet)."""
    row = _load_row(user_id)
    return row[0] if row else {}


def _mutate(user_id: str, change: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    """Apply ``change`` to the user's memory items; other keys in the document are preserved."""
    mutate_doc(user_id, lambda doc: {**doc, "items": change(_items(doc))})


def _append(user_id: str, items: list[dict[str, Any]]) -> None:
    _mutate(user_id, lambda current: current + items)


def _new_memory(content: str, category: str, metadata: dict | None = None) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise ServiceError("Memory content can't be empty.", "invalid_memory")
    if len(content) > MEMORY_MAX_CHARS:
        raise ServiceError(f"Memory is too long (max {MEMORY_MAX_CHARS} characters).", "invalid_memory")
    return {
        "id": uuid.uuid4().hex,
        "content": content,
        "category": category if category in MEMORY_CATEGORIES else "other",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **({"metadata": metadata} if metadata else {}),
    }


def retrieve_memory(user_id: str, query: str | None = None) -> list[dict[str, Any]]:
    """Memories for a prompt: the ones relevant to ``query`` (vector search) plus recent ones.

    Without a query, or if vector search is unavailable, falls back to the most recent.
    """
    items = _load_memory(user_id)
    if query and query.strip():
        from baymon import embeddings

        try:
            return embeddings.relevant(user_id, items, query)
        except Exception as exc:  # never fail a reply because retrieval is down
            log.warning("vector memory retrieval failed, using recent memories: %s", exc)
    return items[-MEMORY_PROMPT_LIMIT:]


def list_memory(user_id: str) -> list[dict[str, Any]]:
    """Return all of the user's memories, oldest first."""
    return _load_memory(user_id)


def add_memory(user_id: str, content: str, category: str = "other", metadata: dict | None = None) -> dict[str, Any]:
    """Store a memory verbatim (no AI involved). Returns the stored item."""
    item = _new_memory(content, category, metadata)
    _append(user_id, [item])
    return item


def update_memory(user_id: str, memory_id: str, content: str | None = None, category: str | None = None) -> dict[str, Any]:
    """Edit a memory's content and/or category. Returns the updated item."""
    patch: dict[str, Any] = {}
    if content is not None:
        patch["content"] = _new_memory(content, "other")["content"]
    if category is not None:
        if category not in MEMORY_CATEGORIES:
            raise ServiceError(f"Unknown category: {category}", "invalid_memory")
        patch["category"] = category
    if not patch:
        raise ServiceError("Nothing to update.", "invalid_memory")
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()

    updated: dict[str, Any] = {}

    def change(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not any(m.get("id") == memory_id for m in items):
            raise ServiceError("Memory not found.", "memory_not_found")
        out = [{**m, **patch} if m.get("id") == memory_id else m for m in items]
        updated.update(next(m for m in out if m.get("id") == memory_id))
        return out

    _mutate(user_id, change)
    return updated


def delete_memory(user_id: str, memory_id: str) -> None:
    def change(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not any(m.get("id") == memory_id for m in items):
            raise ServiceError("Memory not found.", "memory_not_found")
        return [m for m in items if m.get("id") != memory_id]

    _mutate(user_id, change)


def _format_memory(memory: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {m.get('content', '')}" for m in memory if m.get("content"))


def extract_memories(user_id: str, content: str, metadata: dict | None = None) -> list[dict[str, Any]]:
    """Let the AI pick out anything worth remembering from ``content`` and store it.

    ``content`` can be a conversation turn or free-form notes. Returns the
    newly stored items (empty if nothing was worth keeping).
    """
    existing = _load_memory(user_id)
    prompt = f"Existing memory:\n{_format_memory(existing) or '(none)'}\n\nContent:\n{content}"
    decision = _chat_json(MEMORY_EXTRACTION_PROMPT, prompt)

    known = {m.get("content", "").strip().lower() for m in existing}
    new_items = []
    for item in decision.get("memories") or []:
        text = str(item.get("content", "")).strip()[:MEMORY_MAX_CHARS]
        if not text or text.lower() in known:
            continue
        known.add(text.lower())
        new_items.append(_new_memory(text, str(item.get("category", "other")), metadata))

    if new_items:
        _append(user_id, new_items)
    return new_items


def insert_to_memory(user_id: str, turn_content: str, metadata: dict | None = None) -> bool:
    """Let the AI decide whether this turn holds anything worth remembering.

    Returns True if at least one memory was stored.
    """
    return bool(extract_memories(user_id, turn_content, metadata))


# --------------------------------------------------------------------------
# User settings (columns on public.users)
# --------------------------------------------------------------------------

USERS_TABLE = "users"
_voice_cache: dict[str, tuple[float, str | None]] = {}
VOICE_CACHE_SECONDS = 60


def get_voice_model(user_id: str) -> str | None:
    """The user's chosen speaking voice, or None if they haven't picked one."""
    import time

    cached = _voice_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < VOICE_CACHE_SECONDS:
        return cached[1]
    try:
        rows = _supabase().table(USERS_TABLE).select("voice_model").eq(USER_ID_COLUMN, user_id).limit(1).execute().data
    except Exception as exc:
        if "voice_model" in str(exc):
            raise ServiceError("The users table has no voice_model column yet.", "config_missing") from exc
        raise ServiceError(f"Couldn't load settings: {exc}", "settings_read") from exc
    if not rows:
        raise ServiceError("No users row for this account.", "user_not_found")
    row: Any = rows[0]
    voice = row.get("voice_model") if isinstance(row, dict) else None
    voice = voice if voice in VOICE_IDS else None  # unknown/legacy values count as "not chosen"
    _voice_cache[user_id] = (time.monotonic(), voice)
    return voice


def set_voice_model(user_id: str, voice: str | None) -> str | None:
    """Save the user's speaking voice (None clears it)."""
    import time

    if voice is not None and voice not in VOICE_IDS:
        raise ServiceError(f"Unknown voice {voice!r}. Choose one of: {', '.join(VOICE_IDS)}.", "invalid_request")
    try:
        rows = _supabase().table(USERS_TABLE).update({"voice_model": voice}).eq(USER_ID_COLUMN, user_id).execute().data
    except Exception as exc:
        if "voice_model" in str(exc):
            raise ServiceError("The users table has no voice_model column yet.", "config_missing") from exc
        raise ServiceError(f"Couldn't save settings: {exc}", "settings_write") from exc
    if not rows:
        raise ServiceError("No users row for this account.", "user_not_found")
    _voice_cache[user_id] = (time.monotonic(), voice)
    return voice


def resolve_voice(user_id: str | None, requested: str | None = None) -> str:
    """Voice to speak with: the request's, else the user's saved one, else the default."""
    if requested in VOICE_IDS:
        return requested  # type: ignore[return-value]
    if user_id:
        try:
            return get_voice_model(user_id) or TTS_VOICE
        except ServiceError:
            pass  # never fail speech over a settings lookup
    return TTS_VOICE


# --------------------------------------------------------------------------
# Text generation
# --------------------------------------------------------------------------


def build_system_prompt(user_input: str, session_context: dict, user_id: str) -> str:
    """Persona + mode + reply format + relevant memory, for chat and talk replies."""
    memory = session_context.get("memory")
    if memory is None:
        memory = retrieve_memory(user_id, user_input) if user_id else []

    system = session_context.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    mode = session_context.get("mode")
    if mode in MODE_PROMPTS:
        system += f"\n\n{MODE_PROMPTS[mode]}"
    system += f"\n\n{TALK_FORMATTING_PROMPT if mode == 'talk' else FORMATTING_PROMPT}"
    if memory_text := _format_memory(memory):
        system += f"\n\nWhat you remember about the user:\n{memory_text}"
    return system


def _generate(user_input: str, session_context: dict, user_id: str) -> tuple[str, str | None]:
    """Return (reply text, requested mode switch or None)."""
    system = build_system_prompt(user_input, session_context, user_id)
    mode = session_context.get("mode")

    history = session_context.get("history") or []
    messages: list[Any] = [
        {"role": "system", "content": system},
        *history[-HISTORY_LIMIT:],
        {"role": "user", "content": user_input},
    ]
    tools = [SWITCH_MODE_TOOL] if mode in MODE_PROMPTS else None

    new_mode = None
    try:
        resp = _groq().chat.completions.create(model=TEXT_MODEL, messages=messages, tools=tools)
        message = resp.choices[0].message
        if message.tool_calls:
            messages.append(message.model_dump(include={"role", "content", "tool_calls"}, exclude_none=True))
            for call in message.tool_calls:
                try:
                    requested = json.loads(call.function.arguments or "{}").get("mode")
                except json.JSONDecodeError:
                    requested = None
                if call.function.name == "switch_mode" and requested in MODE_PROMPTS:
                    new_mode = requested
                    outcome = f"Switched to {requested} mode. Briefly confirm to the user."
                else:
                    outcome = "Unknown action; nothing changed."
                messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome})
            resp = _groq().chat.completions.create(model=TEXT_MODEL, messages=messages)
            message = resp.choices[0].message
    except groq.GroqError as exc:
        raise _wrap_groq_error(exc, "generate a reply") from exc

    text = (message.content or "").strip()
    if not text:
        raise ServiceError("The model returned an empty reply.", "empty_reply")
    return text, new_mode


def generate_text(user_input: str, session_context: dict, user_id: str) -> str:
    """Generate the main text response via Groq.

    ``session_context`` keys (all optional):
      - ``history``: list of ``{"role": "user"|"assistant", "content": str}``
      - ``memory``: pre-fetched memory list (fetched via user_id if absent)
      - ``system_prompt``: override for the default persona prompt
      - ``mode``: current input mode ("talk" | "type"); enables the switch_mode tool
    """
    return _generate(user_input, session_context, user_id)[0]


def _looks_speakable(text: str) -> bool:
    """Cheap pre-check so short plain replies skip the extra model call."""
    return len(text) <= 300 and not re.search(r"```|^\s*[|#>\-<]|https?://|`", text, re.MULTILINE)


def decide_speech_text(text: str) -> str:
    """Ask the AI whether to speak the full text or a spoken summary.

    Returns the text that should be passed to generate_audio.
    """
    if _looks_speakable(text):
        return text
    decision = _chat_json(SPEECH_DECISION_PROMPT, text)
    speech = str(decision.get("speech") or "").strip()
    return speech or text


CAPTION_MAX_WORDS = 40
CAPTION_MAX_SENTENCES = 2
_UI_TAG_BLOCK = re.compile(r"^<([a-z]+)\b[^>\n]*>.*?^</\1>[ \t]*$", re.S | re.M)


def strip_formatting(text: str) -> str:
    """Reduce Markdown + UI tags to plain speakable prose."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # code blocks
    text = _UI_TAG_BLOCK.sub(" ", text)  # <schedule>…</schedule> etc.
    text = re.sub(r"</?[a-z]+\b[^>\n]*>", " ", text)  # stray tags
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*(#{1,6}|>|[-*+]|\d+[.)])\s+", "", text, flags=re.M)  # headings, quotes, lists
    text = re.sub(r"(\*\*|__|\*|_|`|~~)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p]


def limit_caption(text: str, max_words: int = CAPTION_MAX_WORDS, max_sentences: int = CAPTION_MAX_SENTENCES) -> str:
    """Hard-limit to whole sentences within the budget, else the first max_words words + ellipsis."""
    kept: list[str] = []
    words = 0
    for sentence in _split_sentences(text)[:max_sentences]:
        n = len(sentence.split())
        if words + n > max_words:
            break
        kept.append(sentence)
        words += n
    if kept:
        return " ".join(kept)
    cut = " ".join(text.split()[:max_words]).rstrip(",;:-—")
    return f"{cut}…" if cut else ""


def _fits_caption(text: str, max_words: int) -> bool:
    return 0 < len(text.split()) <= max_words and len(_split_sentences(text)) <= CAPTION_MAX_SENTENCES


def make_caption(text: str, max_words: int = CAPTION_MAX_WORDS) -> str:
    """Exactly what will be spoken: plain text, at most 2 sentences and ``max_words`` words.

    Short plain replies are used as-is (formatting stripped); anything else gets a
    first-person spoken summary from the model, then the limit is enforced in code.
    """
    has_structure = re.search(r"```|^\s*<[a-z]+\b|^\s*\|", text, re.M)
    plain = strip_formatting(text)
    if not has_structure and _fits_caption(plain, max_words):
        return plain
    budget = (
        f"\nThe spoken version must be at most {CAPTION_MAX_SENTENCES} sentences and {max_words} words. "
        'Always use "summary" unless the reply already fits.'
    )
    try:
        speech = str(_chat_json(SPEECH_DECISION_PROMPT + budget, text).get("speech") or "")
    except ServiceError:
        speech = ""
    caption = limit_caption(strip_formatting(speech) or plain, max_words)
    return caption or "The details are on screen."


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------


def _split_for_tts(text: str, limit: int = TTS_MAX_CHARS) -> list[str]:
    """Split text into chunks under ``limit`` chars, on sentence then word boundaries."""
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _parse_wav(data: bytes) -> tuple[tuple[int, int, int], bytes]:
    """Return ((channels, sample_width, rate), pcm) from a PCM WAV.

    Groq streams WAVs with a placeholder (0xFFFFFFFF) data size, which the
    stdlib ``wave`` module can't handle, so chunks are walked by hand and the
    data chunk is taken to the end of the buffer.
    """
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ServiceError("Speech service returned invalid audio.", "tts_bad_audio")
    fmt = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            channels, rate = struct.unpack("<HI", data[body + 2 : body + 8])
            bits = struct.unpack("<H", data[body + 14 : body + 16])[0]
            fmt = (channels, bits // 8, rate)
        elif chunk_id == b"data":
            if fmt is None:
                break
            end = len(data) if size == 0xFFFFFFFF else min(body + size, len(data))
            return fmt, data[body:end]
        pos = body + size + (size & 1)
    raise ServiceError("Speech service returned invalid audio.", "tts_bad_audio")


def _concat_wav(parts: list[bytes]) -> bytes:
    fmt, _ = _parse_wav(parts[0])
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(fmt[0])
        writer.setsampwidth(fmt[1])
        writer.setframerate(fmt[2])
        for part in parts:
            part_fmt, pcm = _parse_wav(part)
            if part_fmt != fmt:
                raise ServiceError("Speech chunks had mismatched audio formats.", "tts_bad_audio")
            writer.writeframes(pcm)
    return out.getvalue()


def generate_audio(text_to_speak: str, context: dict | None = None) -> bytes:
    """Generate WAV audio from the chosen text.

    ``context`` may contain ``voice`` (troy, austin, daniel, autumn, diana, hannah).
    Long text is chunked to respect Orpheus's per-request limit and the
    resulting WAV segments are joined.
    """
    context = context or {}
    voice = context.get("voice", TTS_VOICE)
    chunks = _split_for_tts(text_to_speak)
    if not chunks:
        raise ServiceError("Nothing to speak.", "tts_empty")

    parts = []
    for chunk in chunks:
        try:
            # TTS has tight per-minute limits; retry a few more times (the SDK honours retry-after).
            resp = _groq().with_options(max_retries=4).audio.speech.create(
                model=TTS_MODEL, voice=voice, input=chunk, response_format="wav"
            )
        except groq.GroqError as exc:
            raise _wrap_groq_error(exc, "generate speech") from exc
        parts.append(resp.read())
    return _concat_wav(parts)


# --------------------------------------------------------------------------
# Generation flow
# --------------------------------------------------------------------------


def run_turn(
    user_id: str | None,
    user_input: str,
    session_context: dict | None = None,
    voice_mode: bool = False,
    remember: bool = True,
) -> TurnResult:
    """Full generation flow: memory -> text -> (speech decision + audio) -> memory write.

    Text generation errors propagate. Memory and audio failures are non-fatal
    and collected in ``TurnResult.errors`` so the text still returns.
    Pass ``user_id=None`` to run without long-term memory, or ``remember=False``
    to skip the memory write (e.g. to run insert_to_memory in the background).
    """
    result = TurnResult(text="")
    session_context = dict(session_context or {})
    if "memory" not in session_context:
        try:
            session_context["memory"] = retrieve_memory(user_id, user_input) if user_id else []
        except ServiceError as exc:
            session_context["memory"] = []
            result.errors.append(exc)

    result.text, result.mode = _generate(user_input, session_context, user_id or "")

    if voice_mode:
        try:
            result.speech_text = decide_speech_text(result.text)
            voice = resolve_voice(user_id, session_context.get("voice"))
            result.audio = generate_audio(result.speech_text, {**session_context, "voice": voice})
        except ServiceError as exc:
            result.errors.append(exc)

    if user_id and remember:
        try:
            result.memory_stored = insert_to_memory(
                user_id,
                f"User: {user_input}\nAssistant: {result.text}",
                {"source": "voice" if voice_mode else "text"},
            )
        except ServiceError as exc:
            result.errors.append(exc)

    return result

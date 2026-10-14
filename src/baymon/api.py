"""HTTP API for the frontend.

All /v1 routes require ``Authorization: Bearer <supabase access token>``. The
token is verified locally against the project's JWKS; the user id always comes
from the token, never from the request body.

Run: ``uv run baymon-api`` (or ``uv run uvicorn baymon.api:app --reload``).
Errors are returned as ``{"error": {"code": str, "message": str}}``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from supabase import ClientOptions, create_client
from supabase_auth.errors import AuthError

from baymon import completions, services
from baymon.services import ServiceError

log = logging.getLogger("baymon.api")

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("BAYMON_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if o.strip()
]
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq's direct-upload limit

STATUS_BY_CODE = {
    "unauthorized": 401,
    "memory_not_found": 404,
    "user_not_found": 404,
    "memory_conflict": 409,
    "invalid_memory": 422,
    "invalid_request": 422,
    "audio_empty": 422,
    "audio_too_large": 413,
    "transcription_empty": 422,
    "tts_empty": 422,
    "groq_rate_limit": 429,
    "provider_rate_limit": 429,
    "invalid_credentials": 401,
    "model_unavailable": 403,
    "context_too_long": 413,
    "groq_unreachable": 503,
    "config_missing": 500,
}

app = FastAPI(title="Baymon API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


@app.exception_handler(ServiceError)
def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
    status = STATUS_BY_CODE.get(exc.code, 502)
    if status >= 500:
        log.warning("service error %s: %s", exc.code, exc.message)
    return _error(status, exc.code, exc.message)


@app.exception_handler(HTTPException)
def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    code = "unauthorized" if exc.status_code == 401 else "http_error"
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    message = f"{where}: {first.get('msg', 'invalid')}" if where else first.get("msg", "Invalid request.")
    return _error(422, "invalid_request", message, details=jsonable_errors(exc))


def jsonable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]


@app.exception_handler(Exception)
def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return _error(500, "internal", "Something went wrong.")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class User(BaseModel):
    id: str
    email: str | None = None


_bearer = HTTPBearer(auto_error=False)


def current_user(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User:
    if creds is None:
        raise HTTPException(401, "Missing bearer token.")
    try:
        result = services._supabase().auth.get_claims(creds.credentials)
    except AuthError as exc:
        raise HTTPException(401, f"Invalid token: {exc.message}") from exc
    except Exception as exc:  # malformed tokens raise plain decode errors
        raise HTTPException(401, "Invalid token.") from exc
    claims = result["claims"] if result else {}
    if not claims.get("sub") or claims.get("aud") != "authenticated":
        raise HTTPException(401, "Token is not for a signed-in user.")
    return User(id=claims["sub"], email=claims.get("email"))


CurrentUser = Annotated[User, Depends(current_user)]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

Voice = Literal["autumn", "diana", "hannah", "austin", "daniel", "troy"]
Category = Literal["preference", "fact", "task", "decision", "other"]


class ErrorInfo(BaseModel):
    code: str
    message: str


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=40)
    voice: bool = Field(False, description="Also return spoken audio of the reply.")
    mode: Literal["talk", "type"] | None = Field(
        None, description="Current input mode; enables the AI to switch modes on request."
    )
    tts_voice: Voice | None = None


class ChatResponse(BaseModel):
    text: str
    speech_text: str | None = Field(None, description="What was spoken (may be a summary of text).")
    audio_base64: str | None = Field(None, description="WAV audio, base64-encoded.")
    audio_mime: str | None = None
    mode: Literal["talk", "type"] | None = Field(None, description="Mode the user asked to switch to.")
    warnings: list[ErrorInfo] = Field(default_factory=list, description="Non-fatal failures (e.g. audio).")


class Memory(BaseModel):
    id: str
    content: str
    category: str
    created_at: str
    updated_at: str | None = None
    metadata: dict[str, Any] | None = None


class MemoryList(BaseModel):
    items: list[Memory]


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=services.MEMORY_MAX_CHARS)
    category: Category = "other"


class MemoryUpdate(BaseModel):
    content: str | None = Field(None, min_length=1, max_length=services.MEMORY_MAX_CHARS)
    category: Category | None = None


class MemoryImport(BaseModel):
    text: str = Field(min_length=1, max_length=20_000, description="Free-form notes; the AI extracts memories.")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    voice: Voice | None = None


class Transcription(BaseModel):
    text: str


class TalkResponse(BaseModel):
    transcript: str = Field(description="What Whisper heard; the user's message.")
    text: str = Field(description="Full reply, as stored in history.")
    caption: str = Field(description="Exactly the words spoken: plain text, at most 2 sentences / 40 words.")
    audio_base64: str | None = Field(None, description="WAV of caption, base64-encoded; null if speech failed.")
    audio_mime: str | None = None
    mode: Literal["type"] | None = Field(None, description='"type" if the user asked to switch to typing.')
    warnings: list[ErrorInfo] = Field(default_factory=list)


_history_adapter = TypeAdapter(list[ChatMessage])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class Session(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: int | None = Field(None, description="Unix time the access token expires.")
    user: User


class ModelInfo(BaseModel):
    key: str
    label: str
    preview: bool
    default: bool


class AgentMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None = Field(None, max_length=400_000)
    tool_calls: list[dict[str, Any]] | None = Field(None, max_length=64)
    tool_call_id: str | None = Field(None, max_length=200)


class AgentTool(BaseModel):
    type: Literal["function"] = "function"
    function: dict[str, Any]


class AgentRequest(BaseModel):
    messages: list[AgentMessage] = Field(min_length=1, max_length=1000)
    tools: list[AgentTool] = Field(default_factory=list, max_length=64)
    model: str = completions.DEFAULT_MODEL
    mode: Literal["talk", "type"] = "type"
    environment: dict[str, str] = Field(default_factory=dict, max_length=20, description="cwd, os, shell, date…")
    max_tokens: int | None = Field(None, ge=1, le=65_536)


class ObserveRequest(BaseModel):
    user_message: str = Field(min_length=1, max_length=20_000)
    reply: str = Field("", max_length=40_000)
    source: str = Field("cli", max_length=40)


class VoiceReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=40_000)
    voice: Voice | None = None


class VoiceReply(BaseModel):
    caption: str = Field(description="Exactly what is spoken: plain text, at most 2 sentences / 40 words.")
    audio_base64: str | None = None
    audio_mime: str | None = None
    warnings: list[ErrorInfo] = Field(default_factory=list)
WHISPER_PROMPT_CHARS = 896  # Groq rejects longer Whisper prompts


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/me")
def me(user: CurrentUser) -> User:
    return user


def _auth_client() -> Any:
    # A fresh client per call: signing in must never change the shared service client's session.
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    ).auth


def _session(resp: Any) -> Session:
    if not resp or not resp.session or not resp.user:
        raise ServiceError("Sign-in failed.", "invalid_credentials")
    s = resp.session
    return Session(
        access_token=s.access_token,
        refresh_token=s.refresh_token,
        expires_at=s.expires_at,
        user=User(id=resp.user.id, email=resp.user.email),
    )


@app.post("/v1/auth/login")
def login(body: LoginRequest) -> Session:
    """Email + password sign-in for clients without Supabase keys (e.g. the CLI)."""
    try:
        return _session(_auth_client().sign_in_with_password({"email": body.email, "password": body.password}))
    except AuthError as exc:
        raise ServiceError(exc.message or "Wrong email or password.", "invalid_credentials") from exc


@app.post("/v1/auth/refresh")
def refresh(body: RefreshRequest) -> Session:
    try:
        return _session(_auth_client().refresh_session(body.refresh_token))
    except AuthError as exc:
        raise ServiceError("Session expired. Sign in again.", "invalid_credentials") from exc


@app.get("/v1/models")
def models(user: CurrentUser) -> list[ModelInfo]:
    return [
        ModelInfo(key=m.key, label=m.label, preview=m.preview, default=m.key == completions.DEFAULT_MODEL)
        for m in completions.MODELS.values()
    ]


@app.post("/v1/agent/complete", responses={200: {"content": {"application/x-ndjson": {}}}})
def agent_complete(body: AgentRequest, user: CurrentUser) -> StreamingResponse:
    """One model step for a client-side agent loop. Streams NDJSON events (see baymon.completions).

    The client sends the conversation (without a system message) and its tool
    schemas; the server adds the system prompt with the user's memory.
    """
    system = completions.build_system_prompt(user.id, body.mode, body.environment)
    messages = [{"role": "system", "content": system}] + [m.model_dump(exclude_none=True) for m in body.messages]
    tools = [t.model_dump() for t in body.tools]
    events = completions.stream_completion(body.model, messages, tools, body.max_tokens)

    def ndjson() -> Iterator[bytes]:
        for event in events:
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


def _remember_turn(user_id: str, message: str, reply: str, source: str) -> None:
    try:
        services.insert_to_memory(user_id, f"User: {message}\nAssistant: {reply}", {"source": source})
    except ServiceError as exc:
        log.warning("memory write failed for %s: %s", user_id, exc.message)


@app.post("/v1/chat")
def chat(body: ChatRequest, user: CurrentUser, background: BackgroundTasks) -> ChatResponse:
    """Generate a reply (and optional audio). Memory is updated after the response is sent."""
    context: dict[str, Any] = {"history": [m.model_dump() for m in body.history]}
    if body.mode:
        context["mode"] = body.mode
    if body.tts_voice:
        context["voice"] = body.tts_voice

    result = services.run_turn(user.id, body.message, context, voice_mode=body.voice, remember=False)
    background.add_task(_remember_turn, user.id, body.message, result.text, "voice" if body.voice else "text")

    return ChatResponse(
        text=result.text,
        speech_text=result.speech_text,
        audio_base64=base64.b64encode(result.audio).decode() if result.audio else None,
        audio_mime="audio/wav" if result.audio else None,
        mode=result.mode,  # type: ignore[arg-type]
        warnings=[ErrorInfo(code=e.code, message=e.message) for e in result.errors],
    )


@app.post("/v1/transcribe")
def transcribe(
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="flac, mp3, mp4, m4a, ogg, wav or webm")],
    prompt: Annotated[str | None, Form(max_length=1000)] = None,
) -> Transcription:
    data = file.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise ServiceError("Audio file is too large (max 25 MB).", "audio_too_large")
    return Transcription(text=services.listen(data, file.filename or "audio.webm", prompt=prompt))


def _parse_history(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        turns = _history_adapter.validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ServiceError(f"history{'.' + where if where else ''}: {first.get('msg')}", "invalid_request") from exc
    return [t.model_dump() for t in turns[-40:]]


def _whisper_prompt(turns: list[dict[str, str]]) -> str | None:
    """Context for Whisper: the tail of the last thing Baymon said."""
    last = next((t["content"] for t in reversed(turns) if t["role"] == "assistant"), "")
    return services.strip_formatting(last)[-WHISPER_PROMPT_CHARS:] or None


@app.post("/v1/talk")
def talk(
    user: CurrentUser,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Recording: m4a (iOS/Android) or webm (web), max 25 MB")],
    history: Annotated[str | None, Form(description="JSON array of {role, content}")] = None,
    tts_voice: Annotated[Voice | None, Form()] = None,
) -> TalkResponse:
    """One hands-free voice turn: transcribe, reply, caption, speak."""
    data = file.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise ServiceError("Audio file is too large (max 25 MB).", "audio_too_large")
    turns = _parse_history(history)
    transcript = services.listen(data, file.filename or "audio.m4a", prompt=_whisper_prompt(turns))

    context: dict[str, Any] = {"history": turns, "mode": "talk", **({"voice": tts_voice} if tts_voice else {})}
    result = services.run_turn(user.id, transcript, context, voice_mode=False, remember=False)

    caption = services.make_caption(result.text)
    warnings = [ErrorInfo(code=e.code, message=e.message) for e in result.errors]
    audio = None
    try:
        audio = services.generate_audio(caption, context)
    except ServiceError as exc:
        warnings.append(ErrorInfo(code=exc.code, message=exc.message))

    background.add_task(_remember_turn, user.id, transcript, result.text, "voice")
    return TalkResponse(
        transcript=transcript,
        text=result.text,
        caption=caption,
        audio_base64=base64.b64encode(audio).decode() if audio else None,
        audio_mime="audio/wav" if audio else None,
        mode="type" if result.mode == "type" else None,
        warnings=warnings,
    )


@app.post("/v1/speak", response_class=Response, responses={200: {"content": {"audio/wav": {}}}})
def speak(body: SpeakRequest, user: CurrentUser) -> Response:
    audio = services.generate_audio(body.text, {"voice": body.voice} if body.voice else None)
    return Response(content=audio, media_type="audio/wav")


@app.post("/v1/speak/reply")
def speak_reply(body: VoiceReplyRequest, user: CurrentUser) -> VoiceReply:
    """Turn any reply into a short spoken caption and its audio."""
    caption = services.make_caption(body.text)
    warnings: list[ErrorInfo] = []
    audio = None
    try:
        audio = services.generate_audio(caption, {"voice": body.voice} if body.voice else None)
    except ServiceError as exc:
        warnings.append(ErrorInfo(code=exc.code, message=exc.message))
    return VoiceReply(
        caption=caption,
        audio_base64=base64.b64encode(audio).decode() if audio else None,
        audio_mime="audio/wav" if audio else None,
        warnings=warnings,
    )


@app.post("/v1/memory/observe", status_code=202)
def observe(body: ObserveRequest, user: CurrentUser, background: BackgroundTasks) -> dict[str, bool]:
    """Let the AI learn from a finished turn, in the background."""
    background.add_task(_remember_turn, user.id, body.user_message, body.reply, body.source)
    return {"queued": True}


@app.get("/v1/memory")
def get_memory(user: CurrentUser) -> MemoryList:
    return MemoryList(items=[Memory.model_validate(m) for m in services.list_memory(user.id)])


@app.post("/v1/memory", status_code=201)
def create_memory(body: MemoryCreate, user: CurrentUser) -> Memory:
    """Store a memory exactly as given."""
    return Memory.model_validate(services.add_memory(user.id, body.content, body.category, {"source": "manual"}))


@app.post("/v1/memory/import")
def import_memory(body: MemoryImport, user: CurrentUser) -> MemoryList:
    """Let the AI pull memories out of free-form notes. Returns what was stored."""
    items = services.extract_memories(user.id, body.text, {"source": "import"})
    return MemoryList(items=[Memory.model_validate(m) for m in items])


@app.patch("/v1/memory/{memory_id}")
def edit_memory(memory_id: str, body: MemoryUpdate, user: CurrentUser) -> Memory:
    return Memory.model_validate(services.update_memory(user.id, memory_id, body.content, body.category))


@app.delete("/v1/memory/{memory_id}", status_code=204)
def remove_memory(memory_id: str, user: CurrentUser) -> Response:
    services.delete_memory(user.id, memory_id)
    return Response(status_code=204)


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "baymon.api:app",
        host=os.getenv("BAYMON_HOST", "127.0.0.1"),
        port=int(os.getenv("BAYMON_PORT", "8000")),
        reload=os.getenv("BAYMON_RELOAD", "").lower() in ("1", "true"),
    )

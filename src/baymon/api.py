"""HTTP API for the frontend.

All /v1 routes require ``Authorization: Bearer <supabase access token>``. The
token is verified locally against the project's JWKS; the user id always comes
from the token, never from the request body.

Run: ``uv run baymon-api`` (or ``uv run uvicorn baymon.api:app --reload``).
Errors are returned as ``{"error": {"code": str, "message": str}}``.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from supabase_auth.errors import AuthError

from baymon import services
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
    "audio_empty": 422,
    "audio_too_large": 413,
    "transcription_empty": 422,
    "tts_empty": 422,
    "groq_rate_limit": 429,
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


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/me")
def me(user: CurrentUser) -> User:
    return user


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


@app.post("/v1/speak", response_class=Response, responses={200: {"content": {"audio/wav": {}}}})
def speak(body: SpeakRequest, user: CurrentUser) -> Response:
    audio = services.generate_audio(body.text, {"voice": body.voice} if body.voice else None)
    return Response(content=audio, media_type="audio/wav")


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

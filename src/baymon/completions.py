"""Streaming chat completions with tools, for the CLI agent.

The client (CLI) owns the conversation and executes tools locally; the server
adds the system prompt (persona, memory, mode, environment) and proxies the
model so API keys never leave the server.

Events yielded by ``stream_completion`` (one JSON object per NDJSON line):
  {"type": "reasoning", "delta": str}
  {"type": "text", "delta": str}
  {"type": "tool_calls", "tool_calls": [{"id", "type": "function", "function": {"name", "arguments"}}]}
  {"type": "done", "finish_reason": str | None, "usage": {...} | None}
  {"type": "status", "message": str}        # e.g. waiting out a provider rate limit
  {"type": "error", "code": str, "message": str}
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import groq

from baymon import services
from baymon.services import ServiceError


@dataclass(frozen=True)
class ModelSpec:
    key: str  # public name clients use
    label: str
    groq_id: str
    reasoning_effort: str | None = None  # gpt-oss only
    preview: bool = False


MODELS: dict[str, ModelSpec] = {
    "gpt-oss-120b": ModelSpec("gpt-oss-120b", "GPT-OSS 120B", "openai/gpt-oss-120b", reasoning_effort="medium"),
    "minimax-m2.7": ModelSpec("minimax-m2.7", "MiniMax M2.7 (preview)", "minimaxai/minimax-m2.7", preview=True),
}
DEFAULT_MODEL = "gpt-oss-120b"

MAX_RATE_LIMIT_WAIT = 90  # seconds; longer waits (daily limits) fail immediately
RATE_LIMIT_RETRIES = 4

AGENT_PROMPT = """\
You are running as a command-line agent on the user's own computer. You have tools to \
read, search and edit files, run shell commands, fetch web pages, keep a task plan, and \
manage your long-term memory of the user.

How to work:
- Work in the user's current directory unless told otherwise; relative paths resolve there.
- Read a file before editing it. Use edit_file for targeted changes and write_file for new \
files or full rewrites. Keep changes minimal and match the existing style.
- After changing code, run the project's tests or a quick check when one exists, and check the \
result matches what you expect (e.g. the test count went up after adding a test). Never claim \
success you haven't verified.
- run_command runs in a real shell on the user's machine. Prefer non-interactive commands. \
Never run destructive commands (deleting files, force-pushing, dropping data) unless the \
user explicitly asked for exactly that.
- Some tools need the user's approval. If they deny one, adjust or ask; don't retry the same call.
- For tasks with several steps, keep a short plan with update_plan and mark steps done as you go.
- Don't narrate every tool call; do the work, then give a short summary of what changed and \
anything the user should check.

Understanding the user is part of your job: when you learn something durable about them \
(preferences, projects, people, goals, routines, how they like to work), call remember. \
Never store passwords, keys or other secrets. Use recall when past context would help, and \
forget when the user asks you to."""

CLI_FORMATTING_PROMPT = """\
FORMATTING

Your replies are rendered as Markdown in a terminal. Use short paragraphs, **bold** for key \
terms, `code` for identifiers and paths, fenced code blocks with a language for code, and \
Markdown tables only for genuinely tabular data. Do not use HTML or UI tags."""


def build_system_prompt(user_id: str, mode: str, environment: dict[str, str]) -> str:
    parts = [services.DEFAULT_SYSTEM_PROMPT, services.MODE_PROMPTS.get(mode, services.MODE_PROMPTS["type"]), AGENT_PROMPT]
    parts.append(services.TALK_FORMATTING_PROMPT if mode == "talk" else CLI_FORMATTING_PROMPT)
    if environment:
        parts.append("Environment:\n" + "\n".join(f"- {k}: {v}" for k, v in environment.items()))
    try:
        memory = services.retrieve_memory(user_id)
    except ServiceError:
        memory = []
    if memory:
        lines = "\n".join(f"- [{m.get('id', '?')}] {m.get('content', '')}" for m in memory if m.get("content"))
        parts.append(f"What you remember about the user (ids in brackets, for forget):\n{lines}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def _provider(spec: ModelSpec) -> tuple[Any, str, str]:
    """Return (client, model id, provider name). All models run on Groq."""
    return services._groq(), spec.groq_id, "groq"


def _wrap_error(exc: Exception, spec: ModelSpec, provider: str) -> ServiceError:
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", exc))
    if status == 404 and provider == "groq" and spec.preview:
        return ServiceError(
            f"{spec.label} isn't enabled on this Groq account yet (it's a preview model). "
            "Switch models with /model, or ask Groq to enable it.",
            "model_unavailable",
        )
    lowered = message.lower()
    if status in (400, 413) and ("context" in lowered or "too long" in lowered or "reduce the length" in lowered):
        return ServiceError("The conversation is too long for the model. Use /compact or /clear.", "context_too_long")
    if isinstance(exc, groq.GroqError):
        return services._wrap_groq_error(exc, "generate a reply")
    return ServiceError(f"{spec.label} failed ({status or 'error'}): {message}", "provider_error")


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class _ThinkSplitter:
    """Moves <think>...</think> spans out of streamed content into reasoning."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.thinking = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self.buf += chunk
        out: list[tuple[str, str]] = []
        while self.buf:
            tag = self.CLOSE if self.thinking else self.OPEN
            kind = "reasoning" if self.thinking else "text"
            idx = self.buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append((kind, self.buf[:idx]))
                self.buf = self.buf[idx + len(tag) :]
                self.thinking = not self.thinking
                continue
            # Hold back a possible partial tag at the end.
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self.buf.endswith(tag[:n])), 0)
            emit, self.buf = (self.buf[:-keep], self.buf[-keep:]) if keep else (self.buf, "")
            if emit:
                out.append((kind, emit))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        return [("reasoning" if self.thinking else "text", rest)] if rest else []


def stream_completion(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Start a streaming completion. Raises ServiceError before streaming if the request fails."""
    spec = MODELS.get(model)
    if spec is None:
        raise ServiceError(f"Unknown model: {model}. Choose one of: {', '.join(MODELS)}.", "invalid_request")
    client, model_id, provider = _provider(spec)

    kwargs: dict[str, Any] = {"model": model_id, "messages": messages, "stream": True}
    if tools:
        kwargs["tools"] = tools
    if max_tokens:
        kwargs["max_completion_tokens"] = max_tokens
    if spec.reasoning_effort:
        kwargs["reasoning_effort"] = spec.reasoning_effort
    create = client.with_options(max_retries=0).chat.completions.create
    try:
        stream = create(**kwargs)
    except groq.RateLimitError as exc:
        wait = _retry_after(exc)
        if wait is None:
            raise _wrap_error(exc, spec, provider) from exc
        return _after_rate_limit(create, kwargs, spec, provider, wait)
    except groq.GroqError as exc:
        raise _wrap_error(exc, spec, provider) from exc
    return _events(stream, spec, provider)


def _retry_after(exc: Exception) -> int | None:
    """Seconds to wait before retrying, or None if it's too long to wait in-request."""
    response = getattr(exc, "response", None)
    raw = response.headers.get("retry-after", "") if response is not None else ""
    try:
        wait = max(int(float(raw)), 1)
    except ValueError:
        wait = 10
    return wait if wait <= MAX_RATE_LIMIT_WAIT else None


def _after_rate_limit(create: Any, kwargs: dict[str, Any], spec: ModelSpec, provider: str, wait: int) -> Iterator[dict[str, Any]]:
    """Wait out short per-minute limits while telling the client, then stream."""
    who = "Groq"
    for _ in range(RATE_LIMIT_RETRIES):
        yield {"type": "status", "message": f"{who} rate limit reached; retrying in {wait}s"}
        time.sleep(wait)
        try:
            stream = create(**kwargs)
        except groq.RateLimitError as exc:
            next_wait = _retry_after(exc)
            if next_wait is None:
                err = _wrap_error(exc, spec, provider)
                yield {"type": "error", "code": err.code, "message": err.message}
                return
            wait = next_wait
            continue
        except groq.GroqError as exc:
            err = _wrap_error(exc, spec, provider)
            yield {"type": "error", "code": err.code, "message": err.message}
            return
        yield from _events(stream, spec, provider)
        return
    yield {"type": "error", "code": "provider_rate_limit", "message": f"Still rate limited by {who}. Try again in a minute."}


def _events(stream: Any, spec: ModelSpec, provider: str) -> Iterator[dict[str, Any]]:
    splitter = _ThinkSplitter()
    calls: dict[int, dict[str, Any]] = {}
    finish_reason = None
    usage = None
    try:
        for chunk in stream:
            x_groq = getattr(chunk, "x_groq", None)
            if x_groq is not None and getattr(x_groq, "usage", None):
                usage = x_groq.usage.model_dump(include={"prompt_tokens", "completion_tokens", "total_tokens"})
            elif getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump(include={"prompt_tokens", "completion_tokens", "total_tokens"})
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if reasoning := getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None):
                yield {"type": "reasoning", "delta": reasoning}
            if delta.content:
                for kind, text in splitter.feed(delta.content):
                    yield {"type": kind, "delta": text}
            for tc in delta.tool_calls or []:
                call = calls.setdefault(tc.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.id:
                    call["id"] = tc.id
                if tc.function and tc.function.name:
                    call["function"]["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    call["function"]["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except groq.GroqError as exc:
        err = _wrap_error(exc, spec, provider)
        yield {"type": "error", "code": err.code, "message": err.message}
        return
    for kind, text in splitter.flush():
        yield {"type": kind, "delta": text}
    if calls:
        yield {"type": "tool_calls", "tool_calls": [calls[i] for i in sorted(calls)]}
    yield {"type": "done", "finish_reason": finish_reason, "usage": usage}

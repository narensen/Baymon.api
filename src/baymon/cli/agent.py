"""Client-side agent loop: stream a model step, run requested tools locally, repeat."""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from baymon.cli import tools
from baymon.cli.client import ApiError, Client
from baymon.cli.config import Config
from baymon.cli.ui import UI

MAX_STEPS = 60
CONTEXT_BUDGET_CHARS = 360_000  # ~90k tokens; GPT-OSS has a 131k window
KEEP_RECENT = 8  # messages never trimmed

COMPACT_PROMPT = (
    "Summarize our conversation so far so you can continue it with no other context: the user's goals, "
    "decisions made, files created or changed (with paths), commands run and their outcomes, the current "
    "state of the work, and open questions or next steps. Be concise and factual. Reply with the summary only."
)


@dataclass
class TurnResult:
    text: str
    requested_mode: str | None = None
    interrupted: bool = False
    usage: dict[str, Any] | None = None


class Agent:
    def __init__(self, client: Client, config: Config, ui: UI, cwd: Path | None = None) -> None:
        self.client = client
        self.config = config
        self.ui = ui
        self.messages: list[dict[str, Any]] = []
        self.ctx = tools.ToolContext(cwd=(cwd or Path.cwd()).resolve(), client=client, ui=ui,
                                     auto_approve=config.auto_approve)
        self._observers: list[threading.Thread] = []
        self.last_usage: dict[str, Any] | None = None

    # ----------------------------------------------------------- context

    @property
    def cwd(self) -> Path:
        return self.ctx.cwd

    def set_cwd(self, path: Path) -> None:
        self.ctx.cwd = path.resolve()

    def environment(self) -> dict[str, str]:
        env = {
            "cwd": str(self.cwd),
            "os": f"{platform.system()} {platform.release()}",
            "shell": "PowerShell" if sys.platform == "win32" else os.path.basename(os.environ.get("SHELL", "bash")),
            "date": datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M %Z"),
        }
        head = self.cwd / ".git" / "HEAD"
        try:
            ref = head.read_text().strip()
            env["git_branch"] = ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:12]
        except OSError:
            pass
        return env

    def reset(self) -> None:
        self.messages.clear()

    def _size(self) -> int:
        return sum(len(m.get("content") or "") + len(str(m.get("tool_calls") or "")) for m in self.messages)

    def _trim(self) -> None:
        if self._size() <= CONTEXT_BUDGET_CHARS:
            return
        # 1) shrink old tool outputs
        for m in self.messages[:-KEEP_RECENT]:
            if m["role"] == "tool" and len(m.get("content") or "") > 400:
                m["content"] = m["content"][:400] + "\n[older output trimmed to save context]"
        # 2) drop whole old turns (from a user message to the next user message)
        while self._size() > CONTEXT_BUDGET_CHARS and len(self.messages) > KEEP_RECENT:
            nxt = next((i for i, m in enumerate(self.messages[1:], 1) if m["role"] == "user"), None)
            if nxt is None or nxt >= len(self.messages) - 1:
                break
            del self.messages[:nxt]
        self.ui.info("(Trimmed older context to stay within the model's limit. /compact summarizes instead.)")

    # --------------------------------------------------------------- run

    def _step(self, mode: str, with_tools: bool = True) -> tuple[str, list[dict[str, Any]] | None]:
        payload = {
            "messages": self.messages,
            "tools": tools.schemas() if with_tools else [],
            "model": self.config.model,
            "mode": mode,
            "environment": self.environment(),
        }
        text = ""
        calls = None
        self.ui.start_response()
        try:
            for event in self.client.stream_agent(payload):
                kind = event.get("type")
                if kind == "text":
                    text += event["delta"]
                    self.ui.text(event["delta"])
                elif kind == "reasoning":
                    self.ui.reasoning(event["delta"])
                elif kind == "tool_calls":
                    calls = event["tool_calls"]
                elif kind == "status":
                    self.ui.status(event.get("message", ""))
                elif kind == "done":
                    self.last_usage = event.get("usage")
                elif kind == "error":
                    raise ApiError(event.get("message", "Model error"), event.get("code", "model_error"))
        finally:
            self.ui.end_response()
        return text, calls

    def run(self, user_text: str, mode: str = "type") -> TurnResult:
        start = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        self._trim()
        self.ctx.requested_mode = None
        self.ctx.auto_approve = self.config.auto_approve
        final = ""
        try:
            for _ in range(MAX_STEPS):
                try:
                    text, calls = self._step(mode)
                except KeyboardInterrupt:
                    self.messages.append({"role": "assistant", "content": "[Interrupted by the user.]"})
                    return TurnResult("", interrupted=True)
                message: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    message["tool_calls"] = calls
                self.messages.append(message)
                if not calls:
                    final = text
                    break
                if not self._run_tools(calls):
                    return TurnResult("", interrupted=True)
            else:
                final = "(Stopped after too many steps. Say “continue” to keep going.)"
                self.ui.warn(final)
        except ApiError as exc:
            # Leave the conversation valid: drop a dangling user message, or close a tool sequence.
            if len(self.messages) == start + 1:
                self.messages.pop()
            elif self.messages[-1]["role"] == "tool":
                self.messages.append({"role": "assistant", "content": f"[Request failed: {exc.message}]"})
            raise

        self._observe(user_text, final)
        return TurnResult(final, self.ctx.requested_mode, usage=self.last_usage)

    def _run_tools(self, calls: list[dict[str, Any]]) -> bool:
        """Execute tool calls in order. Returns False if the user interrupted."""
        for i, call in enumerate(calls):
            fn = call.get("function", {})
            name, raw = fn.get("name", ""), fn.get("arguments", "")
            tool = tools.BY_NAME.get(name)
            try:
                if tool is not None:
                    try:
                        label = tool.label(json.loads(raw or "{}"))
                    except Exception:
                        label = name
                    if name != "update_plan":
                        self.ui.tool_start(label)
                _, _, result = tools.execute(name, raw, self.ctx)
            except KeyboardInterrupt:
                for rest in calls[i:]:
                    self.messages.append({"role": "tool", "tool_call_id": rest.get("id", ""),
                                          "content": "Cancelled: the user interrupted."})
                self.ui.warn("Interrupted.")
                return False
            if name != "update_plan":
                self.ui.tool_end(result.summary, result.error)
            self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result.content})
        return True

    def compact(self) -> str:
        """Replace the conversation with a model-written summary."""
        if not self.messages:
            return ""
        self.messages.append({"role": "user", "content": COMPACT_PROMPT})
        try:
            summary, _ = self._step("type", with_tools=False)
        except (ApiError, KeyboardInterrupt):
            self.messages.pop()
            raise
        self.messages = [
            {"role": "user", "content": f"[Summary of our earlier conversation]\n{summary}"},
            {"role": "assistant", "content": "Got it — I have the context. What's next?"},
        ]
        return summary

    # ------------------------------------------------------------ memory

    def _observe(self, user_text: str, reply: str) -> None:
        def send() -> None:
            try:
                self.client.memory_observe(user_text, reply)
            except Exception:
                pass  # learning is best-effort; never interrupt the user

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        self._observers = [t for t in self._observers if t.is_alive()] + [thread]

    def wait_for_background(self, timeout: float = 3.0) -> None:
        for thread in self._observers:
            thread.join(timeout)

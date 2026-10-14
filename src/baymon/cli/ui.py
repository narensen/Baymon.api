"""Terminal rendering: streamed Markdown, tool activity, approvals, plans."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from baymon.cli.tools import Decision

THEME = Theme({
    "muted": "grey58",
    "accent": "bold cyan",
    "tool": "cyan",
    "ok": "green",
    "warn": "yellow",
    "err": "bold red",
    "you": "bold magenta",
})

PLAN_ICONS = {"done": ("✔", "green"), "in_progress": ("◐", "yellow"), "pending": ("○", "grey58")}


class MarkdownStream:
    """Prints streamed Markdown block by block (at blank lines outside code fences)."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.buf = ""
        self.printed_any = False

    def feed(self, delta: str) -> None:
        self.buf += delta
        lines = self.buf.split("\n")
        complete, tail = lines[:-1], lines[-1]
        in_fence = False
        cut = None
        for i, line in enumerate(complete):
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            elif not stripped and not in_fence:
                cut = i
        if cut is None:
            return
        block = "\n".join(complete[:cut])
        self.buf = "\n".join(complete[cut + 1 :] + [tail])
        self._print(block)

    def pending_lines(self) -> int:
        return self.buf.count("\n")

    def flush(self) -> None:
        block, self.buf = self.buf, ""
        self._print(block)

    def _print(self, block: str) -> None:
        if block.strip():
            if self.printed_any:
                self.console.print()  # blocks are rendered separately; keep paragraph spacing
            self.console.print(Markdown(block, code_theme="monokai", hyperlinks=True))
            self.printed_any = True


class UI:
    def __init__(self, console: Console | None = None, interactive: bool = True, show_reasoning: bool = False) -> None:
        self.console = console or Console(theme=THEME, highlight=False)
        self.interactive = interactive
        self.show_reasoning = show_reasoning
        self._status: Status | None = None
        self._stream: MarkdownStream | None = None
        self._reasoning_chars = 0

    # --------------------------------------------------------- status line

    def _spin(self, label: str) -> None:
        if not self.interactive:
            return
        if self._status is None:
            self._status = self.console.status(Text(label, style="muted"), spinner="dots")
            self._status.start()
        else:
            self._status.update(Text(label, style="muted"))

    def _stop_spin(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # ------------------------------------------------------- model output

    def start_response(self) -> None:
        self._stream = MarkdownStream(self.console)
        self._reasoning_chars = 0
        self._spin("Thinking…")

    def reasoning(self, delta: str) -> None:
        self._reasoning_chars += len(delta)
        if self.show_reasoning:
            self._stop_spin()
            self.console.print(Text(delta, style="muted italic"), end="")
        else:
            self._spin(f"Thinking… ({self._reasoning_chars:,} chars)")

    def status(self, message: str) -> None:
        if self.interactive:
            self._spin(message + "…")
        else:
            self.info(message)

    def text(self, delta: str) -> None:
        # Rich prints finished blocks above the live spinner, so it can keep running.
        assert self._stream is not None
        self._stream.feed(delta)
        pending = self._stream.pending_lines()
        self._spin(f"Writing… ({pending} lines)" if pending else "Writing…")

    def end_response(self) -> None:
        self._stop_spin()
        if self._stream is not None:
            self._stream.flush()
            self._stream = None

    # ------------------------------------------------------------- tools

    def tool_start(self, label: str) -> None:
        self._stop_spin()
        self.console.print(Text.assemble(("● ", "tool"), (label, "bold")))

    def tool_end(self, summary: str, error: bool) -> None:
        style = "err" if error else "muted"
        self.console.print(Text.assemble(("  ⎿ ", "muted"), (summary or ("failed" if error else "done"), style)))

    def command_output(self, line: str) -> None:
        self.console.print(Text("  │ " + line, style="grey50"), soft_wrap=False, overflow="ellipsis", no_wrap=True)

    def show_plan(self, steps: list[dict[str, str]]) -> None:
        lines = Text()
        for i, step in enumerate(steps):
            icon, style = PLAN_ICONS.get(step["status"], PLAN_ICONS["pending"])
            lines.append(f"{icon} ", style=style)
            lines.append(step["step"], style="strike grey58" if step["status"] == "done" else "")
            if i < len(steps) - 1:
                lines.append("\n")
        self.console.print(Panel(lines, title="Plan", title_align="left", border_style="grey37", expand=False))

    def approve(self, tool: str, title: str, preview: str | None, preview_lang: str) -> Decision:
        self._stop_spin()
        body: Any
        if preview:
            shown = preview if len(preview) < 12_000 else preview[:12_000] + "\n… (preview truncated)"
            body = Syntax(shown, preview_lang, theme="monokai", word_wrap=True, background_color="default")
        else:
            body = Text("")
        self.console.print(Panel(body, title=Text(title, style="bold"), title_align="left", border_style="yellow"))
        if not self.interactive or not sys.stdin.isatty():
            self.console.print(Text("  Declined (no terminal to ask; use --yes to allow).", style="warn"))
            return Decision(False, "Running non-interactively; approval not available.")
        while True:
            try:
                answer = self.console.input("[warn]  Allow?[/] [bold]y[/]es / [bold]n[/]o / [bold]a[/]lways this session: ").strip().lower()
            except EOFError:
                return Decision(False)
            if answer in ("y", "yes", ""):
                return Decision(True)
            if answer in ("a", "always"):
                return Decision(True, always=True)
            if answer in ("n", "no"):
                feedback = self.console.input("[muted]  What should Baymon do instead? (Enter to skip): [/]").strip()
                return Decision(False, feedback)

    # ------------------------------------------------------------ general

    def info(self, message: str) -> None:
        self.console.print(Text(message, style="muted"))

    def warn(self, message: str) -> None:
        self._stop_spin()
        self.console.print(Text(f"⚠ {message}", style="warn"))

    def error(self, message: str) -> None:
        self._stop_spin()
        self.console.print(Text(f"✖ {message}", style="err"))

    def markdown(self, text: str) -> None:
        self.console.print(Markdown(text, code_theme="monokai"))

"""Interactive session: prompt, slash commands, type and talk modes."""

from __future__ import annotations

import getpass
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from rich.table import Table
from rich.text import Text

from baymon import __version__
from baymon.cli import audio
from baymon.cli.agent import Agent, TurnResult
from baymon.cli.client import ApiError, Client
from baymon.cli.config import VOICES, Config, config_dir
from baymon.cli.ui import UI

COMMANDS: dict[str, str] = {
    "/help": "Show commands",
    "/model": "Show or switch the model: /model [name]",
    "/talk": "Switch to hands-free voice",
    "/type": "Switch to typing",
    "/voice": "Show or set the speaking voice: /voice [name]",
    "/memory": "List what Baymon remembers",
    "/remember": "Save a memory: /remember <text>",
    "/forget": "Delete a memory: /forget <id>",
    "/import": "Learn from a notes file: /import <path>",
    "/clear": "Start a new conversation",
    "/compact": "Summarize the conversation to free up context",
    "/auto": "Toggle auto-approving edits and commands",
    "/cd": "Change the working directory: /cd <path>",
    "/reasoning": "Toggle showing the model's reasoning",
    "/whoami": "Show the signed-in account",
    "/logout": "Sign out",
    "/quit": "Exit (or Ctrl+D)",
}


LOGIN_METHODS = {"1": "email", "2": "token"}


def _choose_method(ui: UI) -> str:
    console = ui.console
    console.print(Text("How do you want to sign in?", style="bold"))
    console.print("  1. Email login")
    console.print(Text.assemble("  2. Token access ", ("(coming soon)", "muted")))
    while True:
        choice = console.input("Choose [1]: ").strip() or "1"
        if choice in LOGIN_METHODS:
            return LOGIN_METHODS[choice]
        ui.error("Enter 1 or 2.")


def _email_login(client: Client, ui: UI) -> bool:
    console = ui.console
    for _ in range(3):
        email = console.input("Email: ").strip()
        password = getpass.getpass("Password: ")
        try:
            creds = client.login(email, password)
        except ApiError as exc:
            ui.error(exc.message)
            continue
        console.print(Text(f"Signed in as {creds.email}.", style="ok"))
        return True
    return False


def sign_in(client: Client, ui: UI, ask_server: bool = False, method: str | None = None) -> bool:
    """Interactive sign-in with your Supabase account. Returns True on success."""
    console = ui.console
    if ask_server:
        url = console.input(f"Baymon server URL [{client.config.api_url}]: ").strip()
        if url:
            client.config.set("api_url", url)
            client.config.save()
            client.set_api_url(client.config.api_url)
    if not client.health():
        ui.error(f"Can't reach the Baymon server at {client.config.api_url}.")
        ui.info("Start it with `uv run baymon-api`, or point the CLI at yours: baymon config api_url https://…")
        return False
    method = method or _choose_method(ui)
    if method == "token":
        ui.warn("Token access isn't available yet. Use email login for now.")
        return False
    return _email_login(client, ui)


class Session:
    def __init__(self, client: Client, config: Config, ui: UI, cwd: Path, mode: str) -> None:
        self.client = client
        self.config = config
        self.ui = ui
        self.agent = Agent(client, config, ui, cwd)
        self.mode = mode
        self.last_reply = ""
        self._prompt: Callable[[], str] = self._make_prompt()

    # ------------------------------------------------------------ input

    def _toolbar(self) -> str:
        cwd = str(self.agent.cwd).replace(str(Path.home()), "~")
        auto = " · auto-approve" if self.config.auto_approve else ""
        return f" {self.config.model} · {self.mode} mode · {cwd}{auto} · /help "

    def _make_prompt(self) -> Callable[[], str]:
        if not sys.stdin.isatty():
            return lambda: input()
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        config_dir().mkdir(parents=True, exist_ok=True)
        keys = KeyBindings()

        @keys.add("escape", "enter")
        def _newline(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.insert_text("\n")

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(config_dir() / "history")),
            completer=WordCompleter(list(COMMANDS), sentence=True, match_middle=False),
            complete_while_typing=True,
            key_bindings=keys,
            bottom_toolbar=lambda: self._toolbar(),
        )
        return lambda: session.prompt(HTML("<ansimagenta><b>❯ </b></ansimagenta>"))

    # ------------------------------------------------------------- loop

    def run(self, first_prompt: str | None = None) -> None:
        self._banner()
        if first_prompt:
            self.handle(first_prompt)
        while True:
            try:
                if self.mode == "talk":
                    line = self._listen()
                    if line is None:
                        continue
                else:
                    self.ui.console.print()
                    line = self._prompt()
            except EOFError:
                break
            except KeyboardInterrupt:
                if self.mode == "talk":
                    self.ui.console.print()
                    break
                continue
            if line.strip() in ("/quit", "/exit"):
                break
            self.handle(line)
        self.agent.wait_for_background()
        self.ui.info("Bye.")

    def _banner(self) -> None:
        email = self.client.creds.email if self.client.creds else "?"
        self.ui.console.print(Text.assemble(
            ("Baymon ", "accent"), (f"v{__version__}", "muted"), ("  ·  ", "muted"), (email, ""),
        ))
        self.ui.console.print(Text(
            f"{self.config.model} · {self.mode} mode · {self.agent.cwd}  —  /help for commands, "
            "Esc+Enter for a new line", style="muted",
        ))

    def handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith("/"):
            try:
                self.command(line)
            except ApiError as exc:
                self.ui.error(exc.message)
            return
        self.turn(line)

    def turn(self, text: str) -> None:
        try:
            result = self.agent.run(text, self.mode)
        except ApiError as exc:
            self.ui.error(exc.message)
            if exc.code == "unauthorized":
                sign_in(self.client, self.ui)
            return
        except KeyboardInterrupt:
            self.ui.warn("Interrupted.")
            return
        if result.interrupted:
            return
        self.last_reply = result.text
        self._footer(result)
        if self.mode == "talk" and result.text:
            self._speak(result.text)
        if result.requested_mode and result.requested_mode != self.mode:
            self.set_mode(result.requested_mode)

    def _footer(self, result: TurnResult) -> None:
        usage = result.usage or {}
        if usage.get("total_tokens"):
            self.ui.info(f"  {self.config.model} · {usage.get('prompt_tokens', 0):,} in · "
                         f"{usage.get('completion_tokens', 0):,} out")

    # ------------------------------------------------------------- voice

    def _listen(self) -> str | None:
        console = self.ui.console
        console.print()
        console.print(Text.assemble(("you ", "you"), ("🎙 ", ""), ("(speak, or type a message/command) ", "muted")), end="")
        tail = re.sub(r"[`*#>|_]+", "", self.last_reply)[-400:]

        def transcribe(wav: bytes, prompt: str) -> str:
            return self.client.transcribe(wav, prompt or tail or None)

        try:
            heard = audio.listen(transcribe, lambda t: console.print(t, end=" ", soft_wrap=True),
                                 no_speech_timeout_ms=None)
        except audio.NothingHeard:
            console.print()
            return None
        except audio.AudioError as exc:
            console.print()
            self.ui.error(str(exc))
            self.set_mode("type")
            return None
        except ApiError as exc:
            console.print()
            self.ui.error(exc.message)
            return None
        if not heard.typed:
            console.print()
        return heard.text

    def _speak(self, text: str) -> None:
        try:
            caption, wav, warnings = self.client.speak_reply(text, self.config.voice)
        except ApiError as exc:
            self.ui.warn(f"Couldn't speak the reply: {exc.message}")
            return
        for w in warnings:
            self.ui.warn(w.get("message", ""))
        if caption.strip() != text.strip():
            self.ui.info(f"🔊 {caption}")
        if wav:
            try:
                audio.play(wav)
            except audio.AudioError as exc:
                self.ui.warn(str(exc))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.ui.info("Talk mode — just speak. Type /type to switch back." if mode == "talk" else "Type mode.")

    # ---------------------------------------------------------- commands

    def command(self, line: str) -> None:
        name, _, arg = line.partition(" ")
        arg = arg.strip()
        ui, console = self.ui, self.ui.console
        if name == "/help":
            table = Table(show_header=False, box=None, padding=(0, 2))
            for cmd, desc in COMMANDS.items():
                table.add_row(Text(cmd, style="accent"), desc)
            console.print(table)
            ui.info("Esc+Enter inserts a new line. Ctrl+C interrupts a reply; Ctrl+D exits.")
        elif name == "/model":
            models = self.client.models()
            keys = [m["key"] for m in models]
            if arg:
                if arg not in keys:
                    ui.error(f"Unknown model {arg!r}. Choose: {', '.join(keys)}")
                    return
                self.config.model = arg
                self.config.save()
                ui.info(f"Model: {arg}")
                return
            for i, m in enumerate(models, 1):
                mark = "●" if m["key"] == self.config.model else "○"
                console.print(f"  {mark} {i}. [bold]{m['key']}[/]  [muted]{m['label']}[/]")
            choice = console.input("Pick a number (Enter to keep): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(models):
                self.config.model = models[int(choice) - 1]["key"]
                self.config.save()
                ui.info(f"Model: {self.config.model}")
        elif name in ("/talk", "/type"):
            self.set_mode(name[1:])
        elif name == "/voice":
            if not arg:
                ui.info(f"Voice: {self.config.voice}  (options: {', '.join(VOICES)})")
            elif arg in VOICES:
                self.config.voice = arg
                self.config.save()
                ui.info(f"Voice: {arg}")
            else:
                ui.error(f"Choose one of: {', '.join(VOICES)}")
        elif name == "/memory":
            items = self.client.memory_list()
            if not items:
                ui.info("Nothing remembered yet.")
            for m in items:
                console.print(Text.assemble((f"  [{m['id'][:8]}] ", "muted"), (f"({m['category']}) ", "tool"), m["content"]))
        elif name == "/remember":
            if not arg:
                ui.error("Usage: /remember <text>")
                return
            self.client.memory_add(arg)
            ui.info("Saved.")
        elif name == "/forget":
            items = self.client.memory_list()
            match = [m for m in items if arg and m["id"].startswith(arg)]
            if len(match) != 1:
                ui.error("Give the memory id (or a unique prefix) from /memory.")
                return
            self.client.memory_delete(match[0]["id"])
            ui.info(f"Forgot: {match[0]['content']}")
        elif name == "/import":
            path = Path(os.path.expanduser(arg)) if arg else None
            if not path or not path.is_file():
                ui.error("Usage: /import <path to a text file>")
                return
            items = self.client.memory_import(path.read_text(encoding="utf-8", errors="replace")[:20_000])
            ui.info(f"Learned {len(items)} thing(s):")
            for m in items:
                console.print(f"  • {m['content']}")
        elif name == "/clear":
            self.agent.reset()
            self.last_reply = ""
            ui.info("New conversation.")
        elif name == "/compact":
            try:
                self.agent.compact()
            except KeyboardInterrupt:
                ui.warn("Cancelled.")
                return
            ui.info("Conversation compacted.")
        elif name == "/auto":
            self.config.auto_approve = not self.config.auto_approve
            ui.info("Auto-approve ON: edits and commands run without asking." if self.config.auto_approve
                    else "Auto-approve off: Baymon asks before edits and commands.")
        elif name == "/cd":
            target = (self.agent.cwd / os.path.expanduser(arg or str(Path.home()))).resolve()
            if not target.is_dir():
                ui.error(f"Not a directory: {target}")
                return
            self.agent.set_cwd(target)
            ui.info(f"Working directory: {target}")
        elif name == "/reasoning":
            ui.show_reasoning = not ui.show_reasoning
            ui.info(f"Reasoning {'shown' if ui.show_reasoning else 'hidden'}.")
        elif name == "/whoami":
            me = self.client.me()
            ui.info(f"{me.get('email')} ({me.get('id')}) on {self.config.api_url}")
        elif name == "/logout":
            self.client.logout()
            ui.info("Signed out.")
            raise SystemExit(0)
        else:
            ui.error(f"Unknown command {name}. Try /help.")

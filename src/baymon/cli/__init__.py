"""`baymon` command-line entry point.

    baymon                      interactive session (type mode by default)
    baymon "fix the tests"      interactive session starting with a prompt
    baymon -p "question"        answer once and exit (stdin is appended if piped)
    baymon login [--email | --token] [--server]
    baymon logout | whoami
    baymon config [key [value]]
    baymon memory [list | add <text> | rm <id> | import <file>]
    baymon mic-check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from baymon import __version__


def _setup_console() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass


def _context() -> tuple:
    from baymon.cli.client import Client
    from baymon.cli.config import Config, Credentials
    from baymon.cli.ui import UI

    config = Config.load()
    ui = UI(interactive=sys.stdout.isatty())
    return config, Client(config, Credentials.load()), ui


def cmd_login(args: list[str]) -> int:
    from baymon.cli.repl import sign_in

    config, client, ui = _context()
    method = "email" if "--email" in args else "token" if "--token" in args else None
    ask_server = "--server" in args or not client.health()
    return 0 if sign_in(client, ui, ask_server=ask_server, method=method) else 1


def cmd_logout(args: list[str]) -> int:
    config, client, ui = _context()
    client.logout()
    ui.info("Signed out.")
    return 0


def cmd_whoami(args: list[str]) -> int:
    from baymon.cli.client import ApiError

    config, client, ui = _context()
    if not client.signed_in:
        ui.info(f"Not signed in (server: {config.api_url}). Run `baymon login`.")
        return 1
    try:
        me = client.me()
    except ApiError as exc:
        ui.error(exc.message)
        return 1
    ui.console.print(f"{me.get('email')}  ({me.get('id')})\nserver: {config.api_url}\nmodel:  {config.model}")
    return 0


def cmd_config(args: list[str]) -> int:
    from dataclasses import asdict

    from baymon.cli.config import config_dir

    config, client, ui = _context()
    if not args:
        for key, value in asdict(config).items():
            ui.console.print(f"{key:13} {value}")
        ui.info(f"({config_dir() / 'config.json'})")
        return 0
    if len(args) == 1:
        if not hasattr(config, args[0]):
            ui.error(f"Unknown setting {args[0]!r}.")
            return 1
        ui.console.print(str(getattr(config, args[0])))
        return 0
    try:
        config.set(args[0], " ".join(args[1:]))
    except KeyError:
        ui.error(f"Unknown setting {args[0]!r}. Settings: api_url, model, mode, voice, auto_approve")
        return 1
    except ValueError as exc:
        ui.error(str(exc))
        return 1
    config.save()
    ui.info(f"{args[0]} = {getattr(config, args[0])}")
    return 0


def cmd_memory(args: list[str]) -> int:
    from baymon.cli.client import ApiError

    config, client, ui = _context()
    action = args[0] if args else "list"
    try:
        if action == "list":
            for m in client.memory_list():
                ui.console.print(f"[{m['id'][:8]}] ({m['category']}) {m['content']}", highlight=False)
        elif action == "add" and len(args) > 1:
            m = client.memory_add(" ".join(args[1:]))
            ui.info(f"Saved [{m['id'][:8]}].")
        elif action in ("rm", "forget") and len(args) == 2:
            matches = [m for m in client.memory_list() if m["id"].startswith(args[1])]
            if len(matches) != 1:
                ui.error("No unique memory with that id.")
                return 1
            client.memory_delete(matches[0]["id"])
            ui.info(f"Forgot: {matches[0]['content']}")
        elif action == "import" and len(args) == 2:
            text = Path(args[1]).expanduser().read_text(encoding="utf-8", errors="replace")[:20_000]
            for m in client.memory_import(text):
                ui.console.print(f"+ {m['content']}")
        else:
            ui.error("Usage: baymon memory [list | add <text> | rm <id> | import <file>]")
            return 1
    except ApiError as exc:
        ui.error(exc.message)
        return 1
    return 0


def cmd_mic_check(args: list[str]) -> int:
    from baymon.cli import audio

    _, _, ui = _context()
    ui.info("Stay quiet for a few seconds, then talk. Ctrl+C to stop.")
    try:
        audio.monitor(lambda line: print("\r" + line, end="", flush=True))
    except audio.AudioError as exc:
        ui.error(str(exc))
        return 1
    print()
    return 0


SUBCOMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "whoami": cmd_whoami,
    "config": cmd_config,
    "memory": cmd_memory,
    "mic-check": cmd_mic_check,
}


def _stdin_has_data() -> bool:
    """True if input was piped or redirected (and isn't an idle, never-closing pipe)."""
    if sys.stdin is None or sys.stdin.isatty():
        return False
    import os
    import stat

    try:
        st_mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return False
    if stat.S_ISREG(st_mode):
        return True
    if sys.platform == "win32":
        return stat.S_ISFIFO(st_mode)
    import select

    ready, _, _ = select.select([sys.stdin], [], [], 0.25)
    return bool(ready)


def _one_shot(prompt: str, config, client, ui, cwd: Path) -> int:  # type: ignore[no-untyped-def]
    from baymon.cli.agent import Agent
    from baymon.cli.client import ApiError

    if _stdin_has_data():
        piped = sys.stdin.read()
        if piped.strip():
            prompt = f"{prompt}\n\n<stdin>\n{piped[:200_000]}\n</stdin>"
    agent = Agent(client, config, ui, cwd)
    try:
        result = agent.run(prompt, "type")
    except ApiError as exc:
        ui.error(exc.message)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        agent.wait_for_background()
    return 0 if not result.interrupted else 130


def main(argv: list[str] | None = None) -> None:
    _setup_console()
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in SUBCOMMANDS:
        sys.exit(SUBCOMMANDS[argv[0]](argv[1:]))

    parser = argparse.ArgumentParser(
        prog="baymon",
        description="Baymon, your personal AI companion and coding agent.",
        epilog="Other commands: baymon login | logout | whoami | config [key [value]] | "
        "memory [list|add|rm|import] | mic-check",
    )
    parser.add_argument("prompt", nargs="*", help="start with this message")
    parser.add_argument("-p", "--print", action="store_true", help="answer once and exit (non-interactive)")
    parser.add_argument("-m", "--model", help="model to use (e.g. gpt-oss-120b, minimax-m2.7)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--talk", action="store_true", help="start in hands-free voice mode")
    mode.add_argument("--type", action="store_true", help="start in typing mode")
    parser.add_argument("-y", "--yes", action="store_true", help="auto-approve file edits and commands")
    parser.add_argument("-C", "--cd", metavar="DIR", help="working directory")
    parser.add_argument("--show-reasoning", action="store_true", help="print the model's reasoning")
    parser.add_argument("--version", action="version", version=f"baymon {__version__}")
    args = parser.parse_args(argv)

    from baymon.cli.repl import Session, sign_in

    config, client, ui = _context()
    ui.show_reasoning = args.show_reasoning
    if args.model:
        config.model = args.model
    if args.yes:
        config.auto_approve = True
    cwd = Path(args.cd).expanduser().resolve() if args.cd else Path.cwd()
    if not cwd.is_dir():
        ui.error(f"Not a directory: {cwd}")
        sys.exit(2)

    prompt = " ".join(args.prompt).strip()
    if args.print:
        if not prompt:
            ui.error("-p needs a prompt, e.g. baymon -p \"summarize this repo\"")
            sys.exit(2)
        if not client.signed_in:
            ui.error("Not signed in. Run `baymon login` first.")
            sys.exit(1)
        ui.interactive = False
        sys.exit(_one_shot(prompt, config, client, ui, cwd))

    if not client.signed_in and not sign_in(client, ui):
        sys.exit(1)
    start_mode = "talk" if args.talk else "type" if args.type else config.mode
    Session(client, config, ui, cwd, start_mode).run(prompt or None)

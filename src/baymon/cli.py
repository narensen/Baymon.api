"""Terminal chat for trying out the services layer.

Two modes:
  talk (default)  hands-free: just speak. Text appears as you pause, your turn ends
                  after ~1.5 s of silence, the reply is spoken, and Baymon listens
                  again. Stays on until you switch modes.
  type            type messages; replies are text only

Switch by typing /talk or /type (typing works while Baymon is listening too),
or just ask: "switch to typing", "let's talk instead".

Other commands:
  /memory   show what Baymon remembers about you
  /quit     exit (Ctrl+C also quits)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile

from baymon import mic, services
from baymon.services import ServiceError

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _play(audio: bytes) -> None:
    player = next((p for p in ("pw-play", "aplay") if shutil.which(p)), None)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio)
    if player is None:
        print(f"{DIM}(no audio player found; saved reply to {f.name}){RESET}")
        return
    try:
        subprocess.run([player, f.name], stderr=subprocess.DEVNULL)
    except KeyboardInterrupt:
        pass
    finally:
        os.unlink(f.name)


def _show_memory(user_id: str | None) -> None:
    if not user_id:
        print(f"{DIM}Memory is off. Set BAYMON_USER_ID in .env to enable it.{RESET}")
        return
    memory = services.retrieve_memory(user_id)
    if not memory:
        print(f"{DIM}Nothing remembered yet.{RESET}")
    for m in memory:
        print(f"{DIM}- [{m.get('category', 'other')}] {m.get('content')}{RESET}")


def _mode_label(talk_mode: bool) -> str:
    return "Talk mode — just speak." if talk_mode else "Type mode."


def _listen() -> str:
    """Listen hands-free until the user finishes a turn; returns what they said or typed."""
    print(f"\n{BOLD}you>{RESET} {DIM}🎙{RESET}", end="", flush=True)
    try:
        heard = mic.stream_transcribe(
            lambda t: print(f" {t}", end="", flush=True), no_speech_timeout_ms=None
        )
    except BaseException:
        print()
        raise
    if not heard.typed:
        print()
    return heard.text


def main() -> None:
    parser = argparse.ArgumentParser(prog="baymon", description="Chat with Baymon.")
    parser.add_argument("--type", action="store_true", help="start in type mode instead of talk mode")
    parser.add_argument("--user", default=os.getenv("BAYMON_USER_ID"), help="Supabase user id (enables memory)")
    parser.add_argument("--mic-check", action="store_true", help="show live mic levels and speech detection")
    args = parser.parse_args()

    if args.mic_check:
        try:
            mic.monitor()
        except ServiceError as exc:
            print(f"⚠  {exc.message}")
        return

    user_id: str | None = args.user
    talk_mode: bool = not args.type
    history: list[dict[str, str]] = []

    print(f"{BOLD}Baymon{RESET} {DIM}— /talk, /type, /memory, /quit{RESET}")
    if not user_id:
        print(f"{DIM}Memory is off (no BAYMON_USER_ID).{RESET}")
    print(f"{DIM}{_mode_label(talk_mode)}{RESET}")

    while True:
        try:
            line = _listen() if talk_mode else input(f"\n{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except ServiceError as exc:
            if exc.code == "transcription_empty":
                continue  # noise, not speech
            print(f"⚠  {exc.message} {DIM}[{exc.code}]{RESET}")
            if exc.code == "mic_unavailable":
                talk_mode = False
                print(f"{DIM}{_mode_label(talk_mode)}{RESET}")
            continue

        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line in ("/talk", "/type"):
            talk_mode = line == "/talk"
            print(f"{DIM}{_mode_label(talk_mode)}{RESET}")
            continue
        if line == "/memory":
            try:
                _show_memory(user_id)
            except ServiceError as exc:
                print(f"⚠  {exc.message} {DIM}[{exc.code}]{RESET}")
            continue

        try:
            print(f"{DIM}thinking…{RESET}", end="\r", flush=True)
            context = {"history": history, "mode": "talk" if talk_mode else "type"}
            result = services.run_turn(user_id, line, context, voice_mode=talk_mode)
        except ServiceError as exc:
            print(f"⚠  {exc.message} {DIM}[{exc.code}]{RESET}")
            continue
        except KeyboardInterrupt:
            print(f"\n{DIM}(cancelled){RESET}")
            continue

        print(f"{BOLD}baymon>{RESET} {result.text}")
        for err in result.errors:
            print(f"⚠  {err.message} {DIM}[{err.code}]{RESET}")
        if result.memory_stored:
            print(f"{DIM}(saved to memory){RESET}")
        if result.audio:
            if result.speech_text != result.text:
                print(f"{DIM}🔊 {result.speech_text}{RESET}")
            _play(result.audio)

        history += [{"role": "user", "content": line}, {"role": "assistant", "content": result.text}]

        if result.mode:
            talk_mode = result.mode == "talk"
            print(f"{DIM}{_mode_label(talk_mode)}{RESET}")

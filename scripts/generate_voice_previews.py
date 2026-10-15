"""Pre-record voice previews so the app can play them without calling the TTS API.

    uv run python scripts/generate_voice_previews.py           # missing clips only
    uv run python scripts/generate_voice_previews.py --force   # re-record everything

Writes src/baymon/static/voice_previews/<voice>/<n>.wav (served at /voice-previews/...).
Edit PREVIEW_LINES in baymon/services.py to change what is said, then rerun with --force.
"""

from __future__ import annotations

import sys
from pathlib import Path

from baymon import services

OUT = Path(__file__).resolve().parent.parent / "src" / "baymon" / "static" / "voice_previews"


def main() -> None:
    force = "--force" in sys.argv
    made = 0
    for voice in services.VOICE_IDS:
        for n, line in enumerate(services.PREVIEW_LINES, 1):
            path = OUT / voice / f"{n}.wav"
            if path.exists() and not force:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(services.generate_audio(line, {"voice": voice}))
            made += 1
            print(f"recorded {voice}/{n}.wav ({path.stat().st_size // 1024} KB)")
    print(f"done: {made} new clip(s) in {OUT}")


if __name__ == "__main__":
    main()

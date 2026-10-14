"""Per-user settings and saved sign-in, stored in the OS config directory.

Linux:   $XDG_CONFIG_HOME/baymon  (default ~/.config/baymon)
macOS:   ~/Library/Application Support/Baymon
Windows: %APPDATA%\\Baymon
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

try:  # written by the packaging scripts so builds can ship with a default server
    from baymon.cli._build import DEFAULT_API_URL  # type: ignore[import-not-found]
except ImportError:
    DEFAULT_API_URL = "https://baymon-api.onrender.com"

VOICES = ("troy", "austin", "daniel", "autumn", "diana", "hannah")


def config_dir() -> Path:
    if override := os.getenv("BAYMON_CONFIG_DIR"):
        return Path(override)
    if sys.platform == "win32":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "Baymon"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Baymon"
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "baymon"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if private and sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(path)


@dataclass
class Config:
    api_url: str = DEFAULT_API_URL
    model: str = "gpt-oss-120b"
    mode: str = "type"  # "type" | "talk"
    voice: str = "troy"
    auto_approve: bool = False

    @classmethod
    def load(cls) -> Config:
        data = _read(config_dir() / "config.json")
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        if env_url := os.getenv("BAYMON_API_URL"):
            cfg.api_url = env_url
        cfg.api_url = cfg.api_url.rstrip("/")
        return cfg

    def save(self) -> None:
        _write(config_dir() / "config.json", asdict(self))

    def set(self, key: str, value: str) -> None:
        """Set a field from a string (for `baymon config key value`)."""
        current = getattr(self, key, None)
        if key not in {f.name for f in fields(self)}:
            raise KeyError(key)
        if isinstance(current, bool):
            setattr(self, key, value.lower() in ("1", "true", "yes", "on"))
        elif key == "mode" and value not in ("type", "talk"):
            raise ValueError("mode must be 'type' or 'talk'")
        elif key == "voice" and value not in VOICES:
            raise ValueError(f"voice must be one of: {', '.join(VOICES)}")
        else:
            setattr(self, key, value.rstrip("/") if key == "api_url" else value)


@dataclass
class Credentials:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    user_id: str = ""
    email: str = ""
    api_url: str = ""  # server these tokens belong to
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Credentials | None:
        data = _read(config_dir() / "credentials.json")
        if not data.get("refresh_token"):
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        _write(config_dir() / "credentials.json", asdict(self), private=True)

    @staticmethod
    def clear() -> None:
        try:
            (config_dir() / "credentials.json").unlink()
        except FileNotFoundError:
            pass

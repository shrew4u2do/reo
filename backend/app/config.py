"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(val: str | None, default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("REOLINK_HOST", "")
    port: int = int(os.getenv("REOLINK_PORT", "443"))
    username: str = os.getenv("REOLINK_USERNAME", "admin")
    password: str = os.getenv("REOLINK_PASSWORD", "")
    use_https: bool = _bool(os.getenv("REOLINK_USE_HTTPS"), True)


settings = Settings()

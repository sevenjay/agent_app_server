"""Application configuration shared by every module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dynaconf import Dynaconf


BASE_DIR = Path(__file__).resolve().parent

settings = Dynaconf(
    settings_files=[
        str(BASE_DIR / "settings.toml"),
        str(BASE_DIR / ".secrets.toml"),
    ],
    environments=True,
)


def environment_settings(settings_obj: Any = settings) -> Any:
    """Return the already-selected Dynaconf environment settings."""
    return settings_obj

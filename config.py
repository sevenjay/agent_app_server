"""Application configuration shared by every module."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from dynaconf import Dynaconf


BASE_DIR = Path(__file__).resolve().parent
with (BASE_DIR / "pyproject.toml").open("rb") as pyproject_file:
    APP_VERSION = str(tomllib.load(pyproject_file)["project"]["version"])

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

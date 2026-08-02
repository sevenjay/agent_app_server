"""Runtime logging and Uvicorn bootstrap helpers."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
import re
import tempfile
import time
from typing import Any

import uvicorn
from fastapi import FastAPI
from utility.log import LOGI
from uvicorn.logging import AccessFormatter, DefaultFormatter

from config import BASE_DIR, environment_settings


LOG_DIRECTORY = BASE_DIR / "logs"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_QUERY_IN_MESSAGE_PATTERN = re.compile(r"\?[^\s\"']+")


def ensure_log_directory(log_directory: str | Path = LOG_DIRECTORY) -> Path:
    path = Path(log_directory)
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-test-", dir=path):
            pass
    except OSError as exc:
        raise RuntimeError(f"Log directory is not writable: {path}") from exc
    return path


def _redact_access_path(full_path: object) -> str:
    normalized = _CONTROL_CHARACTER_PATTERN.sub(
        lambda match: {"\t": r"\t", "\n": r"\n", "\r": r"\r"}.get(
            match.group(),
            f"\\x{ord(match.group()):02x}",
        ),
        str(full_path),
    )
    path, separator, _query = normalized.partition("?")
    return f"{path}?<redacted>" if separator else path


class RedactingErrorFormatter(DefaultFormatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return _QUERY_IN_MESSAGE_PATTERN.sub("?<redacted>", message)


class RedactingAccessFormatter(AccessFormatter):
    converter = time.gmtime

    def formatMessage(self, record: logging.LogRecord) -> str:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            record = copy.copy(record)
            client_addr, method, full_path, http_version, status_code = record.args
            record.args = (
                client_addr,
                method,
                _redact_access_path(full_path),
                http_version,
                status_code,
            )
        return super().formatMessage(record)


def build_uvicorn_log_config(log_directory: str | Path) -> dict[str, Any]:
    path = Path(log_directory)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": RedactingErrorFormatter,
                "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                "datefmt": LOG_DATE_FORMAT,
                "use_colors": False,
            },
            "access": {
                "()": RedactingAccessFormatter,
                "fmt": '%(asctime)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": LOG_DATE_FORMAT,
                "use_colors": False,
            },
        },
        "handlers": {
            "default_console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access_console": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "stream": "ext://sys.stdout",
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": str(path / "uvicorn-error.log"),
                "maxBytes": LOG_MAX_BYTES,
                "backupCount": LOG_BACKUP_COUNT,
                "encoding": "utf-8",
            },
            "access_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "access",
                "filename": str(path / "uvicorn-access.log"),
                "maxBytes": LOG_MAX_BYTES,
                "backupCount": LOG_BACKUP_COUNT,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default_console", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": [],
                "level": "INFO",
                "propagate": True,
            },
            "uvicorn.access": {
                "handlers": ["access_console", "access_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


def run_web_server(app: FastAPI, *, log_directory: str | Path) -> None:
    selected = environment_settings()
    host = str(getattr(selected, "web_host", "127.0.0.1"))
    port = int(getattr(selected, "web_port", 8888))
    LOGI(f"Starting web application at http://{host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=build_uvicorn_log_config(log_directory),
        access_log=True,
    )

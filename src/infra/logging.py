"""Centralized logging configuration utilities."""

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path
from typing import Any, Dict

from pythonjsonlogger import jsonlogger

_CONFIGURED = False


class RuntimeJsonFormatter(jsonlogger.JsonFormatter):
    """Adds run metadata to each structured log line."""

    def __init__(self, run_id: str | None, environment: str | None) -> None:
        super().__init__()
        self._run_id = run_id
        self._environment = environment

    def add_fields(
        self,
        log_record: Dict[str, Any],
        record: logging.LogRecord,
        message_dict: Dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("run_id", self._run_id)
        log_record.setdefault("environment", self._environment)
        log_record.setdefault("logger", record.name)
        log_record.setdefault("level", record.levelname)


def configure_logging(*, run_id: str | None = None, environment: str | None = None) -> None:
    """Configure console + rotating JSON file logging."""

    global _CONFIGURED
    if _CONFIGURED:
        return

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_dir = Path(os.environ.get("LOG_DIR", "storage/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "agenthedge.log"

    logging_config: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": RuntimeJsonFormatter,
                "run_id": run_id,
                "environment": environment,
            },
            "console": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level,
                "formatter": "console",
            },
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "level": level,
                "when": "midnight",
                "backupCount": int(os.environ.get("LOG_RETENTION_DAYS", "7")),
                "filename": str(log_path),
                "encoding": "utf-8",
                "formatter": "json",
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "file"],
        },
    }

    logging.config.dictConfig(logging_config)
    _CONFIGURED = True


__all__ = ["configure_logging", "RuntimeJsonFormatter"]

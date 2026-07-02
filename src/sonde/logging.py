"""Logging configuration — plain or JSON, with log injection protection."""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any


class PlainFormatter(logging.Formatter):
    """Message-only formatter that escapes control chars to block log injection.

    Leading newlines are preserved (phase banners use them for visual separation).
    """

    _ESCAPES = str.maketrans(
        {
            **{c: f"\\x{c:02x}" for c in range(0x20) if c != 0x09},
            0x0A: "\\n",
            0x0D: "\\r",
            0x7F: "\\x7f",
        }
    )

    def __init__(self) -> None:
        super().__init__(fmt="%(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:
        msg = super().formatMessage(record)
        stripped = msg.lstrip("\n")
        leading = len(msg) - len(stripped)
        return "\n" * leading + stripped.translate(self._ESCAPES)


class JsonFormatter(logging.Formatter):
    """Single-line JSON log output for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(*, level: int = logging.INFO, fmt: str = "plain") -> None:
    """Configure stdlib logging. Call once at startup."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {"()": PlainFormatter},
                "json": {"()": JsonFormatter},
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": fmt,
                },
            },
            "root": {
                "level": level,
                "handlers": ["stderr"],
            },
        }
    )

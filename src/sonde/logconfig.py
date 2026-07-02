"""Logging configuration — plain or JSON, with log injection protection."""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any


class PlainFormatter(logging.Formatter):
    """Message-only formatter that escapes control chars in the message body."""

    # Escape C0 controls (0x00-0x1F, keeping tab), CR/LF as readable \r/\n, DEL,
    # and C1 controls (0x80-0x9F) — all of which can drive terminal escape
    # sequences or forge log lines from untrusted server responses.
    _ESCAPES = str.maketrans(
        {
            **{c: f"\\x{c:02x}" for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)},
            0x0A: "\\n",
            0x0D: "\\r",
            0x7F: "\\x7f",
            **{c: f"\\x{c:02x}" for c in range(0x80, 0xA0)},
        }
    )

    # Same neutralisation for exception/stack text, but keep the traceback's own
    # \n and \t so multi-line tracebacks stay readable; only embedded controls
    # (e.g. an ESC smuggled into an exception message) are escaped.
    _EXC_ESCAPES = str.maketrans(
        {
            **{c: f"\\x{c:02x}" for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)},
            0x0D: "\\r",
            0x7F: "\\x7f",
            **{c: f"\\x{c:02x}" for c in range(0x80, 0xA0)},
        }
    )

    def __init__(self) -> None:
        # Plain format is intentionally message-only (no timestamp/level prefix):
        # it replaces the tool's former print() calls for interactive terminal use,
        # where those prefixes are noise. The json format carries timestamp/level/
        # logger for aggregators. Deliberate deviation from the logging convention's
        # "timestamps in both formats".
        super().__init__(fmt="%(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:
        msg = super().formatMessage(record)
        # Preserve leading \n (phase banners) but escape embedded control chars.
        stripped = msg.lstrip("\n")
        leading = len(msg) - len(stripped)
        return "\n" * leading + stripped.translate(self._ESCAPES)

    def formatException(self, ei) -> str:
        # Base format() appends this (unescaped) after the message; neutralise
        # control chars while preserving the traceback's structural newlines.
        return super().formatException(ei).translate(self._EXC_ESCAPES)

    def formatStack(self, stack_info: str) -> str:
        return super().formatStack(stack_info).translate(self._EXC_ESCAPES)


class JsonFormatter(logging.Formatter):
    """Single-line JSON log output for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage().lstrip("\n"),
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

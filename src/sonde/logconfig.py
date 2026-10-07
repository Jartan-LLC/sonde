"""Logging configuration — plain or JSON, with log injection protection."""

from __future__ import annotations

import functools
import json
import logging
import logging.config
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, override

# Secret substrings that `scrub` replaces in log output, error text and reported headers.
_SECRETS: list[str] = []
# A shorter value can't be a real credential, and redacting it would mangle ordinary text.
_MIN_SECRET_LEN = 8


# JSON's two-character escapes, by the character each stands for.
_SHORT_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "\b": "b",
    "\f": "f",
    "\n": "n",
    "\r": "r",
    "\t": "t",
}


def register_log_secrets(values: Iterable[str]) -> None:
    """Register secret substrings for `scrub` to replace from now on.

    Values too short to be a credential are skipped.
    """
    for v in values:
        if len(v) >= _MIN_SECRET_LEN and v not in _SECRETS:
            _SECRETS.append(v)
    # Longest first, so where two secrets start at the same place the longer is redacted.
    _SECRETS.sort(key=len, reverse=True)


def _unicode_escape(code: int) -> str:
    """Return a regex for the JSON unicode escape of `code`, its hex digits in either case."""
    digits = "".join(f"[{d}{d.upper()}]" if d.isalpha() else d for d in f"{code:04x}")
    return re.escape("\\") + "u" + digits


def _echo_pattern(secret: str) -> str:
    """Return a regex for `secret` with any of its characters JSON-escaped, or none."""
    parts: list[str] = []
    for c in secret:
        forms: list[str] = []
        if c in _SHORT_ESCAPES:
            forms.append(re.escape("\\" + _SHORT_ESCAPES[c]))
        # JSON escapes UTF-16 code units, so a character outside the BMP is a surrogate pair.
        # surrogatepass: os.environ decodes a non-UTF-8 byte to a lone surrogate.
        utf16 = c.encode("utf-16-be", "surrogatepass")
        units = (int.from_bytes(utf16[i : i + 2]) for i in range(0, len(utf16), 2))
        forms.append("".join(map(_unicode_escape, units)))
        # Raw form last: a raw backslash would otherwise match an escape's first character.
        forms.append(re.escape(c))
        parts.append(f"(?:{'|'.join(forms)})")
    return "".join(parts)


# Keyed on the secrets themselves: setup_logging and the tests clear _SECRETS in place.
@functools.lru_cache(maxsize=1)
def _secrets_pattern(secrets: tuple[str, ...]) -> re.Pattern[str] | None:
    return re.compile("|".join(map(_echo_pattern, secrets))) if secrets else None


def scrub(text: str) -> str:
    """Return `text` with every registered secret replaced by `***`.

    A secret also matches with any of its characters JSON-escaped, so an echo in a JSON
    body is redacted however its encoder escaped it. An echo escaped twice, as in JSON
    nested inside a JSON string, isn't.
    """
    pattern = _secrets_pattern(tuple(_SECRETS))
    return pattern.sub("***", text) if pattern else text


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
        """Format the message alone.

        Plain output is read in a terminal, where a timestamp and level are noise; the
        JSON format carries them for aggregators.
        """
        super().__init__(fmt="%(message)s")

    @override
    def formatMessage(self, record: logging.LogRecord) -> str:
        msg = super().formatMessage(record)
        # Preserve leading \n (phase banners) but escape embedded control chars.
        stripped = msg.lstrip("\n")
        leading = len(msg) - len(stripped)
        return "\n" * leading + scrub(stripped).translate(self._ESCAPES)

    @override
    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        # Base format() appends this (unescaped) after the message; neutralise
        # control chars while preserving the traceback's structural newlines.
        return scrub(super().formatException(ei)).translate(self._EXC_ESCAPES)

    @override
    def formatStack(self, stack_info: str) -> str:
        return scrub(super().formatStack(stack_info)).translate(self._EXC_ESCAPES)


class JsonFormatter(logging.Formatter):
    """Single-line JSON log output for machine consumption."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Strip banner leading \n (see setup_logging's formatter contract) so
            # each record stays a single line.
            "message": scrub(record.getMessage().lstrip("\n")),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(*, level: int = logging.INFO, fmt: str = "plain") -> None:
    r"""Configure stdlib logging. Call once at startup.

    Formatter contract: log messages may carry leading newlines — phase banners
    are emitted as ``logger.info("\n== PHASE ...")`` for interactive spacing. Any
    new formatter registered here must decide how to handle them: PlainFormatter
    preserves them, JsonFormatter strips them to keep each record single-line.

    Args:
        level: The root logger's level.
        fmt: `plain` or `json`.
    """
    _SECRETS.clear()  # reset per run
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

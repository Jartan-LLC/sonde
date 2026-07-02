"""Tests for the logging module: formatters and setup_logging()."""

import json
import logging
import sys

import pytest

from sonde.logconfig import JsonFormatter, PlainFormatter, setup_logging


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_record(msg, level=logging.INFO, name="sonde.test"):
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


@pytest.fixture(autouse=True)
def _auto_restore_logger(restore_root_logger):
    """Autouse wrapper around the shared conftest fixture."""


# --------------------------------------------------------------------------- #
# PlainFormatter
# --------------------------------------------------------------------------- #
class TestPlainFormatter:
    def setup_method(self):
        self.fmt = PlainFormatter()

    def test_escapes_control_chars(self):
        record = _make_record("before\nafter\r\x1b[31mred\x00null\x7fdel")
        result = self.fmt.format(record)
        assert "\\n" in result
        assert "\\r" in result
        assert "\\x1b" in result
        assert "\\x00" in result
        assert "\\x7f" in result
        assert "\n" not in result
        assert "\r" not in result

    def test_preserves_leading_newlines(self):
        record = _make_record("\n== PHASE: sanity / auth ==")
        result = self.fmt.format(record)
        assert result.startswith("\n")
        assert result == "\n== PHASE: sanity / auth =="

    def test_preserves_multiple_leading_newlines(self):
        record = _make_record("\n\nDouble banner")
        result = self.fmt.format(record)
        assert result.startswith("\n\n")
        assert "\\n" not in result.lstrip("\n")

    def test_preserves_tabs(self):
        record = _make_record("col1\tcol2\tcol3")
        result = self.fmt.format(record)
        assert "\t" in result

    def test_escapes_control_chars_from_percent_args(self):
        record = logging.LogRecord(
            name="sonde.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="  headers: %s",
            args=("\x1b[31mred\ninjected",),
            exc_info=None,
        )
        result = self.fmt.format(record)
        assert "\x1b" not in result
        assert "\n" not in result
        assert "\\x1b" in result
        assert "\\n" in result

    def test_clean_message_unchanged(self):
        msg = "  burst=10   200=8    429=2    other=0   in 1.23s"
        record = _make_record(msg)
        assert self.fmt.format(record) == msg

    def test_escapes_c1_control_chars(self):
        record = _make_record("hi\x9b31mred\x85next")
        result = self.fmt.format(record)
        assert "\x9b" not in result
        assert "\x85" not in result
        assert "\\x9b" in result
        assert "\\x85" in result

    def test_escapes_controls_in_exception_but_keeps_newlines(self):
        record = _make_record("boom")
        try:
            raise ValueError("evil\x1b[31m\x9binjected")
        except ValueError:
            record.exc_info = sys.exc_info()
        result = self.fmt.format(record)
        # embedded ESC (C0) and CSI (C1) neutralised in the traceback text...
        assert "\x1b" not in result
        assert "\x9b" not in result
        assert "\\x1b" in result
        assert "\\x9b" in result
        # ...but the traceback stays multi-line (structural newlines preserved)
        assert "\n" in result


# --------------------------------------------------------------------------- #
# JsonFormatter
# --------------------------------------------------------------------------- #
class TestJsonFormatter:
    def setup_method(self):
        self.fmt = JsonFormatter()

    def test_produces_valid_json(self):
        record = _make_record("test message")
        result = self.fmt.format(record)
        parsed = json.loads(result)
        assert parsed["message"] == "test message"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "sonde.test"
        assert "timestamp" in parsed

    def test_timestamp_is_iso(self):
        record = _make_record("ts test")
        parsed = json.loads(self.fmt.format(record))
        from datetime import datetime

        datetime.fromisoformat(parsed["timestamp"])

    def test_includes_exception(self):
        record = _make_record("with exc")
        try:
            raise ValueError("boom")
        except ValueError:
            record.exc_info = sys.exc_info()
        parsed = json.loads(self.fmt.format(record))
        assert "exc" in parsed
        assert "boom" in parsed["exc"]

    def test_single_line(self):
        record = _make_record("line\nbreak")
        result = self.fmt.format(record)
        lines = result.strip().split("\n")
        assert len(lines) == 1

    def test_control_chars_neutralised_by_json_serialisation(self):
        # JsonFormatter has no bespoke escaping — it relies on json.dumps, which
        # always \u-escapes control chars. This verifies that round-trip leaves
        # no raw control chars (and thus a valid single-line record).
        record = _make_record("headers: \x1b[31mred\x00null\nnewline")
        result = self.fmt.format(record)
        assert "\x1b" not in result
        assert "\x00" not in result
        assert "\n" not in result


# --------------------------------------------------------------------------- #
# setup_logging
# --------------------------------------------------------------------------- #
class TestSetupLogging:
    def test_idempotent(self):
        setup_logging()
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1

    def test_handler_is_stderr(self):
        setup_logging()
        handler = logging.getLogger().handlers[0]
        assert handler.stream is sys.stderr

    def test_plain_uses_plain_formatter(self):
        setup_logging(fmt="plain")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, PlainFormatter)

    def test_json_uses_json_formatter(self):
        setup_logging(fmt="json")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)

    def test_level_propagates(self):
        setup_logging(level=logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG

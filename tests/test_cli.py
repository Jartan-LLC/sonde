"""Tests for the CLI parser and the run() orchestration end-to-end (mocked fetch)."""

import json
import logging

import pytest

from sonde import core, cli
from sonde.cli import build_parser
from tests.helpers import make_bucket, RLH_420


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_parser_lists_endpoint_subcommands():
    p = build_parser()
    args = p.parse_args(["asset-owners", "--asset-id", "1"])
    assert args.endpoint == "asset-owners"
    assert args.asset_id == 1
    assert args.sweep_drain == 500  # raised default
    assert args.max_requests == 1200


def test_parser_requires_endpoint():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])  # subcommand is required


def test_parser_requires_asset_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["asset-owners"])  # --asset-id required


def test_parser_help_renders(capsys):
    """Catch argparse group misconfiguration — --help must not crash."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["asset-owners", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--verbose" in out
    assert "--quiet" in out
    assert "--log-format" in out
    assert "--output" in out


# --------------------------------------------------------------------------- #
# run() — header path
# --------------------------------------------------------------------------- #
def _args(tmp_path, *extra):
    out = tmp_path / "report.json"
    base = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-copies",
        "1470000",
        "--seq-cap",
        "15",
        "--burst-sizes",
        "10,20",
        "--burst-cooldown",
        "0",
        "--output",
        str(out),
    ]
    return build_parser().parse_args(base + list(extra)), out


def test_run_uses_headers_and_skips_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    args, out = _args(tmp_path)
    report = cli.run(args)
    est = report["estimate"]
    assert est["header_limit"] == 420 and est["header_window_s"] == 60
    assert est["estimated_minutes"] == pytest.approx(43.7, abs=0.5)
    assert report["sweep"] == []  # auto-skipped (headers authoritative)
    # and it actually wrote the file
    assert json.loads(out.read_text())["endpoint"] == "asset-owners"


def test_run_headerless_runs_sweep(clock, tmp_path, monkeypatch):
    # no rate-limit headers -> sweep runs and finds a floor
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers={"server": "x"}))
    args, out = _args(
        tmp_path,
        "--skip-burst",
        "--sweep-intervals",
        "0.2,0.1,0.05,0.03",
        "--sweep-count",
        "12",
        "--sweep-drain",
        "500",
    )
    report = cli.run(args)
    assert report["swept_floor_interval_s"] == 0.05
    assert report["estimate"]["header_limit"] is None
    assert "measured floor" in report["estimate"]["safe_rate_basis"]


def test_run_httpx_flag_falls_back_when_missing(tmp_path, monkeypatch):
    # httpx isn't installed in the test env, so --use-httpx must fall back to threaded.
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    args, out = _args(tmp_path, "--use-httpx")
    report = cli.run(args)
    assert report["burst_impl"] == "threaded (httpx fallback)"


def test_run_aborts_on_non_200(tmp_path, monkeypatch):
    def always_403(session, ep, cursor, budget):
        budget.take()
        return core.Result(status=403, elapsed=0.01, error="forbidden")

    monkeypatch.setattr(core, "fetch", always_403)
    args, out = _args(tmp_path)
    report = cli.run(args)
    assert report["sanity"]["status"] == 403
    assert "estimate" not in report  # bailed before estimating


# --------------------------------------------------------------------------- #
# Output mode flags
# --------------------------------------------------------------------------- #
def test_verbose_quiet_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["asset-owners", "--asset-id", "1", "-v", "-q"])


def test_log_format_choices():
    args = build_parser().parse_args(["asset-owners", "--asset-id", "1", "--log-format", "json"])
    assert args.log_format == "json"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["asset-owners", "--asset-id", "1", "--log-format", "yaml"])


def test_output_default():
    args = build_parser().parse_args(["asset-owners", "--asset-id", "1"])
    assert args.output == "sonde_report.json"


@pytest.fixture()
def _restore_root_logger():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    yield
    root.handlers = old_handlers
    root.level = old_level


def test_output_dash_writes_to_stdout(tmp_path, monkeypatch, capfd, _restore_root_logger):
    """--output - writes valid JSON to stdout, no file created."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-copies",
        "1470000",
        "--seq-cap",
        "15",
        "--burst-sizes",
        "10,20",
        "--burst-cooldown",
        "0",
        "--output",
        "-",
        "-q",
    ]
    cli.main(argv)
    captured = capfd.readouterr()
    report = json.loads(captured.out)
    assert report["endpoint"] == "asset-owners"
    assert "estimate" in report
    assert not out.exists()


def test_output_dash_abort_path(tmp_path, monkeypatch, capfd, _restore_root_logger):
    """--output - still produces JSON on the abort path (non-200 sanity)."""

    def always_403(session, ep, cursor, budget):
        budget.take()
        return core.Result(status=403, elapsed=0.01, error="forbidden")

    monkeypatch.setattr(core, "fetch", always_403)
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--output",
        "-",
        "-q",
    ]
    cli.main(argv)
    captured = capfd.readouterr()
    report = json.loads(captured.out)
    assert report["sanity"]["status"] == 403
    assert not out.exists()


def _assert_all_stderr_json(captured):
    """Every stderr line must be valid JSON. A broken %-style format string
    causes logging.Handler.handleError to print a traceback to stderr, which
    would fail json.loads here — catching silent conversion bugs."""
    lines = [line for line in captured.err.strip().split("\n") if line.strip()]
    assert len(lines) > 0
    for line in lines:
        parsed = json.loads(line)
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "message" in parsed


def test_log_format_json_on_stderr(tmp_path, monkeypatch, capfd, _restore_root_logger):
    """--log-format json produces structured JSON log lines on stderr (header path)."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-copies",
        "1470000",
        "--seq-cap",
        "15",
        "--burst-sizes",
        "10,20",
        "--burst-cooldown",
        "0",
        "--output",
        str(out),
        "--log-format",
        "json",
    ]
    cli.main(argv)
    _assert_all_stderr_json(capfd.readouterr())


def test_log_format_json_sweep_path(clock, tmp_path, monkeypatch, capfd, _restore_root_logger):
    """Exercises sweep/drain/interval format strings through --log-format json."""
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers={"server": "x"}))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-copies",
        "1470000",
        "--seq-cap",
        "15",
        "--skip-burst",
        "--sweep-intervals",
        "0.2,0.1,0.05,0.03",
        "--sweep-count",
        "12",
        "--sweep-drain",
        "500",
        "--output",
        str(out),
        "--log-format",
        "json",
    ]
    cli.main(argv)
    _assert_all_stderr_json(capfd.readouterr())


def test_log_format_json_abort_path(tmp_path, monkeypatch, capfd, _restore_root_logger):
    """Exercises non-200 sanity + auth-warning format strings through --log-format json."""

    def always_403(session, ep, cursor, budget):
        budget.take()
        return core.Result(status=403, elapsed=0.01, error="forbidden")

    monkeypatch.setattr(core, "fetch", always_403)
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--output",
        str(out),
        "--log-format",
        "json",
    ]
    cli.main(argv)
    _assert_all_stderr_json(capfd.readouterr())

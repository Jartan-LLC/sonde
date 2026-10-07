"""Tests for the CLI parser and the run() orchestration end-to-end (mocked fetch)."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sonde import cli, core, endpoint, phases
from sonde.cli import build_parser
from sonde.endpoint import Endpoint
from sonde.endpoints.asset_owners import AssetOwnersEndpoint
from sonde.provider import RobloxProvider
from tests.helpers import RLH_420, FakeClock, Handler, make_bucket, make_burst_handler


def test_parser_lists_endpoint_subcommands():
    p = build_parser()
    args = p.parse_args(["asset-owners", "--asset-id", "1"])
    assert args.endpoint == "asset-owners"
    assert args.asset_id == 1
    assert args.sweep_drain == 500  # raised default
    assert args.max_requests == 1200


def test_cli_registers_endpoints_in_a_fresh_interpreter():
    # In-process tests can't catch a lost registration: other test modules import the
    # endpoints first.
    result = subprocess.run(
        [sys.executable, "-m", "sonde", "asset-owners", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--asset-id" in result.stdout


def test_endpoint_lookup_loads_the_built_ins_in_a_fresh_interpreter():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sonde import endpoint; print(endpoint.get('asset-owners').__name__)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "AssetOwnersEndpoint"


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--burst-sizes", "10,-5"), ("--burst-sizes", "0"), ("--sweep-intervals", "1,-0.5")],
)
def test_parser_rejects_non_positive_list_values(flag: str, value: str):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["asset-owners", "--asset-id", "1", flag, value])
    assert exc.value.code == 2


def test_parser_requires_endpoint():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])  # subcommand is required


def test_parser_requires_asset_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["asset-owners"])  # --asset-id required


def test_parser_help_renders(capsys: pytest.CaptureFixture[str]):
    """Catch argparse group misconfiguration — --help must not crash."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["asset-owners", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--verbose" in out
    assert "--quiet" in out
    assert "--log-format" in out
    assert "--output" in out


def _args(tmp_path: Path, *extra: str) -> tuple[argparse.Namespace, Path]:
    out = tmp_path / "report.json"
    base = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-items",
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


def test_run_uses_headers_and_skips_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    args, out = _args(tmp_path)
    report = cli.run(args)
    est = report["estimate"]
    assert est["header_limit"] == 420
    assert est["header_window_s"] == 60
    assert est["estimated_minutes"] == pytest.approx(43.7, abs=0.5)
    assert report["sweep"] == []  # auto-skipped (headers authoritative)
    # and it actually wrote the file
    assert json.loads(out.read_text())["endpoint"] == "asset-owners"


def test_run_headerless_runs_sweep(
    clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # no rate-limit headers -> sweep runs and finds a floor
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers={"server": "x"}))
    args, _ = _args(
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
    assert report["ratelimit_headers"] == {}
    assert report["swept_floor_interval_s"] == 0.05
    assert report["estimate"]["header_limit"] is None
    assert "measured floor" in report["estimate"]["safe_rate_basis"]


def test_run_report_shape(clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Report consumers read these keys; every section is filled here.
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    args, out = _args(tmp_path, "--force-sweep", "--sweep-intervals", "0.2", "--sweep-count", "5")
    cli.run(args)
    report = json.loads(out.read_text())
    assert list(report) == [
        "endpoint",
        "provider",
        "sanity",
        "ratelimit_headers",
        "sequential",
        "burst",
        "measured_window_seconds",
        "sweep",
        "swept_floor_interval_s",
        "estimate",
        "requests_used",
    ]
    assert list(report["sanity"]) == ["status", "rclass", "items", "headers"]
    assert list(report["ratelimit_headers"]) == [
        "limit",
        "window_s",
        "remaining",
        "reset_s",
        "policies",
        "raw",
    ]
    assert report["ratelimit_headers"]["policies"] == [
        [420, None],
        [420, 60],
        [420, 60],
        [70000, None],
    ]
    assert list(report["sequential"]) == [
        "successful_before_429",
        "first_429_at_request",
        "wall_seconds",
        "seq_req_per_sec",
        "avg_latency_ms",
        "retry_after",
    ]
    assert [list(row) for row in report["burst"]] == 2 * [
        [
            "burst_size",
            "ok_200",
            "throttled_429",
            "other",
            "wall_seconds",
            "launch_spread_ms",
            "max_retry_after",
        ]
    ]
    assert [list(row) for row in report["sweep"]] == [
        [
            "interval_s",
            "drain_requests",
            "bucket_emptied",
            "requests",
            "throttled_429",
            "throttle_frac",
            "clean",
            "effective_req_per_s",
        ]
    ]


def test_run_skip_flags_skip_their_phases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a skipped phase ran")

    # No rate-limit headers, so without --skip-sweep the sweep would run.
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers={"server": "x"}))
    monkeypatch.setattr(phases, "phase_burst", must_not_run)
    monkeypatch.setattr(phases, "phase_sweep", must_not_run)
    args, _ = _args(tmp_path, "--skip-burst", "--skip-sweep")
    report = cli.run(args)
    assert report["burst"] == []
    assert report["sweep"] == []
    assert report["swept_floor_interval_s"] is None


def test_run_aborts_on_non_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def always_403(session: Any, ep: Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
        budget.take()
        return core.Result(status=403, elapsed=0.01, error="forbidden")

    monkeypatch.setattr(core, "fetch", always_403)
    args, _ = _args(tmp_path)
    report = cli.run(args)
    assert report["sanity"]["status"] == 403
    assert "estimate" not in report  # bailed before estimating


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


def test_pagination_defaults():
    args = build_parser().parse_args(["asset-owners", "--asset-id", "1"])
    assert args.page_size == 100
    assert args.total_items is None


def test_pagination_flags_consistent_across_endpoints():
    """Any registered endpoint that exposes pagination spells it as the paired
    --page-size / --total-items, checked across every endpoint (not just two)."""
    for name, cls in endpoint.all_endpoints().items():
        p = argparse.ArgumentParser()
        cls.add_arguments(p)
        dests = {a.dest for a in p._actions}
        if dests & {"page_size", "total_items"}:
            assert {"page_size", "total_items"} <= dests, (
                f"{name}: pagination flags must be the paired --page-size/--total-items"
            )


def test_burst_sizes_parses_to_list():
    args = build_parser().parse_args(
        ["asset-owners", "--asset-id", "1", "--burst-sizes", "5,10,15"]
    )
    assert args.burst_sizes == [5, 10, 15]


def test_bad_burst_sizes_exits_2():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["asset-owners", "--asset-id", "1", "--burst-sizes", "10,abc"])
    assert exc.value.code == 2  # argparse usage error


def test_bad_sweep_intervals_exits_2():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["asset-owners", "--asset-id", "1", "--sweep-intervals", "1,x"])
    assert exc.value.code == 2


def test_configured_secret_absent_from_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """End-to-end: a credential the target echoes back is scrubbed from stderr
    through cli.main() — exercises the provider's credentials() -> register -> scrub."""
    monkeypatch.setenv("ROBLOX_COOKIE", "SUPERSECRETCOOKIEVALUE")

    def echo_secret(session: Any, ep: Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
        budget.take()  # target echoes the bare cookie back in an error body
        return core.Result(status=403, elapsed=0.01, error="denied: SUPERSECRETCOOKIEVALUE")

    monkeypatch.setattr(core, "fetch", echo_secret)
    argv = ["asset-owners", "--asset-id", "1", "--output", "-", "--log-format", "json"]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    captured = capfd.readouterr()
    assert "SUPERSECRETCOOKIEVALUE" not in captured.err
    assert "SUPERSECRETCOOKIEVALUE" not in captured.out
    assert "***" in captured.err  # redaction actually fired, line not merely absent


def _stderr_when_target_echoes(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], argv: list[str], echoed: str
) -> str:
    def echo(session: Any, ep: Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
        budget.take()
        return core.Result(status=403, elapsed=0.01, error=f"denied: {echoed}")

    monkeypatch.setattr(core, "fetch", echo)
    with pytest.raises(SystemExit):
        cli.main([*argv, "--output", "-", "--log-format", "json"])
    return capfd.readouterr().err


ASSET_OWNERS = ["asset-owners", "--asset-id", "1"]


def test_query_param_credentials_are_scrubbed(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], restore_root_logger: None
):
    """A provider's query-parameter credential is redacted, raw and percent-encoded."""

    def auth_params(self: RobloxProvider) -> dict[str, str]:
        return {"key": "SECRET/PARAM VALUE"}

    monkeypatch.setattr(RobloxProvider, "auth_params", auth_params)
    for echoed in ("SECRET/PARAM VALUE", "SECRET%2FPARAM%20VALUE", "SECRET%2FPARAM+VALUE"):
        err = _stderr_when_target_echoes(monkeypatch, capfd, ASSET_OWNERS, echoed)
        assert echoed not in err
        assert "***" in err


def test_credential_named_endpoint_header_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], restore_root_logger: None
):
    def extra_headers(self: Endpoint) -> dict[str, str]:
        return {"X-Api-Key": "WHOLEHEADERSECRET"}

    monkeypatch.setattr(AssetOwnersEndpoint, "extra_headers", extra_headers)
    err = _stderr_when_target_echoes(monkeypatch, capfd, ASSET_OWNERS, "WHOLEHEADERSECRET")
    assert "WHOLEHEADERSECRET" not in err
    assert "***" in err


def test_github_token_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], restore_root_logger: None
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRETTOKENVALUE")
    argv = ["github-stargazers", "--owner", "a", "--repo", "b"]
    err = _stderr_when_target_echoes(monkeypatch, capfd, argv, "ghp_SECRETTOKENVALUE")
    assert "ghp_SECRETTOKENVALUE" not in err
    assert "***" in err


def test_unwritable_output_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bad --output path aborts with exit 2 before probing (fetch never called)."""
    called: list[int] = []

    def spy(*a: Any, **k: Any) -> core.Result:
        called.append(1)
        return core.Result(status=200, elapsed=0.0)

    monkeypatch.setattr(core, "fetch", spy)
    bad = tmp_path / "no_such_dir" / "report.json"
    args = build_parser().parse_args(["asset-owners", "--asset-id", "1", "--output", str(bad)])
    with pytest.raises(SystemExit) as exc:
        cli.run(args)
    assert exc.value.code == 2
    assert not called, "preflight must fail before any probe request"


def test_output_dash_writes_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """--output - writes valid JSON to stdout, no file created. -q suppresses INFO."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    monkeypatch.chdir(tmp_path)
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-items",
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
        "--log-format",
        "json",
    ]
    cli.main(argv)
    captured = capfd.readouterr()
    report = json.loads(captured.out)
    assert report["endpoint"] == "asset-owners"
    assert "estimate" in report
    assert not (tmp_path / "sonde_report.json").exists(), "default file should not be created"
    err_lines = [line for line in captured.err.strip().split("\n") if line.strip()]
    for line in err_lines:
        level = json.loads(line)["level"]
        assert level not in ("INFO", "DEBUG"), f"-q should suppress {level}"


def test_output_dash_abort_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """--output - still produces JSON on the abort path (non-200 sanity)."""

    def always_403(session: Any, ep: Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
        budget.take()
        return core.Result(status=403, elapsed=0.01, error="forbidden")

    monkeypatch.setattr(core, "fetch", always_403)
    monkeypatch.chdir(tmp_path)
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--output",
        "-",
        "-q",
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2  # non-OK sanity aborts with a non-zero exit
    captured = capfd.readouterr()
    report = json.loads(captured.out)
    assert report["sanity"]["status"] == 403
    assert not (tmp_path / "sonde_report.json").exists()


def _assert_all_stderr_json(err: str) -> None:
    """Every stderr line must be valid JSON. A broken %-style format string
    causes logging.Handler.handleError to print a traceback to stderr, which
    would fail json.loads here — catching silent conversion bugs.

    Relies on logging.raiseExceptions being True (the pytest/CPython default);
    if it were flipped to False, handleError would swallow the error silently
    and this canary would stop catching broken format strings."""
    lines = [line for line in err.strip().split("\n") if line.strip()]
    assert len(lines) > 0
    for line in lines:
        parsed = json.loads(line)
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "message" in parsed


def test_log_format_json_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """--log-format json produces structured JSON log lines on stderr (header path)."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-items",
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
    _assert_all_stderr_json(capfd.readouterr().err)


def test_log_format_json_sweep_path(
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """Exercises sweep/drain/interval format strings through --log-format json."""
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers={"server": "x"}))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--total-items",
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
    _assert_all_stderr_json(capfd.readouterr().err)


@pytest.mark.usefixtures("clock", "restore_root_logger")
def test_log_format_json_verbose_throttle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    burst_transport: Callable[[Handler], None],
):
    """Exercises DEBUG format strings (headers, throttle headers) plus the burst
    server-window branch via -v. The burst 429s with a Retry-After so the window is
    read from the header and the async recovery poll (which sleeps on asyncio, not the
    virtual clock) is skipped."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 5, 5, headers=RLH_420))
    burst_transport(make_burst_handler(decider=lambda: False, retry_after=3))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--seq-cap",
        "10",
        "--burst-sizes",
        "10",
        "--burst-cooldown",
        "0",
        "--skip-sweep",
        "--output",
        str(out),
        "--log-format",
        "json",
        "-v",
    ]
    cli.main(argv)
    captured = capfd.readouterr()
    _assert_all_stderr_json(captured.err)
    lines = [line for line in captured.err.strip().split("\n") if line.strip()]
    debug_lines = [line for line in lines if json.loads(line)["level"] == "DEBUG"]
    assert len(debug_lines) > 0, "no DEBUG lines emitted — -v flag not working"


def test_log_format_json_abort_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """Exercises non-200 sanity + auth-warning format strings through --log-format json."""

    def always_403(session: Any, ep: Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
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
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    _assert_all_stderr_json(capfd.readouterr().err)


def test_main_crash_logs_json_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], restore_root_logger: None
):
    """A crash in run() is routed through the logger: stderr stays valid JSON
    with an `exc` key and the process exits 1 — the behaviour the top-level
    `except Exception` handler advertises."""

    def boom(args: argparse.Namespace) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run", boom)
    argv = ["asset-owners", "--asset-id", "1", "--log-format", "json"]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 1
    captured = capfd.readouterr()
    _assert_all_stderr_json(captured.err)
    err_lines = [json.loads(line) for line in captured.err.strip().split("\n") if line.strip()]
    assert any("kaboom" in line.get("exc", "") for line in err_lines)


def test_main_keyboard_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], restore_root_logger: None
):
    """KeyboardInterrupt is caught before `except Exception`, logged as a
    warning, and exits 130 (128 + SIGINT)."""

    def interrupt(args: argparse.Namespace) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupt)
    argv = ["asset-owners", "--asset-id", "1", "--log-format", "json"]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 130
    _assert_all_stderr_json(capfd.readouterr().err)


# Headers with limit but no window — triggers "present but no window" branch
RLH_NO_WINDOW = {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "99"}


def test_log_format_json_limit_no_window(
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """Exercises 'rate-limit headers present but no window' format strings."""
    monkeypatch.setattr(core, "fetch", make_bucket(0.05, 30, headers=RLH_NO_WINDOW))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--seq-cap",
        "10",
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
    _assert_all_stderr_json(capfd.readouterr().err)


def test_log_format_json_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """Exercises budget-exhaustion warning format strings in seq phase."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--max-requests",
        "5",
        "--seq-cap",
        "10",
        "--skip-burst",
        "--skip-sweep",
        "--output",
        str(out),
        "--log-format",
        "json",
    ]
    cli.main(argv)
    _assert_all_stderr_json(capfd.readouterr().err)


def test_log_format_json_drain_failure(
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
):
    """Exercises drain-failure warning format strings in sweep phase."""
    monkeypatch.setattr(core, "fetch", make_bucket(0.001, 10000, headers={"server": "x"}))
    out = tmp_path / "report.json"
    argv = [
        "asset-owners",
        "--asset-id",
        "20573078",
        "--seq-cap",
        "5",
        "--skip-burst",
        "--sweep-intervals",
        "0.1",
        "--sweep-count",
        "5",
        "--sweep-drain",
        "50",
        "--output",
        str(out),
        "--log-format",
        "json",
    ]
    cli.main(argv)
    _assert_all_stderr_json(capfd.readouterr().err)


@pytest.mark.parametrize(
    ("token", "auth_line"), [(None, "none (anonymous)"), ("ghp_x1234567", "credentials set")]
)
def test_auth_line_reflects_declared_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    restore_root_logger: None,
    token: str | None,
    auth_line: str,
):
    # GitHub's auth headers always carry Accept and an API version, token or not.
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    argv = ["github-stargazers", "--owner", "a", "--repo", "b"]
    err = _stderr_when_target_echoes(monkeypatch, capfd, argv, "denied")
    assert f"Auth     : {auth_line}" in err

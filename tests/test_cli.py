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
# --json flag
# --------------------------------------------------------------------------- #
def test_run_json_output_to_stdout(tmp_path, monkeypatch, capsys):
    """--json suppresses file output and writes valid JSON to stdout."""
    monkeypatch.setattr(core, "fetch", make_bucket(60.0 / 420, 420, headers=RLH_420))
    # Reset logging so basicConfig in main() can reconfigure it.
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
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
        "--json",
    ]
    cli.main(argv)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["endpoint"] == "asset-owners"
    assert "estimate" in report
    # --json should suppress file output
    assert not out.exists()

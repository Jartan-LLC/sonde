"""Argument parsing, endpoint selection, and run orchestration.

Usage:
    python -m sonde <endpoint> [common options] [endpoint options]

The common rate-limit options are shared across every endpoint; each registered
endpoint contributes its own options as a subcommand.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from sonde import core, endpoint, phases
from sonde.logconfig import register_log_secrets, setup_logging
from sonde.provider import RateLimit, authoritative_limit

logger = logging.getLogger(__name__)

# Header names whose whole value is a credential, whether the provider or the endpoint
# set the header.
_SECRET_HEADER_KEYS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})


def _list_of[T: float](convert: Callable[[str], T], kind: str) -> Callable[[str], list[T]]:
    """Build an argparse type for a comma-separated list of positive numbers.

    Bad input is a clean exit 2.
    """

    def parse(raw: str) -> list[T]:
        try:
            vals = [convert(x) for x in raw.split(",") if x.strip()]
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"comma-separated {kind} required: {e}") from e
        if not vals:
            raise argparse.ArgumentTypeError("at least one value required")
        if any(v <= 0 for v in vals):
            raise argparse.ArgumentTypeError(f"positive {kind} required")
        return vals

    return parse


_int_list = _list_of(int, "integers")
_float_list = _list_of(float, "numbers")


def build_common_parser() -> argparse.ArgumentParser:
    """All endpoint-agnostic probe options (shared by every subcommand)."""
    c = argparse.ArgumentParser(add_help=False)
    g = c.add_argument_group("rate-limit probe options")
    g.add_argument(
        "--max-requests", type=int, default=1200, help="hard global cap across all phases (safety)"
    )
    g.add_argument(
        "--seq-cap", type=int, default=150, help="max sequential requests before giving up on a 429"
    )
    g.add_argument("--skip-burst", action="store_true")
    g.add_argument(
        "--burst-sizes",
        type=_int_list,
        default=[10, 20, 40, 80],
        help="comma list of concurrent burst sizes (default: 10,20,40,80)",
    )
    g.add_argument(
        "--burst-cooldown",
        type=float,
        default=60.0,
        help="fallback seconds between bursts if the window can't be measured",
    )
    g.add_argument(
        "--recovery-step",
        type=float,
        default=0.25,
        help="first poll delay when measuring the throttle window (grows geometrically)",
    )
    g.add_argument(
        "--recovery-max",
        type=float,
        default=90.0,
        help="give up measuring the window after this many seconds",
    )
    g.add_argument(
        "--recovery-polls",
        type=int,
        default=15,
        help="max polls during recovery measurement (bounds request count)",
    )
    g.add_argument("--skip-sweep", action="store_true", help="skip the sustained-interval sweep")
    g.add_argument(
        "--force-sweep",
        action="store_true",
        help="run the sweep even when authoritative headers are present "
        "(skipped by default in that case; it's redundant and slow)",
    )
    g.add_argument(
        "--sweep-intervals",
        type=_float_list,
        default=[8, 5, 3, 2, 1.2, 0.6, 0.3, 0.15],
        help="inter-request intervals (s) to test, SLOW->FAST (default: "
        "8,5,3,2,1.2,0.6,0.3,0.15). Wide so it can bracket slow limits; only "
        "used as a fallback when headers are missing.",
    )
    g.add_argument(
        "--sweep-count", type=int, default=20, help="paced requests per interval after draining"
    )
    g.add_argument(
        "--sweep-drain",
        type=int,
        default=500,
        help="cap on rapid requests used to empty the bucket before each interval; "
        "the drain runs until empty or this cap",
    )
    g.add_argument(
        "--sweep-tolerance",
        type=float,
        default=0.1,
        help="max fraction of 429s from empty for an interval to count as sustainable",
    )
    g.add_argument(
        "--margin",
        type=float,
        default=0.8,
        help="safety margin: recommended interval = floor / margin (0.8 => 25%% slower)",
    )
    g.add_argument(
        "--output",
        default="sonde_report.json",
        help="report output file (use '-' for stdout)",
    )

    vq = c.add_mutually_exclusive_group()
    vq.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show per-request detail (sets log level to DEBUG)",
    )
    vq.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only show warnings and errors (sets log level to WARNING)",
    )
    c.add_argument(
        "--log-format",
        choices=["plain", "json"],
        default="plain",
        help="log line format: plain (message-only, default) or json (structured)",
    )
    return c


def build_parser() -> argparse.ArgumentParser:
    """Build the parser: one subcommand per registered endpoint, each with the common options."""
    common = build_common_parser()
    p = argparse.ArgumentParser(
        prog="sonde",
        description="Probe any HTTP API for its rate limits. Pick an endpoint subcommand.",
    )
    sub = p.add_subparsers(dest="endpoint", required=True, metavar="ENDPOINT")
    for name, cls in sorted(endpoint.all_endpoints().items()):
        sp = sub.add_parser(name, parents=[common], help=cls.help, description=cls.help)
        cls.add_arguments(sp)
    return p


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the phases against the chosen endpoint and write the report.

    Args:
        args: The parsed command line.

    Returns:
        The report, as written to `--output`.
    """
    ep_cls = endpoint.get(args.endpoint)
    if ep_cls is None:
        raise SystemExit(f"unknown endpoint: {args.endpoint}")
    _preflight_output(args.output)
    ep = ep_cls.from_args(args)
    provider = ep.provider()
    budget = core.Budget(max_requests=args.max_requests)
    # base headers < provider auth < endpoint extras
    headers = {**core.BASE_HEADERS, **provider.auth_headers(), **ep.extra_headers()}
    # Keep our own credentials out of logs and the report: a target can echo them back,
    # and a connection error can quote the URL, with the query parameters percent-encoded.
    params = list(provider.auth_params().values())
    register_log_secrets(
        [
            *provider.credentials(),
            *params,
            *(quote(v, safe="") for v in params),
            *(quote_plus(v) for v in params),
            *(v for k, v in headers.items() if k.lower() in _SECRET_HEADER_KEYS),
        ]
    )
    probe = phases.Probe(
        endpoint=ep, budget=budget, session=core.build_session(headers=headers), headers=headers
    )

    logger.info("Endpoint : %s", ep.name)
    logger.info("Provider : %s", provider.name)
    logger.info(
        "Auth     : %s",
        "credentials set"
        if provider.credentials() or provider.auth_params()
        else "none (anonymous)",
    )
    logger.info("Budget   : %s requests total", args.max_requests)

    report: dict[str, Any] = {"endpoint": ep.name, "provider": provider.name}

    sanity, rl = phases.phase_sanity(probe)
    report["sanity"] = {
        "status": sanity.status,
        "rclass": sanity.rclass,
        "items": sanity.count,
        "headers": sanity.headers,
    }
    report["ratelimit_headers"] = asdict(rl) if rl is not None else {}
    if sanity.rclass != core.RClass.OK:
        logger.warning(
            "\nAborting: no usable success response from the endpoint. "
            "Fix auth / arguments and re-run."
        )
        _dump(args.output, report)
        return report

    seq_summary, cursor_pool = phases.phase_seq(probe, args.seq_cap)
    report["sequential"] = asdict(seq_summary)

    burst_rows, measured_window = _burst(args, probe, cursor_pool)
    report["burst"] = [asdict(row) for row in burst_rows]
    report["measured_window_seconds"] = measured_window

    swept_interval, sweep_rows = _sweep(args, probe, cursor_pool, rl)
    report["sweep"] = [asdict(row) for row in sweep_rows]
    report["swept_floor_interval_s"] = swept_interval

    measured = phases.Measurements(
        page_count=sanity.count,
        rate_limit=rl,
        seq_summary=seq_summary,
        burst_results=burst_rows,
        measured_window=measured_window,
        swept_interval=swept_interval,
    )
    report["estimate"] = phases.phase_estimate(ep, measured, args.margin)
    report["requests_used"] = budget.used

    _dump(args.output, report)
    logger.info("\nRequests used: %s/%s", budget.used, args.max_requests)
    if args.output != "-":
        logger.info("Full report written to: %s", args.output)
    return report


def _burst(
    args: argparse.Namespace, probe: phases.Probe, cursor_pool: list[Any]
) -> tuple[list[phases.BurstRow], float | None]:
    """Run the burst phase unless `--skip-burst`; return its rows and measured window."""
    if args.skip_burst:
        return [], None
    config = phases.BurstConfig(
        sizes=tuple(args.burst_sizes),
        cooldown=args.burst_cooldown,
        recovery_step=args.recovery_step,
        recovery_max=args.recovery_max,
        recovery_polls=args.recovery_polls,
    )
    return phases.phase_burst(probe, cursor_pool, config)


def _sweep(
    args: argparse.Namespace,
    probe: phases.Probe,
    cursor_pool: list[Any],
    rate_limit: RateLimit | None,
) -> tuple[float | None, list[phases.SweepRow]]:
    """Run the sweep when no authoritative headers make it redundant, or when forced.

    Returns:
        The fastest clean interval (None if none was found or the sweep didn't run),
        and its rows.
    """
    if args.skip_sweep:
        return None, []
    if authoritative_limit(rate_limit) is not None and not args.force_sweep:
        logger.info("\n== PHASE: sustained-interval sweep ==")
        logger.info(
            "  skipped: authoritative rate-limit headers already give the limit. "
            "Use --force-sweep to run it anyway as an independent check."
        )
        return None, []
    config = phases.SweepConfig(
        intervals=tuple(sorted(args.sweep_intervals, reverse=True)),
        probe_count=args.sweep_count,
        drain_cap=args.sweep_drain,
        tolerance=args.sweep_tolerance,
    )
    return phases.phase_sweep(probe, cursor_pool, config)


def _preflight_output(path: str) -> None:
    """Fail fast (exit 2) on an unwritable --output path before probing."""
    if path == "-":
        return
    try:
        # Append mode: tests writability without truncating an existing report.
        # On a new path this creates a zero-byte file; if the probe is interrupted
        # before _dump, that empty file remains (acceptable for fail-fast).
        with Path(path).open("a"):
            pass
    except OSError as e:
        logger.error("cannot write --output %r: %s", path, e)
        raise SystemExit(2) from e


def _dump(path: str, report: dict[str, Any]) -> None:
    if path == "-":
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        with Path(path).open("w") as f:
            json.dump(report, f, indent=2)


def _aborted(report: dict[str, Any]) -> bool:
    """Return True when the probe stopped because the endpoint gave no usable response."""
    sanity = report.get("sanity")
    return bool(sanity) and sanity.get("rclass") != core.RClass.OK


def main(argv: list[str] | None = None) -> None:
    """Run sonde from the command line.

    Exit codes: 0 success; 2 a failed precondition (bad arguments, an unwritable
    output, or no usable response from the endpoint); 1 an unexpected crash; 130
    interrupted.

    Args:
        argv: The arguments; `sys.argv[1:]` when omitted.
    """
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    setup_logging(level=level, fmt=args.log_format)
    try:
        report = run(args)
    except KeyboardInterrupt:
        logger.warning("interrupted.")
        sys.exit(130)
    except Exception:
        # Route crashes through the logger so --log-format json keeps stderr valid
        # JSON and the traceback is escaped (PlainFormatter) rather than dumped raw.
        logger.exception("unexpected error")
        sys.exit(1)
    if _aborted(report):
        sys.exit(2)


if __name__ == "__main__":
    main()

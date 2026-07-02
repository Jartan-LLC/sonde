"""
cli.py — argument parsing, endpoint selection, and run orchestration.

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
from collections.abc import Iterable
from typing import Any

from . import (
    core,
    endpoint,
    endpoints,  # noqa: F401  (import registers all endpoints)
    phases,
)
from .logconfig import register_log_secrets, setup_logging

logger = logging.getLogger(__name__)

# Header names whose values are credentials and must be kept out of logs.
_SECRET_HEADER_KEYS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})


def _secret_variants(value: str) -> Iterable[str]:
    """The full header value plus the bare credential inside it, so a target that
    echoes just the token (no `Bearer `, no `.ROBLOSECURITY=`) is still redacted."""
    yield value
    # Only emit a bare variant if it's long enough to be a real credential, so a
    # short prefix can't over-redact unrelated log text.
    after_scheme = value.split(" ", 1)  # "Bearer <tok>" -> "<tok>"
    if len(after_scheme) == 2 and len(after_scheme[1]) >= 8:
        yield after_scheme[1]
    after_eq = value.split("=", 1)  # ".ROBLOSECURITY=<cookie>" -> "<cookie>"
    if len(after_eq) == 2 and len(after_eq[1]) >= 8:
        yield after_eq[1]


def _int_list(raw: str) -> list[int]:
    """argparse type for a comma-separated list of ints (clean exit-2 on bad input)."""
    try:
        vals = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"comma-separated integers required: {e}")
    if not vals:
        raise argparse.ArgumentTypeError("at least one value required")
    return vals


def _float_list(raw: str) -> list[float]:
    """argparse type for a comma-separated list of floats (clean exit-2 on bad input)."""
    try:
        vals = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"comma-separated numbers required: {e}")
    if not vals:
        raise argparse.ArgumentTypeError("at least one value required")
    return vals


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
    ep_cls = endpoint.get(args.endpoint)
    if ep_cls is None:
        raise SystemExit(f"unknown endpoint: {args.endpoint}")
    _preflight_output(args.output)
    ep = ep_cls.from_args(args)
    provider = ep.provider()
    burst_sizes = args.burst_sizes
    sweep_intervals = sorted(args.sweep_intervals, reverse=True)
    budget = core.Budget(max_requests=args.max_requests)
    # base headers < provider auth < endpoint extras
    headers = {**core.BASE_HEADERS, **provider.auth_headers(), **ep.extra_headers()}
    # Keep our own credentials out of logs if the target echoes them back.
    register_log_secrets(
        variant
        for k, v in headers.items()
        if k.lower() in _SECRET_HEADER_KEYS
        for variant in _secret_variants(v)
    )
    session = core.build_session(headers=headers)

    logger.info("Endpoint : %s", ep.name)
    logger.info("Provider : %s", provider.name)
    logger.info(
        "Auth     : %s",
        "credentials set" if provider.auth_headers() else "none (anonymous)",
    )
    logger.info("Budget   : %s requests total", args.max_requests)

    report = {"endpoint": ep.name, "provider": provider.name}

    sanity, rl = phases.phase_sanity(session, ep, budget)
    report["sanity"] = {
        "status": sanity.status,
        "rclass": sanity.rclass.value,
        "items": sanity.count,
        "headers": sanity.headers,
    }
    report["ratelimit_headers"] = rl
    if sanity.rclass != core.RClass.OK:
        logger.warning(
            "\nAborting: no usable success response from the endpoint. "
            "Fix auth / arguments and re-run."
        )
        _dump(args.output, report)
        return report
    page_count = sanity.count  # items per successful page, for the estimate

    seq_summary, cursor_pool = phases.phase_seq(session, ep, budget, args.seq_cap)
    report["sequential"] = seq_summary

    burst_results, measured_window = [], None
    if not args.skip_burst:
        burst_results, measured_window = phases.phase_burst(
            headers,
            ep,
            budget,
            burst_sizes,
            args.burst_cooldown,
            cursor_pool,
            args.recovery_step,
            args.recovery_max,
            args.recovery_polls,
        )
    report["burst"] = burst_results
    report["measured_window_seconds"] = measured_window

    swept_interval, sweep_rows = None, []
    headers_authoritative = bool(rl.get("limit") and rl.get("window_s"))
    run_sweep = (not args.skip_sweep) and (args.force_sweep or not headers_authoritative)
    if run_sweep:
        swept_interval, sweep_rows = phases.phase_sweep(
            session,
            ep,
            budget,
            cursor_pool,
            sweep_intervals,
            args.sweep_count,
            args.sweep_drain,
            args.sweep_tolerance,
        )
    elif headers_authoritative and not args.skip_sweep:
        logger.info("\n== PHASE: sustained-interval sweep ==")
        logger.info(
            "  skipped: authoritative rate-limit headers already give the limit. "
            "Use --force-sweep to run it anyway as an independent check."
        )
    report["sweep"] = sweep_rows
    report["swept_floor_interval_s"] = swept_interval

    report["estimate"] = phases.phase_estimate(
        ep, page_count, seq_summary, burst_results, measured_window, swept_interval, args.margin, rl
    )
    report["requests_used"] = budget.used

    _dump(args.output, report)
    logger.info("\nRequests used: %s/%s", budget.used, args.max_requests)
    if args.output != "-":
        logger.info("Full report written to: %s", args.output)
    return report


def _preflight_output(path: str) -> None:
    """Fail fast (exit 2) on an unwritable --output path before probing."""
    if path == "-":
        return
    try:
        # Append mode: tests writability without truncating an existing report.
        # On a new path this creates a zero-byte file; if the probe is interrupted
        # before _dump, that empty file remains (acceptable for fail-fast).
        with open(path, "a"):
            pass
    except OSError as e:
        logger.error("cannot write --output %r: %s", path, e)
        raise SystemExit(2)


def _dump(path: str, report: dict[str, Any]) -> None:
    if path == "-":
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        with open(path, "w") as f:
            json.dump(report, f, indent=2)


def _aborted(report: dict[str, Any]) -> bool:
    """True when the probe bailed because the endpoint returned no usable response
    (non-OK sanity). main() maps this to a non-zero exit so CI can detect it."""
    sanity = report.get("sanity")
    return bool(sanity) and sanity.get("rclass") != core.RClass.OK.value


def main(argv: list[str] | None = None) -> None:
    """Exit codes: 0 success, 2 precondition failure (bad args / unwritable output /
    endpoint returned no usable response), 1 unexpected crash, 130 interrupted."""
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
        logger.error("unexpected error", exc_info=True)
        sys.exit(1)
    if _aborted(report):
        sys.exit(2)


if __name__ == "__main__":
    main()

"""The sanity and sequential phases: one request, then back-to-back requests until throttled."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

# Through the module, not `from sonde.core import fetch`, so tests can patch core.fetch.
from sonde import core
from sonde.core import Result
from sonde.phases.probe import Probe
from sonde.provider import RateLimit, authoritative_limit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SequentialSummary:
    """What the sequential phase measured.

    Attributes:
        successful_before_429: Successful requests before the phase stopped: at the first
            throttle, or otherwise at an error, the budget or the cap.
        first_429_at_request: The first throttled request's number, counting from 1, or
            None when nothing throttled.
        wall_seconds: The phase's wall time.
        seq_req_per_sec: Successful requests per second, or None when no time passed.
        avg_latency_ms: The successful requests' mean latency, or None when none succeeded.
        retry_after: The last response's Retry-After in seconds, when it sent one.
    """

    successful_before_429: int
    first_429_at_request: int | None
    wall_seconds: float
    seq_req_per_sec: float | None
    avg_latency_ms: float | None
    retry_after: float | None


def phase_sanity(probe: Probe) -> tuple[Result, RateLimit | None]:
    """Send one request and log what it shows about auth and rate limits.

    Returns:
        The request's result, and the provider's parse of its rate-limit headers (None
        when it sent none).
    """
    logger.info("\n== PHASE: sanity / auth ==")
    endpoint = probe.endpoint
    r = core.fetch(probe.session, endpoint, None, probe.budget)
    if r.rclass == core.RClass.OK:
        logger.info(
            "  OK  status=%s  items_returned=%s  latency=%.0fms",
            r.status,
            r.count,
            r.elapsed * 1000,
        )
        logger.info("      next_cursor_present=%s", bool(r.next_cursor))
    else:
        # WARNING (not INFO) so `-q` users still see the triggering status/error
        # alongside the abort message this non-OK path leads to.
        logger.warning(
            "  status=%s (%s)  latency=%.0fms  error=%r",
            r.status,
            r.rclass,
            r.elapsed * 1000,
            r.error,
        )
        if r.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            logger.warning(
                "  -> looks like an auth problem. Set the provider's credential env var."
            )
    if r.headers and logger.isEnabledFor(logging.DEBUG):
        logger.debug("  headers: %s", json.dumps(r.headers))

    rl = endpoint.provider().parse_rate_limit(r.headers)
    stated = authoritative_limit(rl)
    if rl is None or not rl.limit:
        logger.info("  >> no usable rate-limit headers (will fall back to empirical sweep).")
    elif stated is not None:
        limit, window = stated
        logger.info("  >> RATE LIMIT (headers, authoritative): %s per %ss window", limit, window)
        if rl.remaining is not None:
            logger.info("     live: remaining=%s  resets_in=%ss", rl.remaining, rl.reset_s)
        extra = [(c, w) for c, w in rl.policies if w != window]
        if extra:
            logger.info("     other quota(s): %s", extra)
    else:
        logger.info(
            "  >> rate-limit headers present but no window: limit=%s, "
            "remaining=%s, resets_in=%ss "
            "(will fall back to the sweep for the rate estimate).",
            rl.limit,
            rl.remaining,
            rl.reset_s,
        )
    return r, rl


def phase_seq(probe: Probe, cap: int) -> tuple[SequentialSummary, list[Any]]:
    """Send back-to-back requests until the first throttle, error, empty budget, or `cap`.

    Returns:
        The phase's summary, and the cursors it collected for the later phases.
    """
    logger.info("\n== PHASE: sequential sustained probe ==")
    logger.info("  up to %s back-to-back requests until the first 429...", cap)
    cursor = None
    cursor_pool: list[Any] = []
    ok = 0
    first_429 = None
    t_start = time.perf_counter()
    latencies: list[float] = []
    last = None

    for i in range(cap):
        r = core.fetch(probe.session, probe.endpoint, cursor, probe.budget)
        last = r
        if r.rclass == core.RClass.OK:
            ok += 1
            latencies.append(r.elapsed)
            if r.next_cursor:
                cursor = r.next_cursor
                cursor_pool.append(r.next_cursor)
            else:
                cursor = None
        elif r.rclass == core.RClass.THROTTLED:
            first_429 = i + 1
            el = time.perf_counter() - t_start
            rate = ok / el if el > 0 else float("inf")
            logger.info(
                "  throttled after %s successful in %.2fs (~%.1f/s). Retry-After=%s",
                ok,
                el,
                rate,
                r.retry_after,
            )
            if r.headers and logger.isEnabledFor(logging.DEBUG):
                logger.debug("  throttle headers: %s", json.dumps(r.headers))
            break
        elif r.rclass == core.RClass.BUDGET:
            logger.warning("  budget exhausted before being throttled.")
            break
        else:
            logger.warning("  unexpected status=%s error=%r; stopping.", r.status, r.error)
            break

    el = time.perf_counter() - t_start
    avg = (sum(latencies) / len(latencies)) if latencies else None
    if first_429 is None and ok:
        rate = ok / el if el > 0 else float("inf")
        logger.info(
            "  no 429 in %s requests over %.2fs (~%.1f/s); "
            "ceiling is likely a burst/window cap -> see burst.",
            ok,
            el,
            rate,
        )
    summary = SequentialSummary(
        successful_before_429=ok,
        first_429_at_request=first_429,
        wall_seconds=round(el, 3),
        seq_req_per_sec=round(ok / el, 2) if el > 0 else None,
        avg_latency_ms=round(avg * 1000, 1) if avg is not None else None,
        retry_after=last.retry_after if last else None,
    )
    return summary, cursor_pool

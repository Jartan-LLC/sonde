"""The sustained-interval sweep: the fastest interval that stays unthrottled from empty."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, NamedTuple

# Through the module, not `from sonde.core import fetch`, so tests can patch core.fetch.
from sonde import core
from sonde.phases.probe import Probe, cursor_cycle

logger = logging.getLogger(__name__)

# Consecutive throttles that show the drain has emptied the bucket; fewer could be
# transient.
_DRAINED_AFTER = 3


@dataclass(frozen=True)
class SweepConfig:
    """The sustained-interval sweep's settings.

    Attributes:
        intervals: Inter-request intervals in seconds, slowest first.
        probe_count: Paced requests per interval, sent after the drain.
        drain_cap: Most rapid requests one drain may send.
        tolerance: Highest fraction of throttled requests that still counts as clean.
    """

    intervals: tuple[float, ...]
    probe_count: int
    drain_cap: int
    tolerance: float


def phase_sweep(
    probe: Probe, cursor_pool: list[Any], config: SweepConfig
) -> tuple[float | None, list[dict[str, Any]]]:
    """Find the fastest inter-request interval that stays 429-free at STEADY STATE.

    Drains the bucket first (rapid requests until empty), then paces
    `config.probe_count` requests from empty. A too-fast interval throttles immediately
    from empty; a sustainable one stays clean. If the bucket can't be emptied within
    `config.drain_cap`, the measurement is invalid, so the sweep aborts with NO floor
    rather than lie.

    Returns:
        The fastest clean interval in seconds (None if none was found), and one row
        per interval tried.
    """
    logger.info("\n== PHASE: sustained-interval sweep ==")
    logger.info(
        "  drains bucket (until empty, cap %s) then paces %s reqs/interval, slow->fast.",
        config.drain_cap,
        config.probe_count,
    )

    rows: list[dict[str, Any]] = []
    fastest_safe = None
    cursors = cursor_cycle(cursor_pool)

    for interval in config.intervals:  # sorted slow -> fast (descending seconds)
        if probe.budget.remaining() < config.drain_cap + config.probe_count:
            logger.warning(
                "  stopping sweep: budget (%s) too low for drain+probe (%s+%s).",
                probe.budget.remaining(),
                config.drain_cap,
                config.probe_count,
            )
            break

        drained_reqs, emptied = _drain(probe, cursors, config.drain_cap)
        # Drain is interval-independent: if it fails once it fails for all. Abort with
        # no floor rather than report a fake-fast one from coasting on spare quota.
        if not emptied:
            logger.warning(
                "  [!] drain fired %s requests but never emptied the bucket "
                "(--sweep-drain=%s < the limit). The sweep can't measure a floor "
                "from empty here, so it won't report one. Trust the rate-limit headers, "
                "or raise --sweep-drain above the limit (costly).",
                drained_reqs,
                config.drain_cap,
            )
            return None, rows

        time.sleep(interval)  # seed ~1 token so request #1 isn't a guaranteed 429
        paced = _paced(probe, cursors, interval, config.probe_count)
        sent, throttled = paced.sent, paced.throttled
        eff_rate = sent / paced.elapsed_s if paced.elapsed_s > 0 else 0
        frac = (throttled / sent) if sent else 1.0
        clean = frac <= config.tolerance

        rows.append(
            {
                "interval_s": interval,
                "drain_requests": drained_reqs,
                "bucket_emptied": True,
                "requests": sent,
                "throttled_429": throttled,
                "throttle_frac": round(frac, 3),
                "clean": clean,
                "effective_req_per_s": round(eff_rate, 2),
            }
        )
        status = "clean" if clean else f"THROTTLED ({throttled}/{sent}={frac:.0%})"
        logger.info(
            "  interval=%-6ss  [drained in %s]  sent=%-3s 429=%-3s (%s) eff=%4.2f/s  -> %s",
            interval,
            drained_reqs,
            sent,
            throttled,
            format(frac, ".0%"),
            eff_rate,
            status,
        )

        if clean:
            fastest_safe = interval
        else:
            logger.info(
                "  => floor found: %ss throttles from empty; fastest sustainable interval = %s",
                interval,
                f"{fastest_safe}s" if fastest_safe is not None else "none of those tested",
            )
            break

    if fastest_safe is not None and rows and rows[-1]["clean"]:
        logger.info(
            "  => reached fastest tested interval (%ss) still clean; "
            "true floor may be lower — add faster values to --sweep-intervals.",
            fastest_safe,
        )
    return fastest_safe, rows


def _drain(probe: Probe, cursors: Iterator[Any], cap: int) -> tuple[int, bool]:
    """Send rapid requests until the bucket is empty or `cap` is reached.

    Returns:
        The requests sent, and whether the bucket was confirmed empty.
    """
    consecutive = 0
    used = 0
    for _ in range(cap):
        r = core.fetch(probe.session, probe.endpoint, next(cursors), probe.budget)
        if r.rclass == core.RClass.BUDGET:
            return used, False
        used += 1
        consecutive = consecutive + 1 if r.rclass == core.RClass.THROTTLED else 0
        if consecutive >= _DRAINED_AFTER:
            return used, True
    return used, False


class _PacedOutcome(NamedTuple):
    sent: int
    throttled: int
    elapsed_s: float


def _paced(probe: Probe, cursors: Iterator[Any], interval: float, count: int) -> _PacedOutcome:
    """Send `count` requests `interval` seconds apart."""
    throttled = 0
    sent = 0
    t_phase = time.perf_counter()
    for _ in range(count):
        t_req = time.perf_counter()
        r = core.fetch(probe.session, probe.endpoint, next(cursors), probe.budget)
        if r.rclass == core.RClass.BUDGET:
            break
        sent += 1
        if r.rclass == core.RClass.THROTTLED:
            throttled += 1
        slack = interval - (time.perf_counter() - t_req)
        if slack > 0:
            time.sleep(slack)
    return _PacedOutcome(sent, throttled, time.perf_counter() - t_phase)

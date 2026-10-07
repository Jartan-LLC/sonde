"""The concurrent burst phase, which also measures the throttle window."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx

from sonde import core
from sonde.core import Result
from sonde.logconfig import scrub
from sonde.phases.probe import Probe, cursor_cycle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BurstConfig:
    """The concurrent burst phase's settings.

    Attributes:
        sizes: Concurrent burst sizes, in the order they run.
        cooldown: Seconds between bursts when no window could be measured.
        recovery_step: First poll delay when measuring the throttle window.
        recovery_max: Seconds after which the window measurement gives up.
        recovery_polls: Most polls the window measurement sends.
    """

    sizes: tuple[int, ...]
    cooldown: float
    recovery_step: float
    recovery_max: float
    recovery_polls: int


@dataclass(frozen=True)
class BurstRow:
    """One burst's outcome.

    Attributes:
        burst_size: Requests in the burst.
        ok_200: Successful responses.
        throttled_429: Throttled responses.
        other: Every other outcome, errors and budget refusals included.
        wall_seconds: The burst's wall time.
        launch_spread_ms: The time from the first request's launch to the last's.
        max_retry_after: The longest Retry-After among the responses, in seconds.
    """

    burst_size: int
    ok_200: int
    throttled_429: int
    other: int
    wall_seconds: float
    launch_spread_ms: float
    max_retry_after: float | None


def phase_burst(
    probe: Probe, cursor_pool: list[Any], config: BurstConfig
) -> tuple[list[BurstRow], float | None]:
    """Fire bursts of concurrent requests, measuring the window on the first throttled one.

    Returns:
        One row per burst, and the measured window in seconds (None if no burst
        was throttled or the window couldn't be measured).
    """
    if not config.sizes:
        return [], None
    logger.info("\n== PHASE: concurrent burst probe [httpx / asyncio] ==")
    logger.info("  fires N concurrent requests on one event loop; reports launch spread.")
    return asyncio.run(_run_bursts(probe, cursor_pool, config))


def _recovery_steps(
    start_step: float, max_wait: float, max_polls: int, cursor_pool: list[Any]
) -> Generator[tuple[float, Any, float], None, None]:
    """Yield (step_seconds, cursor, cumulative_wait_s) for each recovery poll.

    A pure state machine with no I/O: callers sleep, then fetch, after each yield.
    """
    step = start_step
    waited = 0.0
    cursors = cursor_cycle(cursor_pool)
    for _ in range(max_polls):
        yield step, next(cursors), waited + step
        waited += step
        if waited >= max_wait:
            break
        step *= 1.6


async def _afetch(probe: Probe, client: httpx.AsyncClient, cursor: Any) -> Result:
    """The async twin of `core.fetch`."""
    if not probe.budget.take():
        return core.Result(
            status=-1, elapsed=0.0, rclass=core.RClass.BUDGET, error="request budget exhausted"
        )
    spec, params = core.request_args(probe.endpoint, cursor)
    t0 = time.perf_counter()
    try:
        resp = await client.request(spec.method, spec.url, params=params, json=spec.json_body)
    except httpx.RequestError as e:
        return core.Result(
            status=0,
            elapsed=time.perf_counter() - t0,
            rclass=core.RClass.ERROR,
            error=scrub(str(e)),
        )
    return core.parse_response(resp, time.perf_counter() - t0, probe.endpoint)


async def _measure_recovery(
    probe: Probe, client: httpx.AsyncClient, cursor_pool: list[Any], config: BurstConfig
) -> float | None:
    """Poll with growing delays until a request succeeds; return the wait, or None."""
    logger.info(
        "    measuring recovery window (adaptive, start≈%ss, ≤%s polls, ≤%ss)...",
        config.recovery_step,
        config.recovery_polls,
        config.recovery_max,
    )
    waited = 0.0
    for step, cur, waited in _recovery_steps(
        config.recovery_step, config.recovery_max, config.recovery_polls, cursor_pool
    ):
        await asyncio.sleep(step)
        r = await _afetch(probe, client, cur)
        if r.rclass == core.RClass.OK:
            logger.info("    recovered after ~%.2fs", waited)
            return waited
        if r.rclass == core.RClass.BUDGET:
            logger.warning("    budget exhausted during recovery probe.")
            return None
    logger.info("    no recovery within %.1fs / %s polls.", waited, config.recovery_polls)
    return None


class _BurstOutcome(NamedTuple):
    results: list[Result]
    elapsed_s: float
    spread_ms: float  # first to last launch


async def _one_burst(
    probe: Probe, client: httpx.AsyncClient, batch_cursors: list[Any]
) -> _BurstOutcome:
    """Send one request per cursor at once."""
    launches: list[float] = []

    async def one(cursor: Any) -> Result:
        launches.append(time.perf_counter())
        return await _afetch(probe, client, cursor)

    t0 = time.perf_counter()
    batch = await asyncio.gather(*[one(cur) for cur in batch_cursors])
    elapsed = time.perf_counter() - t0
    spread_ms = (max(launches) - min(launches)) * 1000 if launches else 0.0
    return _BurstOutcome(batch, elapsed, spread_ms)


async def _run_bursts(
    probe: Probe, cursor_pool: list[Any], config: BurstConfig
) -> tuple[list[BurstRow], float | None]:
    results: list[BurstRow] = []
    measured_window: float | None = None
    cursors = cursor_cycle(cursor_pool)
    biggest = max(config.sizes)
    limits = httpx.Limits(max_connections=biggest, max_keepalive_connections=biggest)
    async with httpx.AsyncClient(
        headers=probe.headers, timeout=30, follow_redirects=True, limits=limits
    ) as client:
        for i, n in enumerate(config.sizes):
            if probe.budget.remaining() < n:
                logger.warning(
                    "  skipping burst of %s: only %s requests left in budget.",
                    n,
                    probe.budget.remaining(),
                )
                break
            outcome = await _one_burst(probe, client, list(itertools.islice(cursors, n)))
            row = _summarise_burst(outcome)
            # Recovery is async, so the window is measured here, on the first throttled
            # burst, not in the bookkeeping helper.
            if row.throttled_429 > 0 and measured_window is None:
                if row.max_retry_after:
                    measured_window = row.max_retry_after
                    logger.info("    server-provided window: %.0fs", measured_window)
                else:
                    measured_window = await _measure_recovery(probe, client, cursor_pool, config)
            results.append(row)

            wait = measured_window or row.max_retry_after or config.cooldown
            if i < len(config.sizes) - 1 and probe.budget.remaining() > 0:
                logger.debug("    cooling down %.0fs before next burst...", wait)
                await asyncio.sleep(wait)
    return results, measured_window


def _summarise_burst(outcome: _BurstOutcome) -> BurstRow:
    """Count one burst's outcomes and build its row."""
    batch, elapsed, spread_ms = outcome.results, outcome.elapsed_s, outcome.spread_ms
    n = len(batch)
    ok = sum(1 for r in batch if r.rclass == core.RClass.OK)
    c429 = sum(1 for r in batch if r.rclass == core.RClass.THROTTLED)
    other = n - ok - c429
    retry_afters = [r.retry_after for r in batch if r.retry_after]
    max_ra = max(retry_afters) if retry_afters else None

    row = BurstRow(
        burst_size=n,
        ok_200=ok,
        throttled_429=c429,
        other=other,
        wall_seconds=round(elapsed, 3),
        launch_spread_ms=round(spread_ms, 1),
        max_retry_after=max_ra,
    )
    logger.info(
        "  burst=%-4s 200=%-4s 429=%-4s other=%-3s in %.2fs  launch_spread=%.0fms  retry_after=%s",
        n,
        ok,
        c429,
        other,
        elapsed,
        spread_ms,
        max_ra or "none",
    )
    return row

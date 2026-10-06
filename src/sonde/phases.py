"""phases.py — the generic rate-limit probing engine.

Every probing phase works against a `Probe` and drives its endpoint through
`core.fetch` (or, for the concurrent burst, an async httpx client); the estimate does
no I/O and works from the `Measurements`. Nothing here knows about any specific
endpoint. Phases:

  sanity     one request; read auth + x-ratelimit headers
  sequential back-to-back requests until the first 429
  burst      N concurrent requests (async httpx on one event loop)
  recovery   after a 429, measure how long until requests succeed again
  sweep      find the fastest sustained interval that stays 429-free (fallback)
  estimate   turn the measurements into a safe rate + wall-clock estimate

`core.fetch` is referenced through the module (core.fetch) so tests can monkeypatch it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, NamedTuple

import httpx
import requests

from sonde import core
from sonde.core import Budget, Result
from sonde.endpoint import Endpoint

logger = logging.getLogger(__name__)

# Consecutive throttles that show the drain has emptied the bucket: one or two could
# be transient.
_DRAINED_AFTER = 3


@dataclass(frozen=True)
class Probe:
    """What every phase works against: the endpoint, the shared budget, and the client setup."""

    endpoint: Endpoint
    budget: Budget
    session: requests.Session
    headers: dict[str, str]  # for the burst phase's own httpx client


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


@dataclass
class Measurements:
    """What the earlier phases found, which the estimate turns into a rate."""

    page_count: int  # items per successful page
    rate_limit: dict[str, Any]  # the provider's parse of the rate-limit headers
    seq_summary: dict[str, Any] = field(default_factory=dict[str, Any])
    burst_results: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    measured_window: float | None = None
    swept_interval: float | None = None


def has_authoritative_limit(rate_limit: dict[str, Any]) -> bool:
    """Whether the headers state both a limit and its window, which makes the sweep redundant."""
    return bool(rate_limit.get("limit") and rate_limit.get("window_s"))


def _cursors(cursor_pool: list[Any]) -> Iterator[Any]:
    """Cycle through the collected cursors, or yield None forever when there are none."""
    return itertools.cycle(cursor_pool or [None])


# --------------------------------------------------------------------------- #
# Sanity / auth + header read
# --------------------------------------------------------------------------- #
def phase_sanity(probe: Probe) -> tuple[Result, dict[str, Any]]:
    """Send one request, report auth and the rate-limit headers.

    Returns:
        The response, and the provider's parse of its rate-limit headers.
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
    if has_authoritative_limit(rl):
        logger.info(
            "  >> RATE LIMIT (headers, authoritative): %s per %ss window",
            rl["limit"],
            rl["window_s"],
        )
        if rl.get("remaining") is not None:
            logger.info(
                "     live: remaining=%s  resets_in=%ss",
                rl["remaining"],
                rl.get("reset_s"),
            )
        extra = [(c, w) for c, w in rl.get("policies", []) if w != rl["window_s"]]
        if extra:
            logger.info("     other quota(s): %s", extra)
    elif rl.get("limit"):
        logger.info(
            "  >> rate-limit headers present but no window: limit=%s, "
            "remaining=%s, resets_in=%ss "
            "(will fall back to the sweep for the rate estimate).",
            rl["limit"],
            rl.get("remaining"),
            rl.get("reset_s"),
        )
    else:
        logger.info("  >> no usable rate-limit headers (will fall back to empirical sweep).")
    return r, rl


# --------------------------------------------------------------------------- #
# Sequential sustained probe
# --------------------------------------------------------------------------- #
def phase_seq(probe: Probe, cap: int) -> tuple[dict[str, Any], list[Any]]:
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
    return {
        "successful_before_429": ok,
        "first_429_at_request": first_429,
        "wall_seconds": round(el, 3),
        "seq_req_per_sec": round(ok / el, 2) if el > 0 else None,
        "avg_latency_ms": round(avg * 1000, 1) if avg is not None else None,
        "retry_after": last.retry_after if last else None,
    }, cursor_pool


# --------------------------------------------------------------------------- #
# Recovery probe — backoff generator
# --------------------------------------------------------------------------- #
def _recovery_steps(
    start_step: float, max_wait: float, max_polls: int, cursor_pool: list[Any]
) -> Generator[tuple[float, Any, float], None, None]:
    """Yield (step_seconds, cursor, cumulative_wait_s) for each recovery poll.

    A pure state machine with no I/O: callers sleep, then fetch, after each yield.
    """
    step = start_step
    waited = 0.0
    cursors = _cursors(cursor_pool)
    for _ in range(max_polls):
        yield step, next(cursors), waited + step
        waited += step
        if waited >= max_wait:
            break
        step *= 1.6


# --------------------------------------------------------------------------- #
# Burst probe (async httpx / asyncio)
# --------------------------------------------------------------------------- #
def phase_burst(
    probe: Probe, cursor_pool: list[Any], config: BurstConfig
) -> tuple[list[dict[str, Any]], float | None]:
    """Fire bursts of concurrent requests, measuring the window on the first throttled one.

    Returns:
        One report row per burst, and the measured window in seconds (None if no burst
        was throttled or the window couldn't be measured).
    """
    if not config.sizes:
        return [], None
    logger.info("\n== PHASE: concurrent burst probe [httpx / asyncio] ==")
    logger.info("  fires N concurrent requests on one event loop; reports launch spread.")
    return asyncio.run(_run_bursts(probe, cursor_pool, config))


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
            status=0, elapsed=time.perf_counter() - t0, rclass=core.RClass.ERROR, error=str(e)
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
) -> tuple[list[dict[str, Any]], float | None]:
    results: list[dict[str, Any]] = []
    measured_window: float | None = None
    cursors = _cursors(cursor_pool)
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
            row = _summarise_burst(
                n, outcome.results, elapsed=outcome.elapsed_s, spread_ms=outcome.spread_ms
            )
            # Recovery is async, so the window is measured here, on the first throttled
            # burst, not in the bookkeeping helper.
            if row["throttled_429"] > 0 and measured_window is None:
                if row["max_retry_after"]:
                    measured_window = row["max_retry_after"]
                    logger.info("    server-provided window: %.0fs", measured_window)
                else:
                    measured_window = await _measure_recovery(probe, client, cursor_pool, config)
            results.append(row)

            wait = measured_window or row["max_retry_after"] or config.cooldown
            if i < len(config.sizes) - 1 and probe.budget.remaining() > 0:
                logger.debug("    cooling down %.0fs before next burst...", wait)
                await asyncio.sleep(wait)
    return results, measured_window


def _summarise_burst(
    n: int,
    batch: list[Result],
    elapsed: float,
    spread_ms: float,
) -> dict[str, Any]:
    """Count one burst's outcomes and build its report row."""
    ok = sum(1 for r in batch if r.rclass == core.RClass.OK)
    c429 = sum(1 for r in batch if r.rclass == core.RClass.THROTTLED)
    other = n - ok - c429
    retry_afters = [r.retry_after for r in batch if r.retry_after]
    max_ra = max(retry_afters) if retry_afters else None

    row: dict[str, Any] = {
        "burst_size": n,
        "ok_200": ok,
        "throttled_429": c429,
        "other": other,
        "wall_seconds": round(elapsed, 3),
        "launch_spread_ms": round(spread_ms, 1),
        "max_retry_after": max_ra,
    }
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


# --------------------------------------------------------------------------- #
# Sustained-interval sweep (fallback when headers are absent)
# --------------------------------------------------------------------------- #
def phase_sweep(
    probe: Probe, cursor_pool: list[Any], config: SweepConfig
) -> tuple[float | None, list[dict[str, Any]]]:
    """Find the fastest inter-request interval that stays 429-free at STEADY STATE.

    Drains the bucket first (rapid requests until empty), then paces `probe_count`
    requests from empty. A too-fast interval throttles immediately from empty; a
    sustainable one stays clean. If the bucket can't be emptied within `drain_cap`,
    the measurement is invalid, so the sweep aborts with NO floor rather than lie.

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
    cursors = _cursors(cursor_pool)

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


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
@dataclass
class _Rate:
    """A safe request rate and how it was derived."""

    per_min: float | None = None
    interval: float | None = None  # recommended seconds between requests, when paced
    basis: str | None = None
    header_limit: int | None = None
    header_window: float | None = None


def _safe_rate(m: Measurements, margin: float) -> _Rate:
    """Pick the most trustworthy source of a safe rate, applying `margin`.

    Priority: authoritative headers, the swept floor, the inferred token bucket,
    then the sequential fallbacks.
    """
    rl = m.rate_limit
    if has_authoritative_limit(rl):
        limit, window = rl["limit"], rl["window_s"]
        max_per_min = limit * 60.0 / window
        even = window / limit
        interval = even / margin
        logger.info(
            "  RATE LIMIT (headers): %s per %ss = %.0f req/min ceiling",
            limit,
            window,
            max_per_min,
        )
        logger.info(
            "    even-pace interval : %.3fs (recommend %.3fs with %s%% margin)",
            even,
            interval,
            round(margin * 100),
        )
        logger.info(
            "    practical limiter  : use the live remaining/reset counters "
            "(reset_s is normalised seconds-until); when remaining hits 0, wait reset_s "
            "seconds. Retry-After is a backoff hint, not the window length."
        )
        return _Rate(
            per_min=60.0 / interval,
            interval=interval,
            basis=f"AUTHORITATIVE headers: {limit}/{window}s ({max_per_min:.0f}/min ceiling)",
            header_limit=limit,
            header_window=window,
        )

    if m.swept_interval:
        interval = m.swept_interval / margin
        return _Rate(
            per_min=60.0 / interval,
            interval=interval,
            basis=(
                f"measured floor {m.swept_interval}s -> {interval:.3f}s "
                f"({round(margin * 100)}% of measured max)"
            ),
        )

    fully_ok = [r for r in m.burst_results if r["throttled_429"] == 0]
    if fully_ok and m.measured_window and m.measured_window > 0:
        bucket = max(r["burst_size"] for r in fully_ok)
        return _Rate(
            per_min=(bucket / m.measured_window) * 60.0 * margin,
            basis=f"INFERRED bucket≈{bucket}/window≈{m.measured_window:.1f}s (model-dependent)",
        )

    seq = m.seq_summary
    if seq.get("first_429_at_request"):
        n = seq["successful_before_429"]
        t = seq["wall_seconds"] or 1
        return _Rate(per_min=(n / t) * 60.0 * margin, basis=f"sequential {n} req / {t:.1f}s")
    if seq.get("seq_req_per_sec"):
        # Nothing throttled anywhere, so there's no measured ceiling. Treat observed
        # throughput as a soft ceiling, apply --margin, then halve again for the
        # extra uncertainty — this is the most conservative rung by construction.
        factor = 0.5 * margin
        return _Rate(
            per_min=seq["seq_req_per_sec"] * 60.0 * factor,
            basis=f"no 429 observed; {factor:.0%} of measured sequential throughput",
        )
    return _Rate()


def phase_estimate(endpoint: Endpoint, m: Measurements, margin: float) -> dict[str, Any]:
    """Turn the measurements into a safe rate and, with a known total, a scrape time.

    Args:
        endpoint: The endpoint probed; its `total_items()` sizes the scrape.
        m: What the earlier phases measured.
        margin: The fraction of the measured ceiling to recommend, such as 0.8.

    Returns:
        The estimate section of the report.
    """
    logger.info("\n== PHASE: rate + wall-clock estimate ==")
    rate = _safe_rate(m, margin)

    total_items = endpoint.total_items()
    total_pages = None
    if m.page_count > 0 and total_items is not None:
        total_pages = math.ceil(total_items / m.page_count)
        logger.info("  total items         : %s", format(total_items, ","))
        logger.info("  items per page      : %s", m.page_count)
        logger.info("  => total requests   : %s", format(total_pages, ","))
    else:
        logger.info(
            "  total items unknown (no endpoint total / no page count) -> reporting rate only."
        )

    if rate.per_min:
        if rate.interval:
            logger.info(
                "  recommended interval: %.3fs  (~%.0f req/min)", rate.interval, rate.per_min
            )
        else:
            logger.info("  safe rate estimate  : ~%.0f req/min", rate.per_min)
        logger.info("  basis               : %s", rate.basis)
        if total_pages is not None:
            minutes = total_pages / rate.per_min
            logger.info("  => full scrape time : ~%.0f min  (~%.1f h)", minutes, minutes / 60)
    else:
        logger.info(
            "  safe rate estimate  : insufficient data (nothing throttled) — re-run "
            "with faster --sweep-intervals or larger --burst-sizes."
        )

    return {
        "total_items": total_items,
        "items_per_page": m.page_count,
        "total_pages": total_pages,
        "header_limit": rate.header_limit,
        "header_window_s": rate.header_window,
        "swept_floor_interval_s": m.swept_interval,
        "recommended_interval_s": round(rate.interval, 4) if rate.interval else None,
        "measured_window_seconds": round(m.measured_window, 2) if m.measured_window else None,
        "safe_rate_per_min": round(rate.per_min, 1) if rate.per_min else None,
        "safe_rate_basis": rate.basis,
        "estimated_minutes": (
            round(total_pages / rate.per_min, 1)
            if (total_pages is not None and rate.per_min)
            else None
        ),
    }

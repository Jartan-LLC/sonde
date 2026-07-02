"""
phases.py — the generic rate-limit probing engine.

Every phase takes an `Endpoint` and drives it through `core.fetch` (or, for the
concurrent burst, an async httpx client). Nothing here knows about any specific
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
import json
import logging
import math
import time
from collections.abc import Generator
from typing import Any

import httpx
import requests

from . import core
from .core import Budget, Result
from .endpoint import Endpoint

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sanity / auth + header read
# --------------------------------------------------------------------------- #
def phase_sanity(
    session: requests.Session, endpoint: Endpoint, budget: Budget
) -> tuple[Result, dict[str, Any]]:
    logger.info("\n== PHASE: sanity / auth ==")
    r = core.fetch(session, endpoint, None, budget)
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
            r.rclass.value,
            r.elapsed * 1000,
            r.error,
        )
        if r.status in (401, 403):
            logger.warning(
                "  -> looks like an auth problem. Set the provider's credential env var."
            )
    if r.headers and logger.isEnabledFor(logging.DEBUG):
        logger.debug("  headers: %s", json.dumps(r.headers))

    rl = endpoint.provider().parse_rate_limit(r.headers)
    if rl.get("limit") and rl.get("window_s"):
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
def phase_seq(
    session: requests.Session, endpoint: Endpoint, budget: Budget, cap: int
) -> tuple[dict[str, Any], list[Any]]:
    logger.info("\n== PHASE: sequential sustained probe ==")
    logger.info("  up to %s back-to-back requests until the first 429...", cap)
    cursor = None
    cursor_pool = []
    ok = 0
    first_429 = None
    t_start = time.perf_counter()
    latencies = []
    last = None

    for i in range(cap):
        r = core.fetch(session, endpoint, cursor, budget)
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
# Recovery probe — shared backoff generator (async burst path measures inline)
# --------------------------------------------------------------------------- #
def _recovery_steps(
    start_step: float, max_wait: float, max_polls: int, cursor_pool: list[Any]
) -> Generator[tuple[float, Any, float], None, None]:
    """Yield (step_seconds, cursor, cumulative_wait_s) for each recovery poll.
    Pure state machine — no I/O. Callers sleep then fetch after each yield."""
    step = start_step
    waited = 0.0
    i = 0
    for _ in range(max_polls):
        cur = cursor_pool[i % len(cursor_pool)] if cursor_pool else None
        i += 1
        yield step, cur, waited + step
        waited += step
        if waited >= max_wait:
            break
        step *= 1.6


# --------------------------------------------------------------------------- #
# Burst probe (async httpx / asyncio)
# --------------------------------------------------------------------------- #
def phase_burst(
    headers: dict[str, str],
    endpoint: Endpoint,
    budget: Budget,
    sizes: list[int],
    cooldown: float,
    cursor_pool: list[Any],
    recovery_step: float,
    recovery_max: float,
    recovery_polls: int,
) -> tuple[list[dict[str, Any]], float | None]:
    if not sizes:
        return [], None
    logger.info("\n== PHASE: concurrent burst probe [httpx / asyncio] ==")
    logger.info("  fires N concurrent requests on one event loop; reports launch spread.")

    async def afetch(client, cursor):
        if not budget.take():
            return core.Result(
                status=-1, elapsed=0.0, rclass=core.RClass.BUDGET, error="request budget exhausted"
            )
        provider = endpoint.provider()
        spec = endpoint.build_request(cursor)
        params = {**provider.auth_params(), **(spec.params or {})}
        t0 = time.perf_counter()
        try:
            resp = await client.request(spec.method, spec.url, params=params, json=spec.json_body)
        except httpx.RequestError as e:
            return core.Result(
                status=0, elapsed=time.perf_counter() - t0, rclass=core.RClass.ERROR, error=str(e)
            )
        return core._parse_response(resp, time.perf_counter() - t0, endpoint)

    async def recovery(client):
        logger.info(
            "    measuring recovery window (adaptive, start≈%ss, ≤%s polls, ≤%ss)...",
            recovery_step,
            recovery_polls,
            recovery_max,
        )
        waited = 0.0
        for step, cur, waited in _recovery_steps(
            recovery_step, recovery_max, recovery_polls, cursor_pool
        ):
            await asyncio.sleep(step)
            r = await afetch(client, cur)
            if r.rclass == core.RClass.OK:
                logger.info("    recovered after ~%.2fs", waited)
                return waited
            if r.rclass == core.RClass.BUDGET:
                logger.warning("    budget exhausted during recovery probe.")
                return None
        logger.info("    no recovery within %.1fs / %s polls.", waited, recovery_polls)
        return None

    async def run():
        results = []
        measured_window = None
        idx_base = 0
        limits = httpx.Limits(max_connections=max(sizes), max_keepalive_connections=max(sizes))
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=True, limits=limits
        ) as client:
            for n in sizes:
                if budget.remaining() < n:
                    logger.warning(
                        "  skipping burst of %s: only %s requests left in budget.",
                        n,
                        budget.remaining(),
                    )
                    break
                launches = []

                async def one(i):
                    cur = cursor_pool[(idx_base + i) % len(cursor_pool)] if cursor_pool else None
                    launches.append(time.perf_counter())
                    return await afetch(client, cur)

                t0 = time.perf_counter()
                batch = await asyncio.gather(*[one(i) for i in range(n)])
                elapsed = time.perf_counter() - t0
                idx_base += n
                spread_ms = (max(launches) - min(launches)) * 1000 if launches else 0.0

                row = _summarise_burst(n, batch, elapsed, spread_ms)
                # recovery is async here, so measure the window inline on the first
                # throttled burst rather than inside the bookkeeping helper.
                if row["throttled_429"] > 0 and measured_window is None:
                    if row["max_retry_after"]:
                        measured_window = row["max_retry_after"]
                        logger.info("    server-provided window: %.0fs", measured_window)
                    else:
                        measured_window = await recovery(client)
                results.append(row)

                wait = measured_window or row["max_retry_after"] or cooldown
                if n != sizes[-1] and budget.remaining() > 0:
                    logger.debug("    cooling down %.0fs before next burst...", wait)
                    await asyncio.sleep(wait)
        return results, measured_window

    return asyncio.run(run())


def _summarise_burst(
    n: int,
    batch: list[Result],
    elapsed: float,
    spread_ms: float,
) -> dict[str, Any]:
    """Count one burst's outcomes and build its report row. Window/recovery decisions
    live at the call site (the async burst measures recovery inline)."""
    ok = sum(1 for r in batch if r.rclass == core.RClass.OK)
    c429 = sum(1 for r in batch if r.rclass == core.RClass.THROTTLED)
    other = n - ok - c429
    retry_afters = [r.retry_after for r in batch if r.retry_after]
    max_ra = max(retry_afters) if retry_afters else None

    row = {
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
        max_ra if max_ra else "none",
    )
    return row


# --------------------------------------------------------------------------- #
# Sustained-interval sweep (fallback when headers are absent)
# --------------------------------------------------------------------------- #
def phase_sweep(
    session: requests.Session,
    endpoint: Endpoint,
    budget: Budget,
    cursor_pool: list[Any],
    intervals: list[float],
    probe_count: int,
    drain_cap: int,
    tolerance: float,
) -> tuple[float | None, list[dict[str, Any]]]:
    """Find the fastest inter-request interval that stays 429-free at STEADY STATE.
    Drains the bucket first (rapid requests until empty), then paces `probe_count`
    requests from empty. A too-fast interval throttles immediately from empty; a
    sustainable one stays clean. If the bucket can't be emptied within `drain_cap`,
    the measurement is invalid, so the sweep aborts with NO floor rather than lie.

    Returns (fastest_safe_interval_seconds, rows)."""
    logger.info("\n== PHASE: sustained-interval sweep ==")
    logger.info(
        "  drains bucket (until empty, cap %s) then paces %s reqs/interval, slow->fast.",
        drain_cap,
        probe_count,
    )

    rows = []
    fastest_safe = None
    idx = 0

    def next_cursor():
        nonlocal idx
        cur = cursor_pool[idx % len(cursor_pool)] if cursor_pool else None
        idx += 1
        return cur

    def drain():
        """Fire rapid requests until empty (3 consecutive throttles) or the cap."""
        consecutive = 0
        used = 0
        for _ in range(drain_cap):
            r = core.fetch(session, endpoint, next_cursor(), budget)
            used += 1
            if r.rclass == core.RClass.BUDGET:
                return used, False
            consecutive = consecutive + 1 if r.rclass == core.RClass.THROTTLED else 0
            if consecutive >= 3:
                return used, True
        # Cap exhausted without 3 consecutive throttles -> never confirmed empty. A lone
        # or paired 429 could be transient, so don't claim the bucket was drained.
        return used, False

    for interval in intervals:  # sorted slow -> fast (descending seconds)
        if budget.remaining() < drain_cap + probe_count:
            logger.warning(
                "  stopping sweep: budget (%s) too low for drain+probe (%s+%s).",
                budget.remaining(),
                drain_cap,
                probe_count,
            )
            break

        drained_reqs, emptied = drain()
        # Drain is interval-independent: if it fails once it fails for all. Abort with
        # no floor rather than report a fake-fast one from coasting on spare quota.
        if not emptied:
            logger.warning(
                "  [!] drain fired %s requests but never emptied the bucket "
                "(--sweep-drain=%s < the limit). The sweep can't measure a floor "
                "from empty here, so it won't report one. Trust the rate-limit headers, "
                "or raise --sweep-drain above the limit (costly).",
                drained_reqs,
                drain_cap,
            )
            return None, rows

        time.sleep(interval)  # seed ~1 token so request #1 isn't a guaranteed 429

        throttled = 0
        sent = 0
        t_phase = time.perf_counter()
        for _ in range(probe_count):
            t_req = time.perf_counter()
            r = core.fetch(session, endpoint, next_cursor(), budget)
            if r.rclass == core.RClass.BUDGET:
                break
            sent += 1
            if r.rclass == core.RClass.THROTTLED:
                throttled += 1
            slack = interval - (time.perf_counter() - t_req)
            if slack > 0:
                time.sleep(slack)
        dur = time.perf_counter() - t_phase
        eff_rate = sent / dur if dur > 0 else 0
        frac = (throttled / sent) if sent else 1.0
        clean = frac <= tolerance

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
                "  => floor found: %ss throttles from empty; fastest sustainable interval = %ss",
                interval,
                fastest_safe,
            )
            break

    if fastest_safe is not None and rows and rows[-1]["clean"]:
        logger.info(
            "  => reached fastest tested interval (%ss) still clean; "
            "true floor may be lower — add faster values to --sweep-intervals.",
            fastest_safe,
        )
    return fastest_safe, rows


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
def phase_estimate(
    endpoint: Endpoint,
    page_count: int,
    seq_summary: dict[str, Any],
    burst_results: list[dict[str, Any]],
    measured_window: float | None,
    swept_interval: float | None,
    margin: float,
    rl: dict[str, Any],
) -> dict[str, Any]:
    logger.info("\n== PHASE: rate + wall-clock estimate ==")

    safe_rate_per_min = None
    basis = None
    recommended_interval = None
    header_limit = header_window = None

    # Priority 0: authoritative rate-limit headers.
    if rl and rl.get("limit") and rl.get("window_s"):
        header_limit, header_window = rl["limit"], rl["window_s"]
        max_per_min = header_limit * 60.0 / header_window
        even = header_window / header_limit
        recommended_interval = even / margin
        safe_rate_per_min = 60.0 / recommended_interval
        basis = (
            f"AUTHORITATIVE headers: {header_limit}/{header_window}s"
            f" ({max_per_min:.0f}/min ceiling)"
        )
        logger.info(
            "  RATE LIMIT (headers): %s per %ss = %.0f req/min ceiling",
            header_limit,
            header_window,
            max_per_min,
        )
        logger.info(
            "    even-pace interval : %.3fs (recommend %.3fs with %s%% margin)",
            even,
            recommended_interval,
            round(margin * 100),
        )
        logger.info(
            "    practical limiter  : use the live remaining/reset counters "
            "(reset_s is normalised seconds-until); when remaining hits 0, wait reset_s "
            "seconds. Retry-After is a backoff hint, not the window length."
        )

    # Priority 1: swept floor.
    if safe_rate_per_min is None and swept_interval:
        recommended_interval = swept_interval / margin
        safe_rate_per_min = 60.0 / recommended_interval
        basis = (
            f"measured floor {swept_interval}s -> {recommended_interval:.3f}s "
            f"({round(margin * 100)}% of measured max)"
        )
    # Priority 2: token-bucket inference.
    if safe_rate_per_min is None:
        fully_ok = [r for r in burst_results if r["throttled_429"] == 0]
        if fully_ok and measured_window and measured_window > 0:
            bucket = max(r["burst_size"] for r in fully_ok)
            safe_rate_per_min = (bucket / measured_window) * 60.0 * margin
            basis = f"INFERRED bucket≈{bucket}/window≈{measured_window:.1f}s (model-dependent)"
    # Priority 3/4: sequential fallbacks.
    if safe_rate_per_min is None and seq_summary.get("first_429_at_request"):
        n = seq_summary["successful_before_429"]
        t = seq_summary["wall_seconds"] or 1
        safe_rate_per_min = (n / t) * 60.0 * margin
        basis = f"sequential {n} req / {t:.1f}s"
    if safe_rate_per_min is None and seq_summary.get("seq_req_per_sec"):
        safe_rate_per_min = seq_summary["seq_req_per_sec"] * 60.0 * 0.5
        basis = "no 429 observed; half of measured sequential throughput"

    total_items = endpoint.total_items()
    total_pages = None
    if page_count > 0 and total_items is not None:
        total_pages = math.ceil(total_items / page_count)
        logger.info("  total items         : %s", format(total_items, ","))
        logger.info("  items per page      : %s", page_count)
        logger.info("  => total requests   : %s", format(total_pages, ","))
    else:
        logger.info(
            "  total items unknown (no endpoint total / no page count) -> reporting rate only."
        )

    if safe_rate_per_min:
        if recommended_interval:
            logger.info(
                "  recommended interval: %.3fs  (~%.0f req/min)",
                recommended_interval,
                safe_rate_per_min,
            )
        else:
            logger.info("  safe rate estimate  : ~%.0f req/min", safe_rate_per_min)
        logger.info("  basis               : %s", basis)
        if total_pages is not None:
            minutes = total_pages / safe_rate_per_min
            logger.info("  => full scrape time : ~%.0f min  (~%.1f h)", minutes, minutes / 60)
    else:
        logger.info(
            "  safe rate estimate  : insufficient data (nothing throttled) — re-run "
            "with faster --sweep-intervals or larger --burst-sizes."
        )

    return {
        "total_items": total_items,
        "items_per_page": page_count,
        "total_pages": total_pages,
        "header_limit": header_limit,
        "header_window_s": header_window,
        "swept_floor_interval_s": swept_interval,
        "recommended_interval_s": round(recommended_interval, 4) if recommended_interval else None,
        "measured_window_seconds": round(measured_window, 2) if measured_window else None,
        "safe_rate_per_min": round(safe_rate_per_min, 1) if safe_rate_per_min else None,
        "safe_rate_basis": basis,
        "estimated_minutes": (
            round(total_pages / safe_rate_per_min, 1)
            if (total_pages is not None and safe_rate_per_min)
            else None
        ),
    }

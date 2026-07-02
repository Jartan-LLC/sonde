"""
phases.py — the generic rate-limit probing engine.

Every phase takes an `Endpoint` and drives it through `core.fetch` (or, for the
concurrent burst, an async httpx client). Nothing here knows about any specific
endpoint. Phases:

  sanity     one request; read auth + x-ratelimit headers
  sequential back-to-back requests until the first 429
  burst      N concurrent requests (threaded, or async httpx via --use-httpx)
  recovery   after a 429, measure how long until requests succeed again
  sweep      find the fastest sustained interval that stays 429-free (fallback)
  estimate   turn the measurements into a safe rate + wall-clock estimate

`core.fetch` is referenced through the module (core.fetch) so tests can monkeypatch it.
"""

import asyncio
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

from . import core

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sanity / auth + header read
# --------------------------------------------------------------------------- #
def phase_sanity(session, endpoint, budget):
    logger.info("\n== PHASE: sanity / auth ==")
    r = core.fetch(session, endpoint, None, budget)
    if r.rclass == core.RClass.OK:
        logger.info(
            f"  OK  status={r.status}  items_returned={r.count}  latency={r.elapsed * 1000:.0f}ms"
        )
        logger.info(f"      next_cursor_present={bool(r.next_cursor)}")
    else:
        logger.info(
            f"  status={r.status} ({r.rclass.value})  "
            f"latency={r.elapsed * 1000:.0f}ms  error={r.error!r}"
        )
        if r.status in (401, 403):
            logger.warning(
                "  -> looks like an auth problem. Set the provider's credential env var."
            )
    if r.headers:
        logger.debug(f"  headers: {json.dumps(r.headers)}")

    rl = endpoint.provider().parse_rate_limit(r.headers)
    if rl.get("limit") and rl.get("window_s"):
        logger.info(
            f"  >> RATE LIMIT (headers, authoritative): {rl['limit']} per {rl['window_s']}s window"
        )
        if rl.get("remaining") is not None:
            logger.info(f"     live: remaining={rl['remaining']}  resets_in={rl.get('reset_s')}s")
        extra = [(c, w) for c, w in rl.get("policies", []) if w != rl["window_s"]]
        if extra:
            logger.info(f"     other quota(s): {extra}")
    elif rl.get("limit"):
        logger.info(
            f"  >> rate-limit headers present but no window: limit={rl['limit']}, "
            f"remaining={rl.get('remaining')}, resets_in={rl.get('reset_s')}s "
            f"(will fall back to the sweep for the rate estimate)."
        )
    else:
        logger.info("  >> no usable rate-limit headers (will fall back to empirical sweep).")
    return r, rl


# --------------------------------------------------------------------------- #
# Sequential sustained probe
# --------------------------------------------------------------------------- #
def phase_seq(session, endpoint, budget, cap):
    logger.info("\n== PHASE: sequential sustained probe ==")
    logger.info(f"  up to {cap} back-to-back requests until the first 429...")
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
                f"  throttled after {ok} successful in {el:.2f}s (~{rate:.1f}/s). "
                f"Retry-After={r.retry_after}"
            )
            if r.headers:
                logger.debug(f"  throttle headers: {json.dumps(r.headers)}")
            break
        elif r.rclass == core.RClass.BUDGET:
            logger.warning("  budget exhausted before being throttled.")
            break
        else:
            logger.warning(f"  unexpected status={r.status} error={r.error!r}; stopping.")
            break

    el = time.perf_counter() - t_start
    avg = (sum(latencies) / len(latencies)) if latencies else None
    if first_429 is None and ok:
        rate = ok / el if el > 0 else float("inf")
        logger.info(
            f"  no 429 in {ok} requests over {el:.2f}s (~{rate:.1f}/s); "
            f"ceiling is likely a burst/window cap -> see burst."
        )
    return {
        "successful_before_429": ok,
        "first_429_at_request": first_429,
        "wall_seconds": round(el, 3),
        "seq_req_per_sec": round(ok / el, 2) if el > 0 else None,
        "avg_latency_ms": round(avg * 1000, 1) if avg else None,
        "retry_after": last.retry_after if last else None,
    }, cursor_pool


# --------------------------------------------------------------------------- #
# Recovery probe (sync) — how long after a 429 until requests succeed again
# --------------------------------------------------------------------------- #
def measure_recovery(session, endpoint, budget, cursor_pool, start_step, max_wait, max_polls):
    """Geometric backoff: fine early (to catch a sub-second refill), widening so a
    long window still finishes within max_polls requests. Returns cumulative wait at
    first success, or None."""
    logger.info(
        f"    measuring recovery window (adaptive, start≈{start_step}s, "
        f"≤{max_polls} polls, ≤{max_wait}s)..."
    )
    waited = 0.0
    step = start_step
    i = 0
    for _ in range(max_polls):
        time.sleep(step)
        waited += step
        cur = cursor_pool[i % len(cursor_pool)] if cursor_pool else None
        i += 1
        r = core.fetch(session, endpoint, cur, budget)
        if r.rclass == core.RClass.OK:
            logger.info(f"    recovered after ~{waited:.2f}s")
            return waited
        if r.rclass == core.RClass.BUDGET:
            logger.warning("    budget exhausted during recovery probe.")
            return None
        if waited >= max_wait:
            break
        step *= 1.6
    logger.info(f"    no recovery within {waited:.1f}s / {max_polls} polls.")
    return None


# --------------------------------------------------------------------------- #
# Burst probe (threaded)
# --------------------------------------------------------------------------- #
def phase_burst(
    session,
    endpoint,
    budget,
    sizes,
    cooldown,
    cursor_pool,
    recovery_step,
    recovery_max,
    recovery_polls,
):
    logger.info("\n== PHASE: concurrent burst probe [threaded] ==")
    logger.info("  fires N truly-concurrent requests (pool sized to N); reports launch spread.")
    results = []
    measured_window = None

    def one(idx):
        cur = cursor_pool[idx % len(cursor_pool)] if cursor_pool else None
        t_launch = time.perf_counter()
        return t_launch, core.fetch(session, endpoint, cur, budget)

    for n in sizes:
        if budget.remaining() < n:
            logger.warning(
                f"  skipping burst of {n}: only {budget.remaining()} requests left in budget."
            )
            break

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=n) as pool:
            pairs = list(pool.map(one, range(n)))
        elapsed = time.perf_counter() - t0

        launches = [t for t, _ in pairs]
        batch = [r for _, r in pairs]
        spread_ms = (max(launches) - min(launches)) * 1000 if launches else 0.0
        measured_window, row = _summarise_burst(
            n,
            batch,
            elapsed,
            spread_ms,
            measured_window,
            lambda: measure_recovery(
                session, endpoint, budget, cursor_pool, recovery_step, recovery_max, recovery_polls
            ),
        )
        results.append(row)

        wait = measured_window or row["max_retry_after"] or cooldown
        if n != sizes[-1] and budget.remaining() > 0:
            logger.debug(f"    cooling down {wait:.0f}s before next burst...")
            time.sleep(wait)

    return results, measured_window


# --------------------------------------------------------------------------- #
# Burst probe (async httpx). Same behaviour; httpx imported lazily.
# --------------------------------------------------------------------------- #
def phase_burst_async(
    headers,
    endpoint,
    budget,
    sizes,
    cooldown,
    cursor_pool,
    recovery_step,
    recovery_max,
    recovery_polls,
):
    import httpx  # lazy: only needed for --use-httpx

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
            f"    measuring recovery window (adaptive, start≈{recovery_step}s, "
            f"≤{recovery_polls} polls, ≤{recovery_max}s)..."
        )
        waited = 0.0
        step = recovery_step
        i = 0
        for _ in range(recovery_polls):
            await asyncio.sleep(step)
            waited += step
            cur = cursor_pool[i % len(cursor_pool)] if cursor_pool else None
            i += 1
            r = await afetch(client, cur)
            if r.rclass == core.RClass.OK:
                logger.info(f"    recovered after ~{waited:.2f}s")
                return waited
            if r.rclass == core.RClass.BUDGET:
                logger.warning("    budget exhausted during recovery probe.")
                return None
            if waited >= recovery_max:
                break
            step *= 1.6
        logger.info(f"    no recovery within {waited:.1f}s / {recovery_polls} polls.")
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
                        f"  skipping burst of {n}: "
                        f"only {budget.remaining()} requests left in budget."
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

                # recovery is async here, so summarise inline rather than via callback
                mw_before = measured_window
                measured_window, row = _summarise_burst(
                    n, batch, elapsed, spread_ms, measured_window, recovery_cb=None
                )
                if row["throttled_429"] > 0 and mw_before is None and measured_window is None:
                    if row["max_retry_after"]:
                        measured_window = row["max_retry_after"]
                        logger.info(f"    server-provided window: {measured_window:.0f}s")
                    else:
                        measured_window = await recovery(client)
                results.append(row)

                wait = measured_window or row["max_retry_after"] or cooldown
                if n != sizes[-1] and budget.remaining() > 0:
                    logger.debug(f"    cooling down {wait:.0f}s before next burst...")
                    await asyncio.sleep(wait)
        return results, measured_window

    return asyncio.run(run())


def _summarise_burst(n, batch, elapsed, spread_ms, measured_window, recovery_cb):
    """Shared burst bookkeeping for the threaded and async paths. If recovery_cb is
    given (sync path) it's called to measure the window on the first throttled burst."""
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
        f"  burst={n:<4} 200={ok:<4} 429={c429:<4} other={other:<3} "
        f"in {elapsed:.2f}s  launch_spread={spread_ms:.0f}ms  "
        f"retry_after={max_ra if max_ra else 'none'}"
    )

    if c429 > 0 and measured_window is None and recovery_cb is not None:
        if max_ra:
            measured_window = max_ra
            logger.info(f"    server-provided window: {measured_window:.0f}s")
        else:
            measured_window = recovery_cb()
    return measured_window, row


# --------------------------------------------------------------------------- #
# Sustained-interval sweep (fallback when headers are absent)
# --------------------------------------------------------------------------- #
def phase_sweep(
    session, endpoint, budget, cursor_pool, intervals, probe_count, drain_cap, tolerance
):
    """Find the fastest inter-request interval that stays 429-free at STEADY STATE.
    Drains the bucket first (rapid requests until empty), then paces `probe_count`
    requests from empty. A too-fast interval throttles immediately from empty; a
    sustainable one stays clean. If the bucket can't be emptied within `drain_cap`,
    the measurement is invalid, so the sweep aborts with NO floor rather than lie.

    Returns (fastest_safe_interval_seconds, rows)."""
    logger.info("\n== PHASE: sustained-interval sweep ==")
    logger.info(
        f"  drains bucket (until empty, cap {drain_cap}) then paces "
        f"{probe_count} reqs/interval, slow->fast."
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
        return used, consecutive > 0

    for interval in intervals:  # sorted slow -> fast (descending seconds)
        if budget.remaining() < drain_cap + probe_count:
            logger.warning(
                f"  stopping sweep: budget ({budget.remaining()}) too low for "
                f"drain+probe ({drain_cap}+{probe_count})."
            )
            break

        drained_reqs, emptied = drain()
        # Drain is interval-independent: if it fails once it fails for all. Abort with
        # no floor rather than report a fake-fast one from coasting on spare quota.
        if not emptied:
            logger.warning(
                f"  [!] drain fired {drained_reqs} requests but never emptied the bucket "
                f"(--sweep-drain={drain_cap} < the limit). The sweep can't measure a floor "
                f"from empty here, so it won't report one. Trust the rate-limit headers, "
                f"or raise --sweep-drain above the limit (costly)."
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
            f"  interval={interval:<6}s  [drained in {drained_reqs}]  "
            f"sent={sent:<3} 429={throttled:<3} ({frac:.0%}) eff={eff_rate:4.2f}/s  -> {status}"
        )

        if clean:
            fastest_safe = interval
        else:
            logger.info(
                f"  => floor found: {interval}s throttles from empty; "
                f"fastest sustainable interval = {fastest_safe}s"
            )
            break

    if fastest_safe is not None and rows and rows[-1]["clean"]:
        logger.info(
            f"  => reached fastest tested interval ({fastest_safe}s) still clean; "
            f"true floor may be lower — add faster values to --sweep-intervals."
        )
    return fastest_safe, rows


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
def phase_estimate(
    endpoint, page_count, seq_summary, burst_results, measured_window, swept_interval, margin, rl
):
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
            f"  RATE LIMIT (headers): {header_limit} per {header_window}s = "
            f"{max_per_min:.0f} req/min ceiling"
        )
        logger.info(
            f"    even-pace interval : {even:.3f}s "
            f"(recommend {recommended_interval:.3f}s with {round(margin * 100)}% margin)"
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
    if page_count and total_items:
        total_pages = math.ceil(total_items / page_count)
        logger.info(f"  total items         : {total_items:,}")
        logger.info(f"  items per page      : {page_count}")
        logger.info(f"  => total requests   : {total_pages:,}")
    else:
        logger.info(
            "  total items unknown (no endpoint total / no page count) -> reporting rate only."
        )

    if safe_rate_per_min:
        if recommended_interval:
            logger.info(
                f"  recommended interval: {recommended_interval:.3f}s  "
                f"(~{safe_rate_per_min:.0f} req/min)"
            )
        else:
            logger.info(f"  safe rate estimate  : ~{safe_rate_per_min:.0f} req/min")
        logger.info(f"  basis               : {basis}")
        if total_pages:
            minutes = total_pages / safe_rate_per_min
            logger.info(f"  => full scrape time : ~{minutes:.0f} min  (~{minutes / 60:.1f} h)")
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
            if (total_pages and safe_rate_per_min)
            else None
        ),
    }

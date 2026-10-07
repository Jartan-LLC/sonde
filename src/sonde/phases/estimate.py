"""The estimate: a safe rate, and a scrape time when the total is known, from the measurements."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from sonde.endpoint import Endpoint
from sonde.phases.burst import BurstRow
from sonde.phases.sequential import SequentialSummary
from sonde.provider import RateLimit, authoritative_limit

logger = logging.getLogger(__name__)


@dataclass
class Measurements:
    """What the earlier phases found, which the estimate turns into a rate.

    Attributes:
        page_count: Items per successful page.
        rate_limit: The provider's parse of the rate-limit headers, if there were any.
        seq_summary: `phase_seq`'s summary.
        burst_results: `phase_burst`'s rows.
        measured_window: The throttle window `phase_burst` measured, in seconds.
        swept_interval: The fastest clean interval `phase_sweep` found, in seconds.
    """

    page_count: int
    rate_limit: RateLimit | None
    seq_summary: SequentialSummary
    burst_results: list[BurstRow] = field(default_factory=list[BurstRow])
    measured_window: float | None = None
    swept_interval: float | None = None


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
    stated = authoritative_limit(m.rate_limit)
    if stated is not None:
        limit, window = stated
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

    fully_ok = [r for r in m.burst_results if r.throttled_429 == 0]
    if fully_ok and m.measured_window and m.measured_window > 0:
        bucket = max(r.burst_size for r in fully_ok)
        return _Rate(
            per_min=(bucket / m.measured_window) * 60.0 * margin,
            basis=f"INFERRED bucket≈{bucket}/window≈{m.measured_window:.1f}s (model-dependent)",
        )

    seq = m.seq_summary
    if seq.first_429_at_request:
        n = seq.successful_before_429
        t = seq.wall_seconds or 1
        return _Rate(per_min=(n / t) * 60.0 * margin, basis=f"sequential {n} req / {t:.1f}s")
    if seq.seq_req_per_sec:
        # Nothing throttled anywhere, so there's no measured ceiling. Treat observed
        # throughput as a soft ceiling, apply --margin, then halve again for the
        # extra uncertainty — this is the most conservative rung by construction.
        factor = 0.5 * margin
        return _Rate(
            per_min=seq.seq_req_per_sec * 60.0 * factor,
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

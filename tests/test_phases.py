"""Tests for the probing phases: sequential, sweep (floor + abort), burst summary,
and the estimate's rate-source priority. Uses the virtual `clock` so pacing/sleeps
resolve instantly and deterministically."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from sonde import core, logconfig, phases
from sonde.logconfig import register_log_secrets
from sonde.phases import burst, probe, sweep
from sonde.provider import Provider
from tests.helpers import FakeClock, FakeEndpoint, Handler, make_bucket, make_probe

# A sequential phase that measured nothing, so the estimate falls past its rungs.
_NO_SEQ = phases.SequentialSummary(
    successful_before_429=0,
    first_429_at_request=None,
    wall_seconds=0.0,
    seq_req_per_sec=None,
    avg_latency_ms=None,
    retry_after=None,
)


def _burst(size: int, throttled: int) -> phases.BurstRow:
    return phases.BurstRow(
        burst_size=size,
        ok_200=size - throttled,
        throttled_429=throttled,
        other=0,
        wall_seconds=0.1,
        launch_spread_ms=1.0,
        max_retry_after=None,
    )


def test_sequential_trips_429(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    # bucket of 10, slow refill -> the 11th back-to-back request throttles
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0, capacity=10))
    summary, _ = phases.phase_seq(make_probe(fake_endpoint, core.Budget(1000)), cap=50)
    assert summary.successful_before_429 == 10
    assert summary.first_429_at_request == 11


def test_sequential_no_429_when_limit_high(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.001, capacity=10000))
    summary, _ = phases.phase_seq(make_probe(fake_endpoint, core.Budget(1000)), cap=30)
    assert summary.first_429_at_request is None
    assert summary.successful_before_429 == 30


def test_sweep_finds_floor(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    # 1 token per 0.05s, capacity 30. drain (cap 500) empties it; floor should be 0.05.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.05, capacity=30))
    floor, rows = phases.phase_sweep(
        make_probe(fake_endpoint, core.Budget(5000)),
        cursor_pool=["a", "b", "c"],
        config=phases.SweepConfig(
            intervals=(0.2, 0.1, 0.05, 0.03),
            probe_count=12,
            drain_cap=500,
            tolerance=0.1,
        ),
    )
    assert floor == 0.05  # 0.03 throttles from empty, 0.05 is the fastest clean one
    assert rows[-1].clean is False
    assert all(r.bucket_emptied for r in rows)


def test_sweep_aborts_when_undrainable(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    # capacity 200 but drain cap only 50 -> can't empty -> must abort with NO floor.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0 / 200, capacity=200))
    floor, rows = phases.phase_sweep(
        make_probe(fake_endpoint, core.Budget(5000)),
        cursor_pool=["a", "b"],
        config=phases.SweepConfig(
            intervals=(2, 1, 0.5),
            probe_count=10,
            drain_cap=50,
            tolerance=0.1,
        ),
    )
    assert floor is None
    assert rows == []  # aborts on the first (undrainable) interval


def test_sweep_aborts_when_drain_unconfirmed(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    # capacity 10, drain cap 12 -> the bucket does empty, but drain ends on only 2
    # consecutive throttles, never the 3-in-a-row that CONFIRMS empty. A lone/paired
    # 429 could be transient, so drain conservatively reports "not emptied" and the
    # sweep aborts with no floor rather than measure from an unconfirmed-empty bucket.
    # Guards drain-confirmation semantics: fails against a `consecutive > 0` fallthrough
    # (which would call this emptied and report a floor). The sibling undrainable test
    # reaches the fallthrough at consecutive==0, so only this one exercises the 1-2 case.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0, capacity=10))
    floor, rows = phases.phase_sweep(
        make_probe(fake_endpoint, core.Budget(5000)),
        cursor_pool=["a"],
        config=phases.SweepConfig(
            intervals=(0.1,),
            probe_count=5,
            drain_cap=12,
            tolerance=0.1,
        ),
    )
    assert floor is None
    assert rows == []


def test_sweep_respects_budget(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.05, capacity=30))
    b = core.Budget(60)  # too small for even one drain+probe at cap 500
    phases.phase_sweep(
        make_probe(fake_endpoint, b),
        cursor_pool=["a"],
        config=phases.SweepConfig(
            intervals=(0.2, 0.1),
            probe_count=12,
            drain_cap=500,
            tolerance=0.1,
        ),
    )
    assert b.used <= 60  # never exceeds the budget


def test_summarise_burst_counts():
    batch = [core.Result(200, 0.01) for _ in range(7)] + [
        core.Result(429, 0.01, retry_after=5.0) for _ in range(3)
    ]
    row = burst._summarise_burst(burst._BurstOutcome(batch, elapsed_s=0.2, spread_ms=4.0))
    assert row.ok_200 == 7
    assert row.throttled_429 == 3
    assert row.max_retry_after == 5.0
    # window decision (Retry-After vs adaptive recovery) lives at the async call
    # site now, so it's exercised in test_burst.py, not here.


def test_estimate_prefers_headers():
    rl = Provider().parse_rate_limit(
        {
            "x-ratelimit-limit": "420, 420;w=60",
            "x-ratelimit-remaining": "1",
            "x-ratelimit-reset": "2",
        }
    )
    est = phases.phase_estimate(
        FakeEndpoint(total=1_470_000, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=rl,
            seq_summary=_NO_SEQ,
            swept_interval=0.6,  # present, but headers should win
        ),
        margin=0.8,
    )
    assert est["header_limit"] == 420
    assert est["safe_rate_basis"].startswith("AUTHORITATIVE")
    # 420/60s even-paced at 0.1429s, /0.8 margin => ~0.179s; 14700 pages -> ~44 min
    assert est["recommended_interval_s"] == pytest.approx(0.1786, abs=1e-3)
    assert est["total_pages"] == 14700
    assert est["estimated_minutes"] == pytest.approx(43.7, abs=0.5)


def test_estimate_falls_back_to_sequential_throttle():
    est = phases.phase_estimate(
        FakeEndpoint(total=None, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=None,
            seq_summary=replace(
                _NO_SEQ, successful_before_429=10, first_429_at_request=11, wall_seconds=2.0
            ),
        ),
        margin=0.8,
    )
    assert est["safe_rate_basis"] == "sequential 10 req / 2.0s"
    assert est["safe_rate_per_min"] == pytest.approx(240.0)  # 10 / 2.0s * 60 * 0.8


def test_estimate_falls_back_to_sweep():
    est = phases.phase_estimate(
        FakeEndpoint(total=500_000, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=None,
            seq_summary=_NO_SEQ,
            swept_interval=0.05,
        ),
        margin=0.8,
    )
    assert est["header_limit"] is None
    assert "measured floor" in est["safe_rate_basis"]
    assert est["recommended_interval_s"] == pytest.approx(0.0625, abs=1e-4)


def test_estimate_rate_only_without_total():
    est = phases.phase_estimate(
        FakeEndpoint(total=None, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=None,
            seq_summary=_NO_SEQ,
            swept_interval=0.05,
        ),
        margin=0.8,
    )
    assert est["total_pages"] is None
    assert est["estimated_minutes"] is None
    assert est["safe_rate_per_min"] is not None  # rate still reported


def test_estimate_zero_total_reports_zero_pages():
    # A caller-supplied total of 0 is a KNOWN total, distinct from None (unknown):
    # phase_estimate reports 0 pages / ~0 min, not "rate only". (The natural empty-
    # resource CLI path instead yields page_count=0 and takes the rate-only branch;
    # this unit test pins the total==0 contract directly.)
    est = phases.phase_estimate(
        FakeEndpoint(total=0, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=None,
            seq_summary=_NO_SEQ,
            swept_interval=0.05,
        ),
        margin=0.8,
    )
    assert est["total_pages"] == 0
    assert est["estimated_minutes"] == 0.0


def test_estimate_infers_from_token_bucket():
    """No authoritative headers and no swept floor, but a fully-OK burst plus a
    measured window -> token-bucket inference: (bucket / window) * 60 * margin."""
    est = phases.phase_estimate(
        FakeEndpoint(total=None, page_size=100),
        phases.Measurements(
            page_count=100,
            rate_limit=None,
            seq_summary=_NO_SEQ,
            burst_results=[_burst(10, 0), _burst(20, 3)],  # the throttled one isn't the bucket
            measured_window=12.0,
        ),
        margin=0.8,
    )
    assert est["safe_rate_basis"].startswith("INFERRED")
    assert est["measured_window_seconds"] == 12.0
    assert est["safe_rate_per_min"] == pytest.approx(40.0, abs=1e-6)  # 10/12 * 60 * 0.8


def test_estimate_no_throttle_fallback_scales_with_margin():
    """No-throttle fallback: nothing throttled -> no ceiling, so 0.5 * margin of measured
    sequential throughput. --margin scales it (the most conservative source)."""

    def est(margin: float) -> dict[str, Any]:
        return phases.phase_estimate(
            FakeEndpoint(total=None, page_size=100),
            phases.Measurements(
                page_count=100,
                rate_limit=None,
                # no first_429: the no-throttle fallback
                seq_summary=replace(_NO_SEQ, seq_req_per_sec=10.0),
                burst_results=[],
                measured_window=None,
                swept_interval=None,
            ),
            margin=margin,
        )

    e = est(0.8)
    assert e["safe_rate_basis"] == "no 429 observed; 40% of measured sequential throughput"
    assert e["safe_rate_per_min"] == pytest.approx(240.0)  # 10 * 60 * (0.5 * 0.8)
    assert est(0.5)["safe_rate_per_min"] == pytest.approx(150.0)  # 10 * 60 * (0.5 * 0.5)


def test_recovery_steps_geometric_backoff():
    """_recovery_steps is a pure state machine; assert its backoff schedule,
    cursor round-robin, and max_wait termination directly."""
    steps = list(burst._recovery_steps(0.25, 5.0, 10, ["a", "b"]))
    # cumulative wait (3rd tuple element) grows 0.25, then *1.6 each poll
    waits = [w for _, _, w in steps]
    assert waits == pytest.approx([0.25, 0.65, 1.29, 2.314, 3.9524, 6.57384], abs=1e-4)
    # stops once cumulative >= max_wait (5.0): 6 polls, below max_polls=10
    assert len(steps) == 6
    # cursor round-robins over the pool
    assert [c for _, c, _ in steps] == ["a", "b", "a", "b", "a", "b"]
    # per-poll step grows by the 1.6 factor
    sizes = [s for s, _, _ in steps]
    assert sizes[1] == pytest.approx(sizes[0] * 1.6)


def test_cursors_without_a_pool_yield_none():
    cursors = probe.cursor_cycle([])
    assert [next(cursors) for _ in range(3)] == [None, None, None]


def test_drain_stops_when_the_budget_runs_out(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.001, capacity=10000))
    used, emptied = sweep._drain(
        make_probe(fake_endpoint, core.Budget(2)), probe.cursor_cycle([]), cap=50
    )
    assert (used, emptied) == (2, False)  # the refused third request isn't counted


def test_paced_stops_when_the_budget_runs_out(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, fake_endpoint: FakeEndpoint
):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.001, capacity=10000))
    paced = sweep._paced(
        make_probe(fake_endpoint, core.Budget(3)), probe.cursor_cycle([]), interval=0.1, count=10
    )
    assert (paced.sent, paced.throttled) == (3, 0)


def test_afetch_reports_a_network_error(
    fake_endpoint: FakeEndpoint, burst_transport: Callable[[Handler], None]
):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    burst_transport(refuse)

    async def go() -> core.Result:
        async with httpx.AsyncClient() as client:
            return await burst._afetch(make_probe(fake_endpoint, core.Budget(1)), client, None)

    r = asyncio.run(go())
    assert (r.status, r.rclass) == (0, core.RClass.ERROR)
    assert r.error is not None
    assert "refused" in r.error


def test_afetch_scrubs_a_network_error(
    fake_endpoint: FakeEndpoint, burst_transport: Callable[[Handler], None]
):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused: key=SECRETTOKENVALUE", request=request)

    burst_transport(refuse)

    async def go() -> core.Result:
        async with httpx.AsyncClient() as client:
            return await burst._afetch(make_probe(fake_endpoint, core.Budget(1)), client, None)

    logconfig._SECRETS.clear()
    register_log_secrets(["SECRETTOKENVALUE"])
    try:
        r = asyncio.run(go())
    finally:
        logconfig._SECRETS.clear()
    assert r.error is not None
    assert "SECRETTOKENVALUE" not in r.error

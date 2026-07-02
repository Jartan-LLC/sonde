"""Tests for the probing phases: sequential, sweep (floor + abort), burst summary,
and the estimate's rate-source priority. Uses the virtual `clock` so pacing/sleeps
resolve instantly and deterministically."""

import pytest

from sonde import core, phases
from tests.helpers import FakeEndpoint, make_bucket


# --------------------------------------------------------------------------- #
# Sequential
# --------------------------------------------------------------------------- #
def test_sequential_trips_429(clock, monkeypatch, fake_endpoint):
    # bucket of 10, slow refill -> the 11th back-to-back request throttles
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0, capacity=10))
    summary, pool = phases.phase_seq(None, fake_endpoint, core.Budget(1000), cap=50)
    assert summary["successful_before_429"] == 10
    assert summary["first_429_at_request"] == 11


def test_sequential_no_429_when_limit_high(clock, monkeypatch, fake_endpoint):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.001, capacity=10000))
    summary, pool = phases.phase_seq(None, fake_endpoint, core.Budget(1000), cap=30)
    assert summary["first_429_at_request"] is None
    assert summary["successful_before_429"] == 30


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #
def test_sweep_finds_floor(clock, monkeypatch, fake_endpoint):
    # 1 token per 0.05s, capacity 30. drain (cap 500) empties it; floor should be 0.05.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.05, capacity=30))
    floor, rows = phases.phase_sweep(
        None,
        fake_endpoint,
        core.Budget(5000),
        cursor_pool=["a", "b", "c"],
        intervals=[0.2, 0.1, 0.05, 0.03],
        probe_count=12,
        drain_cap=500,
        tolerance=0.1,
    )
    assert floor == 0.05  # 0.03 throttles from empty, 0.05 is the fastest clean one
    assert rows[-1]["clean"] is False
    assert all(r["bucket_emptied"] for r in rows)


def test_sweep_aborts_when_undrainable(clock, monkeypatch, fake_endpoint):
    # capacity 200 but drain cap only 50 -> can't empty -> must abort with NO floor.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0 / 200, capacity=200))
    floor, rows = phases.phase_sweep(
        None,
        fake_endpoint,
        core.Budget(5000),
        cursor_pool=["a", "b"],
        intervals=[2, 1, 0.5],
        probe_count=10,
        drain_cap=50,
        tolerance=0.1,
    )
    assert floor is None
    assert rows == []  # aborts on the first (undrainable) interval


def test_sweep_aborts_when_drain_unconfirmed(clock, monkeypatch, fake_endpoint):
    # capacity 10, drain cap 12 -> the bucket does empty, but drain ends on only 2
    # consecutive throttles, never the 3-in-a-row that CONFIRMS empty. A lone/paired
    # 429 could be transient, so drain conservatively reports "not emptied" and the
    # sweep aborts with no floor rather than measure from an unconfirmed-empty bucket.
    # Guards drain-confirmation semantics: fails against a `consecutive > 0` fallthrough
    # (which would call this emptied and report a floor). The sibling undrainable test
    # reaches the fallthrough at consecutive==0, so only this one exercises the 1-2 case.
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=60.0, capacity=10))
    floor, rows = phases.phase_sweep(
        None,
        fake_endpoint,
        core.Budget(5000),
        cursor_pool=["a"],
        intervals=[0.1],
        probe_count=5,
        drain_cap=12,
        tolerance=0.1,
    )
    assert floor is None
    assert rows == []


def test_sweep_respects_budget(clock, monkeypatch, fake_endpoint):
    monkeypatch.setattr(core, "fetch", make_bucket(refill_period=0.05, capacity=30))
    b = core.Budget(60)  # too small for even one drain+probe at cap 500
    floor, rows = phases.phase_sweep(
        None,
        fake_endpoint,
        b,
        cursor_pool=["a"],
        intervals=[0.2, 0.1],
        probe_count=12,
        drain_cap=500,
        tolerance=0.1,
    )
    assert b.used <= 60  # never exceeds the budget


# --------------------------------------------------------------------------- #
# Burst summary helper
# --------------------------------------------------------------------------- #
def test_summarise_burst_counts():
    batch = [core.Result(200, 0.01) for _ in range(7)] + [
        core.Result(429, 0.01, retry_after=5.0) for _ in range(3)
    ]
    mw, row = phases._summarise_burst(
        10, batch, elapsed=0.2, spread_ms=4.0, measured_window=None, recovery_cb=lambda: 12.0
    )
    assert row["ok_200"] == 7
    assert row["throttled_429"] == 3
    assert row["max_retry_after"] == 5.0
    # first throttled burst with a Retry-After present -> window taken from it
    assert mw == 5.0


def test_summarise_burst_recovery_callback():
    batch = [core.Result(429, 0.01) for _ in range(5)]  # no Retry-After
    called = {"n": 0}

    def cb():
        called["n"] += 1
        return 8.0

    mw, row = phases._summarise_burst(5, batch, 0.1, 2.0, measured_window=None, recovery_cb=cb)
    assert called["n"] == 1  # recovery measured because no Retry-After
    assert mw == 8.0


# --------------------------------------------------------------------------- #
# Estimate — rate-source priority
# --------------------------------------------------------------------------- #
def test_estimate_prefers_headers():
    from sonde.provider import Provider

    rl = Provider().parse_rate_limit(
        {
            "x-ratelimit-limit": "420, 420;w=60",
            "x-ratelimit-remaining": "1",
            "x-ratelimit-reset": "2",
        }
    )
    est = phases.phase_estimate(
        FakeEndpoint(total=1_470_000, page_size=100),
        page_count=100,
        seq_summary={},
        burst_results=[],
        measured_window=None,
        swept_interval=0.6,  # present, but headers should win
        margin=0.8,
        rl=rl,
    )
    assert est["header_limit"] == 420
    assert est["safe_rate_basis"].startswith("AUTHORITATIVE")
    # 420/60s even-paced at 0.1429s, /0.8 margin => ~0.179s; 14700 pages -> ~44 min
    assert est["recommended_interval_s"] == pytest.approx(0.1786, abs=1e-3)
    assert est["total_pages"] == 14700
    assert est["estimated_minutes"] == pytest.approx(43.7, abs=0.5)


def test_estimate_falls_back_to_sweep():
    est = phases.phase_estimate(
        FakeEndpoint(total=500_000, page_size=100),
        page_count=100,
        seq_summary={},
        burst_results=[],
        measured_window=None,
        swept_interval=0.05,
        margin=0.8,
        rl={},
    )
    assert est["header_limit"] is None
    assert "measured floor" in est["safe_rate_basis"]
    assert est["recommended_interval_s"] == pytest.approx(0.0625, abs=1e-4)


def test_estimate_rate_only_without_total():
    est = phases.phase_estimate(
        FakeEndpoint(total=None, page_size=100),
        page_count=100,
        seq_summary={},
        burst_results=[],
        measured_window=None,
        swept_interval=0.05,
        margin=0.8,
        rl={},
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
        page_count=100,
        seq_summary={},
        burst_results=[],
        measured_window=None,
        swept_interval=0.05,
        margin=0.8,
        rl={},
    )
    assert est["total_pages"] == 0
    assert est["estimated_minutes"] == 0.0


# --------------------------------------------------------------------------- #
# Estimate — token-bucket inference (Priority 2)
# --------------------------------------------------------------------------- #
def test_estimate_infers_from_token_bucket():
    """No authoritative headers and no swept floor, but a fully-OK burst plus a
    measured window -> Priority-2 inference: (bucket / window) * 60 * margin."""
    est = phases.phase_estimate(
        FakeEndpoint(total=None, page_size=100),
        page_count=100,
        seq_summary={},
        burst_results=[
            {"burst_size": 10, "throttled_429": 0},
            {"burst_size": 20, "throttled_429": 3},  # throttled -> excluded from bucket
        ],
        measured_window=12.0,
        swept_interval=None,
        margin=0.8,
        rl={},
    )
    assert est["safe_rate_basis"].startswith("INFERRED")
    assert est["measured_window_seconds"] == 12.0
    assert est["safe_rate_per_min"] == pytest.approx(40.0, abs=1e-6)  # 10/12 * 60 * 0.8


# --------------------------------------------------------------------------- #
# Recovery probe — geometric backoff generator + measured return value
# --------------------------------------------------------------------------- #
def test_recovery_steps_geometric_backoff():
    """_recovery_steps is a pure state machine; assert its backoff schedule,
    cursor round-robin, and max_wait termination directly."""
    steps = list(phases._recovery_steps(0.25, 5.0, 10, ["a", "b"]))
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


def test_measure_recovery_returns_cumulative_wait(clock, monkeypatch, fake_endpoint):
    """measure_recovery returns the cumulative wait at the first success (429 for
    the first two polls, OK on the third)."""
    calls = {"n": 0}

    def fake_fetch(session, ep, cursor, budget):
        budget.take()
        calls["n"] += 1
        ok = calls["n"] >= 3
        return core.Result(status=200 if ok else 429, elapsed=0.0, count=100 if ok else 0)

    monkeypatch.setattr(core, "fetch", fake_fetch)
    waited = phases.measure_recovery(
        None,
        fake_endpoint,
        core.Budget(100),
        cursor_pool=["a", "b"],
        start_step=0.25,
        max_wait=90.0,
        max_polls=15,
    )
    assert waited == pytest.approx(1.29, abs=1e-9)  # 0.25 + 0.4 + 0.64

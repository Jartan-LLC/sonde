"""Tests for the async httpx burst phase, driven through `httpx.MockTransport`
(the `burst_transport` fixture swaps in the handler). `no_wait` makes asyncio's
cooldown/recovery sleeps instant."""

import asyncio

import pytest

from sonde import core, phases
from tests.helpers import FakeEndpoint, make_burst_handler


@pytest.fixture
def no_wait(monkeypatch):
    """Make asyncio.sleep a no-op so cooldowns/recovery don't actually wait."""

    async def _fast(*a, **k):
        return

    monkeypatch.setattr(asyncio, "sleep", _fast)


def test_burst_all_success(no_wait, burst_transport):
    burst_transport(make_burst_handler(decider=lambda: True))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(1000),
        sizes=[10, 20],
        cooldown=0,
        cursor_pool=["c1", "c2"],
        recovery_step=0.1,
        recovery_max=1,
        recovery_polls=3,
    )
    assert [r["burst_size"] for r in results] == [10, 20]
    assert all(r["ok_200"] == r["burst_size"] for r in results)
    assert all(r["throttled_429"] == 0 for r in results)
    assert mw is None  # nothing throttled -> no window measured
    assert all("launch_spread_ms" in r for r in results)


def test_burst_counts_throttles(no_wait, burst_transport):
    # succeed for the first 5 calls only, then throttle -> a burst of 10 splits 5/5
    state = {"n": 0}

    def decider():
        state["n"] += 1
        return state["n"] <= 5

    burst_transport(make_burst_handler(decider=decider))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(1000),
        sizes=[10],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=1,
        recovery_polls=2,
    )
    assert results[0]["ok_200"] == 5
    assert results[0]["throttled_429"] == 5


def test_burst_uses_retry_after_as_window(no_wait, burst_transport):
    # every request 429s WITH Retry-After -> window is taken from the header and the
    # adaptive recovery poll is skipped entirely.
    burst_transport(make_burst_handler(decider=lambda: False, retry_after=7))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(1000),
        sizes=[10],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=90,
        recovery_polls=5,
    )
    assert results[0]["throttled_429"] == 10
    assert mw == 7.0


def test_burst_measures_recovery_window(no_wait, burst_transport):
    # whole first burst 429s with NO Retry-After -> recovery() polls with adaptive
    # backoff until a success, and returns the cumulative wait as the window.
    state = {"n": 0}

    def decider():
        state["n"] += 1
        return state["n"] > 12  # burst (10) + first 2 recovery polls throttle, 3rd OK

    burst_transport(make_burst_handler(decider=decider))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(1000),
        sizes=[10],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=90,
        recovery_polls=5,
    )
    assert results[0]["throttled_429"] == 10
    assert mw == pytest.approx(0.516, abs=1e-6)  # cumulative wait at 3rd recovery poll

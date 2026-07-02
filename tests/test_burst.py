"""Tests for the async httpx burst phase, driven through `httpx.MockTransport`
(the `burst_transport` fixture swaps in the handler). `no_wait` makes asyncio's
cooldown/recovery sleeps instant."""

import asyncio

import httpx
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


def test_burst_skips_when_budget_below_size(no_wait, burst_transport):
    # budget (5) < the first burst size (10) -> the burst is skipped entirely and
    # nothing is measured.
    burst_transport(make_burst_handler(decider=lambda: True))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(5),
        sizes=[10],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=1,
        recovery_polls=2,
    )
    assert results == []
    assert mw is None


def test_burst_budget_exhausted_during_recovery(no_wait, burst_transport):
    # the burst (10) 429s with no Retry-After and consumes the whole budget; the first
    # recovery afetch then finds an empty budget and bails BEFORE calling the handler.
    calls = {"n": 0}

    def decider():
        calls["n"] += 1
        return False

    burst_transport(make_burst_handler(decider=decider))
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(10),
        sizes=[10],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=90,
        recovery_polls=5,
    )
    assert results[0]["throttled_429"] == 10
    assert mw is None  # recovery bailed on the exhausted budget -> no window
    assert calls["n"] == 10  # recovery afetch stopped at budget.take, before the handler


def test_burst_measures_window_once_across_bursts(no_wait, burst_transport):
    # both bursts 429; the first carries Retry-After 7, the second 99. The window is
    # fixed on the FIRST throttled burst and the dedup guard blocks the overwrite.
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        ra = 7 if state["n"] <= 10 else 99  # first burst is 10 requests, then 20
        return httpx.Response(429, headers={"Retry-After": str(ra)})

    burst_transport(handler)
    results, mw = phases.phase_burst(
        headers={},
        endpoint=FakeEndpoint(),
        budget=core.Budget(1000),
        sizes=[10, 20],
        cooldown=0,
        cursor_pool=["c1"],
        recovery_step=0.1,
        recovery_max=90,
        recovery_polls=5,
    )
    assert results[0]["throttled_429"] == 10
    assert results[1]["throttled_429"] == 20
    assert results[1]["max_retry_after"] == 99.0  # second burst really saw the larger value
    assert mw == 7.0  # ...but the measured window stays the first burst's, not overwritten

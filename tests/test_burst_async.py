"""Tests for the async httpx burst path, using a fake `httpx` module (real httpx
isn't installed). Also confirms the ImportError surfaces when httpx is absent so the
CLI's fallback can catch it."""

import asyncio
import sys

import pytest

from sonde import core, phases
from tests.helpers import FakeEndpoint, make_fake_httpx


@pytest.fixture
def no_wait(monkeypatch):
    """Make asyncio.sleep a no-op so cooldowns/recovery don't actually wait."""

    async def _fast(*a, **k):
        return

    monkeypatch.setattr(asyncio, "sleep", _fast)


def test_async_burst_all_success(no_wait, monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", make_fake_httpx(decider=lambda: True))
    results, mw = phases.phase_burst_async(
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


def test_async_burst_counts_throttles(no_wait, monkeypatch):
    # succeed for the first 5 calls only, then throttle -> a burst of 10 splits 5/5
    state = {"n": 0}

    def decider():
        state["n"] += 1
        return state["n"] <= 5

    monkeypatch.setitem(sys.modules, "httpx", make_fake_httpx(decider=decider))
    results, mw = phases.phase_burst_async(
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


def test_async_burst_raises_without_httpx(monkeypatch):
    # ensure httpx is not importable, so phase_burst_async raises ImportError
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ImportError):
        phases.phase_burst_async(
            headers={},
            endpoint=FakeEndpoint(),
            budget=core.Budget(10),
            sizes=[5],
            cooldown=0,
            cursor_pool=[],
            recovery_step=0.1,
            recovery_max=1,
            recovery_polls=2,
        )

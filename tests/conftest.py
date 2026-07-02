"""pytest fixtures. Shared fakes live in tests/helpers.py."""

import time

import pytest

from tests.helpers import FakeClock, FakeEndpoint, make_bucket


@pytest.fixture
def clock(monkeypatch):
    """Patch time.perf_counter/sleep with a virtual clock -> instant, deterministic
    timing for the token-bucket sims and the sweep's pacing."""
    c = FakeClock()
    monkeypatch.setattr(time, "perf_counter", c.perf_counter)
    monkeypatch.setattr(time, "sleep", c.sleep)
    return c


@pytest.fixture
def bucket_factory():
    return make_bucket


@pytest.fixture
def fake_endpoint():
    return FakeEndpoint(total=500_000, page_size=100)

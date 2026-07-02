"""pytest fixtures. Shared fakes live in tests/helpers.py."""

import logging
import time

import httpx
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


@pytest.fixture(autouse=True)
def burst_transport(monkeypatch):
    """Route the async burst's httpx.AsyncClient through an httpx.MockTransport so no
    test ever hits the network. Autouse with an all-200 default (harmless for tests
    that never build a client); return value is a setter to swap in a custom handler
    (see tests/helpers.make_burst_handler)."""
    state = {
        "handler": lambda request: httpx.Response(
            200, json={"data": [0] * 100, "nextPageCursor": "c"}
        )
    }
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("limits", None)  # MockTransport ignores pool limits
        kwargs["transport"] = httpx.MockTransport(lambda req: state["handler"](req))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    def set_handler(handler):
        state["handler"] = handler

    return set_handler


@pytest.fixture
def fake_endpoint():
    return FakeEndpoint(total=500_000, page_size=100)


@pytest.fixture()
def restore_root_logger():
    """Save and restore root logger state so setup_logging tests don't leak."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    yield
    for h in root.handlers:
        if h not in old_handlers:
            h.close()
    root.handlers = old_handlers
    root.level = old_level

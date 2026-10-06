"""pytest fixtures. Shared fakes live in tests/helpers.py."""

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from tests.helpers import FakeClock, FakeEndpoint, Handler


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch time.perf_counter/sleep with a virtual clock -> instant, deterministic
    timing for the token-bucket sims and the sweep's pacing."""
    c = FakeClock()
    monkeypatch.setattr(time, "perf_counter", c.perf_counter)
    monkeypatch.setattr(time, "sleep", c.sleep)
    return c


@pytest.fixture(autouse=True)
def burst_transport(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Route the async burst's httpx.AsyncClient through an httpx.MockTransport so no
    test ever hits the network. Autouse with an all-200 default (harmless for tests
    that never build a client); return value is a setter to swap in a custom handler
    (see tests/helpers.make_burst_handler)."""

    def default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [0] * 100, "nextPageCursor": "c"})

    current: Handler = default
    real_client = httpx.AsyncClient

    def dispatch(request: httpx.Request) -> httpx.Response:
        return current(request)  # read per request, so set_handler takes effect

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("limits", None)  # MockTransport ignores pool limits
        kwargs["transport"] = httpx.MockTransport(dispatch)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    def set_handler(handler: Handler) -> None:
        nonlocal current
        current = handler

    return set_handler


@pytest.fixture
def fake_endpoint() -> FakeEndpoint:
    return FakeEndpoint(total=500_000, page_size=100)


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
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

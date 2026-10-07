"""Shared fakes/helpers for the test suite (imported by conftest and test modules)."""

import threading
import time
from collections.abc import Callable
from typing import Any, override

import httpx
import requests
from requests.structures import CaseInsensitiveDict

from sonde import core, endpoint, phases
from sonde.endpoint import PageResult, RequestSpec

type Fetch = Callable[[Any, endpoint.Endpoint, Any, core.Budget], core.Result]
type Handler = Callable[[httpx.Request], httpx.Response]

# Real captured headers from the two runs in this project.
RLH_420 = {
    "server": "public-gateway",
    "x-ratelimit-limit": "420, 420;w=60, 420;w=60, 70000",
    "x-ratelimit-remaining": "419, 70000",
    "x-ratelimit-reset": "2, 0",
}
RLH_15 = {
    "server": "public-gateway",
    "x-ratelimit-limit": "15, 15;w=60, 70000",
    "x-ratelimit-remaining": "14, 70000",
    "x-ratelimit-reset": "21, 0",
}


class FakeClock:
    """Virtual clock so token-bucket sims and sweep pacing resolve instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def perf_counter(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, dt: float) -> None:
        with self._lock:
            self._t += max(0.0, float(dt))


def make_bucket(
    refill_period: float, capacity: int, headers: dict[str, str] | None = None
) -> Fetch:
    """A core.fetch-compatible token-bucket simulator driven by time.perf_counter
    (i.e. the virtual clock when patched). Emits `headers` on every response."""
    tokens = float(capacity)
    last: float | None = None
    lock = threading.Lock()

    def f(session: Any, ep: endpoint.Endpoint, cursor: Any, budget: core.Budget) -> core.Result:
        nonlocal tokens, last
        if not budget.take():
            return core.Result(status=-1, elapsed=0.0, error="budget exhausted")
        with lock:
            now = time.perf_counter()
            if last is None:
                last = now
            tokens = min(capacity, tokens + (now - last) / refill_period)
            last = now
            ok = tokens >= 1
            if ok:
                tokens -= 1
        page = getattr(ep, "page_size", 100)
        r = core.Result(
            status=200 if ok else 429,
            elapsed=0.0,
            count=page if ok else 0,
            next_cursor=f"cur{int(tokens)}" if ok else None,
        )
        if headers:
            r.headers = dict(headers)
        return r

    return f


class FakeResp:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        body: Any = None,
        text: str = "err-body",
    ) -> None:
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self._body = body
        self._text = text

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body

    @property
    def content(self) -> bytes:
        return self._text.encode()


class FakeEndpoint(endpoint.Endpoint):
    """Not registered — a bare endpoint for exercising the phases in isolation."""

    name = "fake-test"
    help = "fake endpoint for tests"

    def __init__(self, total: int | None = None, page_size: int = 100) -> None:
        self._total = total
        self.page_size = page_size

    @override
    def build_request(self, cursor: Any) -> RequestSpec:
        params: dict[str, Any] = {"limit": self.page_size}
        if cursor:
            params["cursor"] = cursor
        return RequestSpec(url="https://example.test/probe", params=params)

    @override
    def parse_page(self, response: Any) -> PageResult:
        body = response.json()
        return PageResult(count=len(body.get("data", ())), next_cursor=body.get("nextPageCursor"))

    @override
    def total_items(self) -> int | None:
        return self._total


def make_burst_handler(decider: Callable[[], bool], retry_after: float | None = None) -> Handler:
    """Build an `httpx.MockTransport` handler for the async burst phase. Each request
    returns 200 or 429 per `decider()`; 429s carry a `Retry-After` header when
    `retry_after` is given."""

    def handler(request: httpx.Request) -> httpx.Response:
        if decider():
            return httpx.Response(200, json={"data": [0] * 100, "nextPageCursor": "c"})
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return httpx.Response(429, headers=headers)

    return handler


def make_probe(ep: endpoint.Endpoint, budget: core.Budget) -> phases.Probe:
    """A Probe for tests that patch core.fetch or the httpx transport, so its session is
    never used."""
    return phases.Probe(endpoint=ep, budget=budget, session=requests.Session(), headers={})

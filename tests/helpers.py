"""Shared fakes/helpers for the test suite (imported by conftest and test modules)."""

import threading
import time

import httpx

from sonde import core, endpoint
from sonde.endpoint import PageResult, RequestSpec

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

    def __init__(self, start=1000.0):
        self._t = start
        self._lock = threading.Lock()

    def perf_counter(self):
        with self._lock:
            return self._t

    def sleep(self, dt):
        with self._lock:
            self._t += max(0.0, float(dt))


def make_bucket(refill_period, capacity, headers=None):
    """A core.fetch-compatible token-bucket simulator driven by time.perf_counter
    (i.e. the virtual clock when patched). Emits `headers` on every response."""
    st = {"tok": float(capacity), "last": None, "lock": threading.Lock()}

    def f(session, ep, cursor, budget):
        if not budget.take():
            return core.Result(status=-1, elapsed=0.0, error="budget exhausted")
        with st["lock"]:
            now = time.perf_counter()
            if st["last"] is None:
                st["last"] = now
            st["tok"] = min(capacity, st["tok"] + (now - st["last"]) / refill_period)
            st["last"] = now
            ok = st["tok"] >= 1
            if ok:
                st["tok"] -= 1
        page = getattr(ep, "page_size", 100)
        r = core.Result(
            status=200 if ok else 429,
            elapsed=0.0,
            count=page if ok else 0,
            next_cursor=("cur%d" % int(st["tok"])) if ok else None,
        )
        if headers:
            r.headers = dict(headers)
        return r

    return f


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == str(key).lower():
                return v
        return default


class FakeResp:
    def __init__(self, status_code, headers=None, body=None, text="err-body"):
        self.status_code = status_code
        self.headers = FakeHeaders(headers or {})
        self._body = body
        self._text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    @property
    def text(self):
        return self._text


class FakeEndpoint(endpoint.Endpoint):
    """Not registered — a bare endpoint for exercising the phases in isolation."""

    name = "fake-test"
    help = "fake endpoint for tests"

    def __init__(self, total=None, page_size=100):
        self._total = total
        self.page_size = page_size

    def build_request(self, cursor):
        params = {"limit": self.page_size}
        if cursor:
            params["cursor"] = cursor
        return RequestSpec(url="https://example.test/probe", params=params)

    def parse_page(self, response):
        body = response.json()
        return PageResult(count=len(body.get("data", [])), next_cursor=body.get("nextPageCursor"))

    def total_items(self):
        return self._total


def make_burst_handler(decider, retry_after=None):
    """Build an `httpx.MockTransport` handler for the async burst phase. Each request
    returns 200 or 429 per `decider()` (a zero-arg callable -> bool); 429s carry a
    `Retry-After` header when `retry_after` is given."""

    def handler(request):
        if decider():
            return httpx.Response(200, json={"data": [0] * 100, "nextPageCursor": "c"})
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return httpx.Response(429, headers=headers)

    return handler

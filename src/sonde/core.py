"""
core.py — endpoint- and provider-agnostic HTTP plumbing.

Response classification, rate-limit-header parsing, and auth are NOT here — those
vary per API and live behind the Provider interface (provider.py). core only knows
how to issue a request, time it, and hand the response to the endpoint's provider
for classification and to the endpoint for item extraction.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http.cookiejar import DefaultCookiePolicy
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from . import __version__

BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": f"sonde/{__version__} (one-time diagnostic)",
}

# Response headers worth surfacing (case-insensitive substring match).
HEADER_SUBSTRINGS = ("ratelimit", "retry-after", "x-request", "server", "cf-ray")


# --------------------------------------------------------------------------- #
# Normalised response class — phases branch on this, never on raw status.
# --------------------------------------------------------------------------- #
class RClass(str, Enum):
    OK = "ok"  # a usable success response
    THROTTLED = "throttled"  # rate-limited (429, or provider-specific)
    ERROR = "error"  # any other non-success (4xx/5xx/network)
    BUDGET = "budget"  # local request budget exhausted (not a server response)


def default_rclass(status: int) -> RClass:
    """Fallback classification (also what the generic Provider uses)."""
    if status == 200:
        return RClass.OK
    if status == 429:
        return RClass.THROTTLED
    if status == -1:
        return RClass.BUDGET
    return RClass.ERROR


# --------------------------------------------------------------------------- #
# Request budget: thread-safe hard ceiling.
# --------------------------------------------------------------------------- #
@dataclass
class Budget:
    max_requests: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.max_requests:
                return False
            self.used += 1
            return True

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_requests - self.used)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def build_session(max_conns: int = 10, headers: dict[str, str] | None = None) -> requests.Session:
    """Session with a pool sized to the largest burst and a no-write cookie jar
    (auth rides on headers, so the shared jar is never mutated -> thread-safe)."""
    s = requests.Session()
    s.headers.update(headers or dict(BASE_HEADERS))
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(max_conns, 10), max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# --------------------------------------------------------------------------- #
# Result + response handling
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    status: int
    elapsed: float
    rclass: RClass | None = None  # defaults from status via default_rclass()
    count: int = 0
    next_cursor: Any = None
    retry_after: float | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self):
        if self.rclass is None:
            self.rclass = default_rclass(self.status)


def interesting_headers(resp: Any) -> dict[str, str]:
    return {
        k: v for k, v in resp.headers.items() if any(sub in k.lower() for sub in HEADER_SUBSTRINGS)
    }


_PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)


def _parse_response(resp: Any, elapsed: float, endpoint: Any) -> Result:
    """Classify via the endpoint's provider, then (on success) let the endpoint pull
    item count + next cursor from the FULL response (so header-based pagination and
    non-JSON bodies are possible)."""
    provider = endpoint.provider()
    rclass = provider.classify(resp)

    ra = resp.headers.get("Retry-After")
    retry_after = None
    if ra is not None:
        try:
            retry_after = float(ra)
        except (ValueError, TypeError):
            retry_after = None

    res = Result(
        status=resp.status_code,
        elapsed=elapsed,
        rclass=rclass,
        retry_after=retry_after,
        headers=interesting_headers(resp),
    )
    if rclass == RClass.OK:
        try:
            page = endpoint.parse_page(resp)
            res.count = page.count
            res.next_cursor = page.next_cursor
        except _PARSE_ERRORS as e:
            res.error = f"OK response but parse_page failed: {e}"
    elif rclass == RClass.ERROR and resp.status_code >= 400:
        res.error = resp.text[:200]
    return res


def fetch(session: requests.Session, endpoint: Any, cursor: Any, budget: Budget) -> Result:
    """One probe request for `endpoint` at pagination position `cursor`."""
    if not budget.take():
        return Result(
            status=-1, elapsed=0.0, rclass=RClass.BUDGET, error="request budget exhausted"
        )

    provider = endpoint.provider()
    spec = endpoint.build_request(cursor)
    params = {**provider.auth_params(), **(spec.params or {})}

    t0 = time.perf_counter()
    try:
        resp = session.request(
            spec.method, spec.url, params=params, json=spec.json_body, timeout=30
        )
    except requests.RequestException as e:
        return Result(status=0, elapsed=time.perf_counter() - t0, rclass=RClass.ERROR, error=str(e))
    return _parse_response(resp, time.perf_counter() - t0, endpoint)

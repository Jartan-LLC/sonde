"""Endpoint- and provider-agnostic HTTP plumbing.

Response classification, rate-limit-header parsing, and auth are NOT here — those
vary per API and live behind the Provider interface (provider.py). core only knows
how to issue a request, time it, and hand the response to the endpoint's provider
for classification and to the endpoint for item extraction.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from http.cookiejar import DefaultCookiePolicy
from typing import TYPE_CHECKING, Any

import requests
from requests.adapters import HTTPAdapter

from sonde import __version__
from sonde.logconfig import scrub

if TYPE_CHECKING:
    from sonde.endpoint import Endpoint, RequestSpec

__all__ = [
    "BASE_HEADERS",
    "Budget",
    "RClass",
    "Result",
    "build_session",
    "default_rclass",
    "fetch",
    "interesting_headers",
    "parse_response",
    "request_args",
]

BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": f"sonde/{__version__} (one-time diagnostic)",
}

# Response headers worth surfacing (case-insensitive substring match).
HEADER_SUBSTRINGS = ("ratelimit", "retry-after", "x-request", "server", "cf-ray")


class RClass(StrEnum):
    """How a response counts for the phases, whatever its raw status."""

    OK = "ok"  # a usable success response
    THROTTLED = "throttled"  # rate-limited (429, or provider-specific)
    ERROR = "error"  # any other non-success (4xx/5xx/network)
    BUDGET = "budget"  # local request budget exhausted (not a server response)


def default_rclass(status: int) -> RClass:
    """Fallback classification (also what the generic Provider uses).

    Args:
        status: The HTTP status, or -1 for a request the budget refused.

    Returns:
        The response class.

    >>> default_rclass(200), default_rclass(429), default_rclass(503)
    (<RClass.OK: 'ok'>, <RClass.THROTTLED: 'throttled'>, <RClass.ERROR: 'error'>)
    """
    if status == HTTPStatus.OK:
        return RClass.OK
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return RClass.THROTTLED
    if status == -1:
        return RClass.BUDGET
    return RClass.ERROR


@dataclass
class Budget:
    """A thread-safe ceiling on the requests a run may send.

    >>> budget = Budget(max_requests=1)
    >>> budget.take(), budget.take(), budget.remaining()
    (True, False, 0)
    """

    max_requests: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self) -> bool:
        """Claim one request, or return False when none are left."""
        with self._lock:
            if self.used >= self.max_requests:
                return False
            self.used += 1
            return True

    def remaining(self) -> int:
        """Return how many requests are left."""
        with self._lock:
            return max(0, self.max_requests - self.used)


def build_session(headers: dict[str, str] | None = None) -> requests.Session:
    """Build the session the serial phases share: no retries, and no cookies kept.

    A server's Set-Cookie is refused, so every request carries the same credentials:
    the ones in the headers.

    Args:
        headers: Request headers; `BASE_HEADERS` when omitted.

    Returns:
        The session.
    """
    s = requests.Session()
    s.headers.update(headers or dict(BASE_HEADERS))
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=10, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


@dataclass
class Result:
    """One request's outcome, as the phases read it."""

    status: int
    elapsed: float
    # None only on input -> derived from status in __post_init__ (never None after construction).
    rclass: RClass | None = None
    count: int = 0
    next_cursor: Any = None
    retry_after: float | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    error: str | None = None

    def __post_init__(self) -> None:
        """Derive `rclass` from the status when none was given."""
        if self.rclass is None:
            self.rclass = default_rclass(self.status)


def interesting_headers(resp: Any) -> dict[str, str]:
    """Return the response headers worth reporting: rate limits, retry hints, server IDs.

    Values are scrubbed of registered secrets, since a server can reflect the request in them.
    """
    return {
        k: scrub(v)
        for k, v in resp.headers.items()
        if any(sub in k.lower() for sub in HEADER_SUBSTRINGS)
    }


_PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)


def parse_response(resp: Any, elapsed: float, endpoint: Endpoint) -> Result:
    """Classify a response with the endpoint's provider, then read its page.

    On success the endpoint reads the item count and next cursor from the whole
    response, so header-based pagination and non-JSON bodies work.

    Args:
        resp: A `requests.Response` or an `httpx.Response`.
        elapsed: The request's wall time in seconds.
        endpoint: The endpoint that sent it.

    Returns:
        The classified result, with the page's count and next cursor on success.
    """
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
            res.error = scrub(f"OK response but parse_page failed: {e}")
    elif rclass == RClass.ERROR and resp.status_code >= HTTPStatus.BAD_REQUEST:
        res.error = scrub(resp.text)[:200]  # scrubbed first, so the cut can't split a secret
    return res


def request_args(endpoint: Endpoint, cursor: Any) -> tuple[RequestSpec, dict[str, Any]]:
    """Return the request for `cursor` and its query parameters.

    The parameters are the provider's auth parameters, overridden by the request's own.
    """
    spec = endpoint.build_request(cursor)
    return spec, {**endpoint.provider().auth_params(), **(spec.params or {})}


def fetch(session: requests.Session, endpoint: Endpoint, cursor: Any, budget: Budget) -> Result:
    """One probe request for `endpoint` at pagination position `cursor`."""
    if not budget.take():
        return Result(
            status=-1, elapsed=0.0, rclass=RClass.BUDGET, error="request budget exhausted"
        )

    spec, params = request_args(endpoint, cursor)
    t0 = time.perf_counter()
    try:
        resp = session.request(
            spec.method, spec.url, params=params, json=spec.json_body, timeout=30
        )
    except requests.RequestException as e:
        return Result(
            status=0, elapsed=time.perf_counter() - t0, rclass=RClass.ERROR, error=scrub(str(e))
        )
    return parse_response(resp, time.perf_counter() - t0, endpoint)

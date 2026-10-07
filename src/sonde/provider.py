"""The per-API "provider" abstraction.

A Provider captures everything that varies by API rather than by endpoint:

- `classify(response)`: what counts as success or throttling.
- `parse_rate_limit(headers)`: the API's rate-limit headers, as a `RateLimit`.
- `auth_headers()` and `auth_params()`: credentials as headers or query parameters.
- `credentials()`: the raw secrets inside those headers, so logs can redact them.

The base `Provider` is a working generic provider: 200 is ok, 429 is throttled, the
IETF rate-limit header draft, and no auth.
Subclasses specialise. Endpoints choose a provider in `Endpoint.make_provider()`.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, override

from sonde.core import RClass, default_rclass

__all__ = ["GitHubProvider", "Provider", "RateLimit", "RobloxProvider", "authoritative_limit"]


@dataclass(frozen=True)
class RateLimit:
    """An API's rate-limit headers, normalised.

    Attributes:
        limit: Requests allowed per window; with several policies, the one that binds.
        window_s: The window in seconds, or None when the API doesn't state it and no
            default is known.
        remaining: Requests left in the current window, when stated.
        reset_s: Seconds until the window resets, when stated (epoch formats are
            converted).
        policies: Every (limit, window_s) policy, as the headers list them or the provider
            knows them.
        raw: The rate-limit headers as received, with lower-cased names.
    """

    limit: int
    window_s: int | None
    remaining: int | None
    reset_s: int | None
    policies: tuple[tuple[int, int | None], ...]
    raw: dict[str, str]


class Provider:
    """Generic provider: 200/429, IETF-draft rate-limit headers, no auth."""

    name = "generic"

    def classify(self, resp: Any) -> RClass:
        """Return how a response counts: ok, throttled, or an error."""
        return default_rclass(resp.status_code)

    def parse_rate_limit(self, headers: dict[str, str] | None) -> RateLimit | None:
        """Normalise the response's rate-limit headers, or return None when it has none.

        >>> Provider().parse_rate_limit({"X-RateLimit-Limit": "60;w=60, 1000;w=3600"}).limit
        1000
        """
        low = {k.lower(): v for k, v in (headers or {}).items()}
        limit_raw = low.get("x-ratelimit-limit")
        if not limit_raw:
            return None

        policies: list[tuple[int, int | None]] = []
        for raw_item in str(limit_raw).split(","):
            item = raw_item.strip()
            if not item:
                continue
            parts = item.split(";")
            try:
                count = int(parts[0].strip())
            except ValueError:
                continue
            window = None
            for raw_param in parts[1:]:
                param = raw_param.strip()
                if param.startswith("w="):
                    with contextlib.suppress(ValueError):
                        window = int(param[2:])
            policies.append((count, window))

        windowed = [(c, w) for c, w in policies if w is not None and w > 0]
        if windowed:
            # Lowest sustained rate (count/window) binds, NOT the smallest window:
            # a short-window policy can permit a higher rate than a long-window one.
            limit, window_s = min(windowed, key=lambda t: t[0] / t[1])
        elif policies:
            limit, window_s = min(policies, key=lambda t: t[0])[0], None
        else:
            return None

        return RateLimit(
            limit=limit,
            window_s=window_s,
            remaining=_first_int(low.get("x-ratelimit-remaining")),
            reset_s=_first_int(low.get("x-ratelimit-reset")),  # already seconds-until
            policies=tuple(policies),
            raw={k: v for k, v in low.items() if k.startswith("x-ratelimit")},
        )

    def auth_headers(self) -> dict[str, str]:
        """Return the credentials sent as headers."""
        return {}

    def auth_params(self) -> dict[str, str]:
        """Return the credentials sent as query parameters."""
        return {}

    def credentials(self) -> list[str]:
        """Return the raw secrets inside `auth_headers()`, to redact from logs.

        A provider whose `auth_headers()` carries a credential overrides this too, since
        only it knows the header's format. Query-parameter values are redacted without
        being listed here.
        """
        return []


class RobloxProvider(Provider):
    """Roblox legacy endpoints: the generic rules plus cookie or bearer auth.

    Roblox uses the IETF header format, so classification and parsing are the generic
    provider's.
    """

    name = "roblox"

    def __init__(self) -> None:
        """Read the credentials from `ROBLOX_COOKIE` and `ROBLOX_BEARER`, if set."""
        self._cookie = os.environ.get("ROBLOX_COOKIE")
        self._bearer = os.environ.get("ROBLOX_BEARER")

    @override
    def auth_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._cookie:
            h["Cookie"] = f".ROBLOSECURITY={self._cookie}"  # legacy web-session auth
        if self._bearer:
            h["Authorization"] = f"Bearer {self._bearer}"  # Open Cloud (ignored by legacy)
        return h

    @override
    def credentials(self) -> list[str]:
        return [c for c in (self._cookie, self._bearer) if c]


class GitHubProvider(Provider):
    """GitHub REST API rules, with token auth from GITHUB_TOKEN.

    GitHub throttles with 403 (and `x-ratelimit-remaining: 0`) as well as 429, gives
    the reset as a Unix epoch (converted to seconds until), and omits the window, so
    a known default stands in.
    """

    name = "github"

    def __init__(self, window_s: int = 3600) -> None:
        """Set the rate-limit window, and read the token from `GITHUB_TOKEN`, if set.

        Args:
            window_s: The window in seconds, which the headers don't state. The core
                API's is an hour; other resources differ (search's is 60 seconds).
        """
        self.window_s = window_s
        self._token = os.environ.get("GITHUB_TOKEN")

    @override
    def classify(self, resp: Any) -> RClass:
        # primary limit -> 403 with remaining 0; secondary -> 403 with Retry-After
        if resp.status_code == HTTPStatus.FORBIDDEN and (
            resp.headers.get("x-ratelimit-remaining") == "0"
            or resp.headers.get("retry-after") is not None
        ):
            return RClass.THROTTLED
        return super().classify(resp)

    @override
    def parse_rate_limit(self, headers: dict[str, str] | None) -> RateLimit | None:
        low = {k.lower(): v for k, v in (headers or {}).items()}
        limit = _first_int(low.get("x-ratelimit-limit"))
        if limit is None:
            return None
        reset_epoch = _first_int(low.get("x-ratelimit-reset"))
        reset_s = max(0, reset_epoch - int(time.time())) if reset_epoch is not None else None
        return RateLimit(
            limit=limit,
            window_s=self.window_s,  # not in headers; known default
            remaining=_first_int(low.get("x-ratelimit-remaining")),
            reset_s=reset_s,  # epoch -> seconds-until
            policies=((limit, self.window_s),),
            raw={k: v for k, v in low.items() if k.startswith("x-ratelimit")},
        )

    @override
    def auth_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @override
    def credentials(self) -> list[str]:
        return [self._token] if self._token else []


def _first_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).split(",")[0].strip())
    except (ValueError, AttributeError):
        return None


def authoritative_limit(rate_limit: RateLimit | None) -> tuple[int, int] | None:
    """Return the (limit, window_s) pair when both are known and nonzero, else None."""
    if rate_limit is None or not rate_limit.limit or not rate_limit.window_s:
        return None
    return rate_limit.limit, rate_limit.window_s

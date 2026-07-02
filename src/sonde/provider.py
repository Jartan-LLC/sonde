"""
provider.py — the per-API "provider" abstraction.

A Provider captures everything that varies by API rather than by endpoint:
  * classify(response)     -> RClass   (what counts as success vs throttled)
  * parse_rate_limit(hdrs) -> dict      (normalise the API's rate-limit headers)
  * auth_headers()         -> dict      (credentials as headers)
  * auth_params()          -> dict      (credentials as query params, if any)

The base `Provider` is a working GENERIC provider: 200 = ok / 429 = throttled, the
IETF `RateLimit`-draft header format (which is what Roblox uses), and no auth.
Subclasses specialise. Endpoints reference a provider via Endpoint._make_provider().

normalised rate-limit dict shape (all keys optional / may be None):
    {limit, window_s, remaining, reset_s, policies, raw}
  * reset_s is ALWAYS seconds-until-reset (epoch formats are converted).
  * window_s may be None if the API doesn't express it and none is known.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .core import RClass

__all__ = ["Provider", "RobloxProvider", "GitHubProvider"]


class Provider:
    """Generic provider: 200/429, IETF-draft rate-limit headers, no auth."""

    name = "generic"

    # --- classification ---
    def classify(self, resp: Any) -> RClass:
        sc = resp.status_code
        if sc == 200:
            return RClass.OK
        if sc == 429:
            return RClass.THROTTLED
        return RClass.ERROR

    # --- rate-limit header parsing (IETF RateLimit draft, e.g. Roblox) ---
    def parse_rate_limit(self, headers: dict[str, str] | None) -> dict[str, Any]:
        low = {k.lower(): v for k, v in (headers or {}).items()}
        limit_raw = low.get("x-ratelimit-limit")
        if not limit_raw:
            return {}

        policies = []  # (count, window_or_None)
        for item in str(limit_raw).split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split(";")
            try:
                count = int(parts[0].strip())
            except ValueError:
                continue
            window = None
            for pr in parts[1:]:
                pr = pr.strip()
                if pr.startswith("w="):
                    try:
                        window = int(pr[2:])
                    except ValueError:
                        pass
            policies.append((count, window))

        windowed = [(c, w) for c, w in policies if w and w > 0]
        if windowed:
            # Lowest sustained rate (count/window) binds, NOT the smallest window:
            # a short-window policy can permit a higher rate than a long-window one.
            limit, window_s = min(windowed, key=lambda t: t[0] / t[1])
        elif policies:
            limit, window_s = min(policies, key=lambda t: t[0])[0], None
        else:
            return {}

        return {
            "limit": limit,
            "window_s": window_s,
            "remaining": _first_int(low.get("x-ratelimit-remaining")),
            "reset_s": _first_int(low.get("x-ratelimit-reset")),  # already seconds-until
            "policies": policies,
            "raw": {k: v for k, v in low.items() if k.startswith("x-ratelimit")},
        }

    # --- auth ---
    def auth_headers(self) -> dict[str, str]:
        return {}

    def auth_params(self) -> dict[str, str]:
        return {}


class RobloxProvider(Provider):
    """Roblox legacy endpoints: identical classification + header parsing to the
    generic provider (Roblox uses the IETF format), plus cookie/bearer auth."""

    name = "roblox"

    def auth_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        cookie = os.environ.get("ROBLOX_COOKIE")
        bearer = os.environ.get("ROBLOX_BEARER")
        if cookie:
            h["Cookie"] = f".ROBLOSECURITY={cookie}"  # legacy web-session auth
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"  # Open Cloud (ignored by legacy)
        return h


class GitHubProvider(Provider):
    """GitHub REST API: throttles with 403 (+ `x-ratelimit-remaining: 0`) as well as
    429, expresses reset as a Unix EPOCH (converted to seconds-until), and omits the
    window (so a known default is injected). Token auth via GITHUB_TOKEN."""

    name = "github"

    def __init__(self, window_s: int = 3600) -> None:
        # GitHub core API is 5000/hour; other resources differ (search=60s) -> override.
        self.window_s = window_s

    def classify(self, resp: Any) -> RClass:
        sc = resp.status_code
        if sc == 200:
            return RClass.OK
        if sc == 429:
            return RClass.THROTTLED
        # primary limit -> 403 with remaining 0; secondary -> 403 with Retry-After
        if sc == 403 and (
            resp.headers.get("x-ratelimit-remaining") == "0"
            or resp.headers.get("retry-after") is not None
        ):
            return RClass.THROTTLED
        return RClass.ERROR

    def parse_rate_limit(self, headers: dict[str, str] | None) -> dict[str, Any]:
        low = {k.lower(): v for k, v in (headers or {}).items()}
        limit = _first_int(low.get("x-ratelimit-limit"))
        if limit is None:
            return {}
        reset_epoch = _first_int(low.get("x-ratelimit-reset"))
        reset_s = max(0, reset_epoch - int(time.time())) if reset_epoch is not None else None
        return {
            "limit": limit,
            "window_s": self.window_s,  # not in headers; known default
            "remaining": _first_int(low.get("x-ratelimit-remaining")),
            "reset_s": reset_s,  # epoch -> seconds-until
            "policies": [(limit, self.window_s)],
            "raw": {k: v for k, v in low.items() if k.startswith("x-ratelimit")},
        }

    def auth_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        tok = os.environ.get("GITHUB_TOKEN")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h


def _first_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).split(",")[0].strip())
    except (ValueError, AttributeError):
        return None

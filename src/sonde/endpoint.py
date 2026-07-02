"""
endpoint.py — the pluggable Endpoint interface.

To test a new API endpoint you implement ONE subclass of `Endpoint` and register
it. The generic probing engine (phases.py) drives everything else. A subclass must
answer three questions:

  1. build_request(cursor) -> RequestSpec   How do I form a request (URL, params,
                                             method) for a given paging position?
  2. parse_page(body)      -> PageResult    Given a 200 body, how many items did I
                                             get and what's the next paging cursor?
  3. total_items()         -> int | None    (optional) how many items exist in total,
                                             so the tool can estimate scrape time.

Plus optional CLI plumbing (add_arguments / from_args) and extra_headers().
See endpoints/asset_owners.py for a worked example, and the README.
"""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .provider import Provider


@dataclass
class RequestSpec:
    """A single HTTP request to issue."""

    url: str
    params: dict[str, Any] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None


@dataclass
class PageResult:
    """What a successful response yielded."""

    count: int  # number of items in this response (0 if not a page)
    next_cursor: Any = None  # opaque token for the next page, or None if last/none


class Endpoint(ABC):
    name: str = "base"  # CLI subcommand name (unique)
    help: str = "abstract endpoint"  # one-line description for --help

    # --- CLI plumbing (override as needed) ---
    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Register endpoint-specific CLI arguments on `parser`."""

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Endpoint:
        """Build an instance from parsed CLI args."""
        return cls()

    # --- provider (which API's rules apply) ---
    def _make_provider(self) -> Provider:
        """Return the Provider for this endpoint's API. Override to use Roblox/GitHub/
        etc. Default is the generic provider (200/429, IETF headers, no auth)."""
        return Provider()

    def provider(self) -> Provider:
        """Memoised provider instance for this endpoint."""
        prov = self.__dict__.get("_provider_instance")
        if prov is None:
            prov = self._make_provider()
            self.__dict__["_provider_instance"] = prov
        return prov

    # --- required behaviour ---
    @abstractmethod
    def build_request(self, cursor: Any) -> RequestSpec: ...

    @abstractmethod
    def parse_page(self, response: Any) -> PageResult:
        """Extract item count + next cursor from a successful RESPONSE object
        (requests.Response / httpx.Response). Call response.json() for JSON bodies;
        read response.headers for header-based pagination (e.g. a Link header)."""
        ...

    # --- optional behaviour ---
    def total_items(self) -> int | None:
        """Known/estimated total items to scrape, for the wall-clock estimate.
        Return None if unknown (the tool will then report rate only)."""
        return None

    def extra_headers(self) -> dict[str, str]:
        """Endpoint-specific headers beyond the provider's auth headers."""
        return {}


# --------------------------------------------------------------------------- #
# Registry: endpoints register themselves so the CLI can offer them as subcommands.
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[Endpoint]] = {}


def register(cls: type[Endpoint]) -> type[Endpoint]:
    """Class decorator: register an Endpoint subclass under its `name`."""
    if not getattr(cls, "name", None) or cls.name == "base":
        raise ValueError(f"{cls.__name__} must set a unique `name`")
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate endpoint name: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Endpoint] | None:
    return _REGISTRY.get(name)


def all_endpoints() -> dict[str, type[Endpoint]]:
    return dict(_REGISTRY)

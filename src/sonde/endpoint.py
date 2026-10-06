"""The pluggable Endpoint interface.

To test a new API endpoint you implement ONE subclass of `Endpoint` and register
it. The generic probing engine (`sonde.phases`) drives everything else. A subclass must
answer three questions:

  1. build_request(cursor) -> RequestSpec   How do I form a request (URL, params,
                                             method) for a given paging position?
  2. parse_page(response)  -> PageResult    Given a successful response, how many items
                                             did I get and what's the next paging cursor?
  3. total_items()         -> int | None    (optional) how many items exist in total,
                                             so the tool can estimate scrape time.

Plus optional CLI plumbing (add_arguments / from_args) and extra_headers().
See endpoints/asset_owners.py for a worked example, and the README.
"""

from __future__ import annotations

import argparse
import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Self

from sonde.provider import Provider

__all__ = [
    "Endpoint",
    "PageResult",
    "RequestSpec",
    "add_pagination_args",
    "all_endpoints",
    "get",
    "pagination_from_args",
    "register",
]


@dataclass
class RequestSpec:
    """A single HTTP request to issue.

    `params` are the endpoint's own query parameters, not credentials: a provider's
    `auth_params()` carries those, so logs can redact them.
    """

    url: str
    params: dict[str, Any] = field(default_factory=dict[str, Any])
    method: str = "GET"
    json_body: Any = None


@dataclass
class PageResult:
    """What a successful response yielded."""

    count: int  # number of items in this response (0 if not a page)
    next_cursor: Any = None  # opaque token for the next page, or None if last/none


class Endpoint(ABC):
    """One API endpoint to probe: how to request a page and how to read it."""

    name: str = "base"  # CLI subcommand name (unique)
    help: str = "abstract endpoint"  # one-line description for --help
    _provider_instance: Provider | None = None

    @classmethod  # noqa: B027 - an optional hook
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Register endpoint-specific CLI arguments on `parser`."""

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Self:  # noqa: ARG003 - the default ignores them
        """Build an instance from parsed CLI args."""
        return cls()

    def _make_provider(self) -> Provider:
        """Return the Provider for this endpoint's API.

        The default is the generic provider: 200/429, IETF headers, no auth. Override it
        for an API with its own rules, such as Roblox's or GitHub's.
        """
        return Provider()

    def provider(self) -> Provider:
        """Return this endpoint's provider, the same instance on every call."""
        if self._provider_instance is None:
            self._provider_instance = self._make_provider()
        return self._provider_instance

    @abstractmethod
    def build_request(self, cursor: Any) -> RequestSpec:
        """Return the request for the page at `cursor` (None for the first page)."""

    @abstractmethod
    def parse_page(self, response: Any) -> PageResult:
        """Read the item count and next cursor from a successful response.

        `response` is a `requests.Response` or an `httpx.Response`. Call
        `response.json()` for a JSON body; read `response.headers` for header-based
        pagination, such as a Link header.
        """

    def total_items(self) -> int | None:
        """Return the known or estimated item total, for the wall-clock estimate.

        None means unknown, and the report gives the rate only.
        """
        return None

    def extra_headers(self) -> dict[str, str]:
        """Return endpoint-specific headers beyond the provider's auth headers.

        Not for credentials: a provider declares those, so logs can redact them.
        """
        return {}


_REGISTRY: dict[str, type[Endpoint]] = {}


def register[E: type[Endpoint]](cls: E) -> E:
    """Class decorator: register an Endpoint subclass under its `name`."""
    if not getattr(cls, "name", None) or cls.name == "base":
        raise ValueError(f"{cls.__name__} must set a unique `name`")
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate endpoint name: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def _loaded_registry() -> dict[str, type[Endpoint]]:
    """Return the registry, once the built-in endpoints have registered themselves."""
    importlib.import_module("sonde.endpoints")
    return _REGISTRY


def get(name: str) -> type[Endpoint] | None:
    """Return the endpoint registered under `name`, built-ins included, or None."""
    return _loaded_registry().get(name)


def all_endpoints() -> dict[str, type[Endpoint]]:
    """Return every registered endpoint, built-in or imported, by name."""
    return dict(_loaded_registry())


def add_pagination_args(parser: argparse.ArgumentParser, *, page_max: int = 100) -> None:
    """Register the standard `--page-size` / `--total-items` flags on `parser`.

    A paginated endpoint calls this from `add_arguments` and `pagination_from_args` from
    `from_args`, so the flags are spelled the same on every endpoint.
    """
    parser.add_argument(
        "--page-size", type=int, default=page_max, help=f"items per page; capped at {page_max}"
    )
    parser.add_argument(
        "--total-items",
        type=int,
        default=None,
        help="known total item count, for the wall-clock estimate",
    )


def pagination_from_args(
    args: argparse.Namespace, *, page_max: int = 100
) -> tuple[int, int | None]:
    """Return `(page_size, total_items)` from parsed args, page_size clamped to `page_max`."""
    return min(args.page_size, page_max), args.total_items

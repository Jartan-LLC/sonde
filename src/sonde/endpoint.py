"""The pluggable Endpoint interface.

To test a new API endpoint you implement ONE subclass of `Endpoint` and make it
known to sonde. The generic probing engine (`sonde.phases`) drives everything else. A subclass
answers three questions:

- `build_request(cursor) -> RequestSpec`: how to form the request for a paging
  position.
- `parse_page(response) -> PageResult`: how many items a successful response holds,
  and the next paging cursor.
- `total_items() -> int | None` (optional): how many items exist in total, so the tool
  can estimate the scrape time.

Plus optional CLI plumbing (add_arguments / from_args) and extra_headers().
An installed package adds an endpoint under the `sonde.endpoints` entry-point group.
See endpoints/asset_owners.py for a worked example, and the README.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Self

from sonde.provider import Provider

__all__ = [
    "Endpoint",
    "PageResult",
    "PluginError",
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

    def make_provider(self) -> Provider:
        """Return the Provider for this endpoint's API.

        The default is the generic provider: 200/429, IETF headers, no auth. Override it
        for an API with its own rules, such as Roblox's or GitHub's.
        """
        return Provider()

    def provider(self) -> Provider:
        """Return this endpoint's provider, the same instance on every call."""
        if self._provider_instance is None:
            self._provider_instance = self.make_provider()
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


class PluginError(Exception):
    """An endpoint couldn't be loaded, registered, or given its CLI arguments."""


_PLUGIN_GROUP = "sonde.endpoints"


@functools.cache
def _load_plugins() -> None:
    """Register the endpoint each installed package declares under `_PLUGIN_GROUP`.

    On failure the registry is left as it was, so a retry reports the same error.

    Raises:
        PluginError: An entry point fails to load, isn't a concrete `Endpoint`
            subclass, or its `name` is unset or taken.
    """
    before = dict(_REGISTRY)
    try:
        _register_plugins()
    except PluginError:
        _REGISTRY.clear()
        _REGISTRY.update(before)
        raise


def _register_plugins() -> None:
    for ep in entry_points(group=_PLUGIN_GROUP):
        where = f"entry point {ep.name!r} ({ep.value})"
        try:
            cls = ep.load()
        except Exception as e:  # a plugin can fail in any way while importing
            raise PluginError(f"{where} failed to load: {e}") from e
        if not (isinstance(cls, type) and issubclass(cls, Endpoint)):
            raise PluginError(f"{where} is not an Endpoint subclass")
        if inspect.isabstract(cls):
            raise PluginError(f"{where} is abstract: it doesn't implement every Endpoint method")
        if _REGISTRY.get(cls.name) is cls:  # already registered with @register
            continue
        try:
            register(cls)
        except ValueError as e:
            raise PluginError(f"{where}: {e}") from e


def _loaded_registry() -> dict[str, type[Endpoint]]:
    """Return the registry, with the built-in and installed endpoints registered."""
    importlib.import_module("sonde.endpoints")
    _load_plugins()
    return _REGISTRY


def get(name: str) -> type[Endpoint] | None:
    """Return the endpoint registered under `name`, built-ins included, or None."""
    return _loaded_registry().get(name)


def all_endpoints() -> dict[str, type[Endpoint]]:
    """Return every registered endpoint, built-in or from an installed package, by name."""
    return dict(_loaded_registry())


def add_pagination_args(parser: argparse.ArgumentParser, *, page_max: int = 100) -> None:
    """Register the standard `--page-size` / `--total-items` flags on `parser`.

    Call this from a paginated endpoint's `add_arguments`, and `pagination_from_args` from
    its `from_args`, so every endpoint spells the flags the same.
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

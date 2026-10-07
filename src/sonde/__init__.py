"""sonde — probe any HTTP API for its rate limits, burst ceiling, and full-scrape time."""

__version__ = "0.1.0"

# Imported after __version__, which core.py reads while these imports run.
from sonde.core import RClass
from sonde.endpoint import (
    Endpoint,
    PageResult,
    RequestSpec,
    add_pagination_args,
    pagination_from_args,
    register,
)
from sonde.provider import GitHubProvider, Provider, RateLimit, RobloxProvider

__all__ = [
    "Endpoint",
    "GitHubProvider",
    "PageResult",
    "Provider",
    "RClass",
    "RateLimit",
    "RequestSpec",
    "RobloxProvider",
    "__version__",
    "add_pagination_args",
    "pagination_from_args",
    "register",
]

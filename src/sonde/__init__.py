"""sonde — probe any HTTP API for its rate limits, burst ceiling, and full-scrape time."""

__version__ = "0.2.0"

# Imported after __version__, which core.py reads while these imports run.
from sonde.endpoint import (
    Endpoint,
    PageResult,
    RequestSpec,
    add_pagination_args,
    pagination_from_args,
    register,
)
from sonde.provider import Provider

__all__ = [
    "Endpoint",
    "PageResult",
    "Provider",
    "RequestSpec",
    "__version__",
    "add_pagination_args",
    "pagination_from_args",
    "register",
]

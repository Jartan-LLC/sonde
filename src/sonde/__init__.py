"""sonde — probe any HTTP API for its rate limits, burst ceiling, and full-scrape time."""

__version__ = "0.1.0"

# Re-exported after __version__ so core.py's `from . import __version__` resolves.
from .endpoint import Endpoint, PageResult, RequestSpec, register  # noqa: E402
from .provider import Provider  # noqa: E402

__all__ = [
    "__version__",
    "Endpoint",
    "RequestSpec",
    "PageResult",
    "register",
    "Provider",
]

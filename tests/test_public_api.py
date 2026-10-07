"""Canary: the public API the README advertises stays importable from `sonde`.

A silent drop from __init__'s re-export / __all__ would pass every other test
while breaking `from sonde import ...` in the docs — this locks it.
"""

import sonde


def test_public_api_reexported():
    assert set(sonde.__all__) >= {
        "__version__",
        "Endpoint",
        "RequestSpec",
        "PageResult",
        "register",
        "Provider",
        "RClass",
        "RateLimit",
        "GitHubProvider",
        "RobloxProvider",
    }
    assert all(hasattr(sonde, name) for name in sonde.__all__)

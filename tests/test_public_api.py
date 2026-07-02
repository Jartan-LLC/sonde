"""Canary: the public API the README advertises stays importable from `sonde`.

A silent drop from __init__'s re-export / __all__ would pass every other test
while breaking `from sonde import ...` in the docs — this locks it.
"""

import sonde
from sonde import Endpoint, PageResult, Provider, RequestSpec, register


def test_public_api_reexported():
    for obj in (Endpoint, RequestSpec, PageResult, register, Provider):
        assert obj is not None
    assert set(sonde.__all__) >= {
        "__version__",
        "Endpoint",
        "RequestSpec",
        "PageResult",
        "register",
        "Provider",
    }

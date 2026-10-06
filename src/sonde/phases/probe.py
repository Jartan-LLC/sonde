"""What the probing phases share: the probe they run against, and the cursor cycle."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

from sonde.core import Budget
from sonde.endpoint import Endpoint


@dataclass(frozen=True)
class Probe:
    """What the probing phases work against.

    Attributes:
        endpoint: The endpoint probed.
        budget: The request budget every phase draws from.
        session: The serial phases' HTTP session.
        headers: The request headers, for the burst phase's own httpx client.
    """

    endpoint: Endpoint
    budget: Budget
    session: requests.Session
    headers: dict[str, str]


def cursor_cycle(cursor_pool: list[Any]) -> Iterator[Any]:
    """Cycle through the collected cursors, or yield None forever when there are none."""
    return itertools.cycle(cursor_pool or [None])

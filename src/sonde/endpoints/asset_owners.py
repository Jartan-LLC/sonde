"""
endpoints/asset_owners.py — the asset-owners endpoint.

    GET https://inventory.roblox.com/v2/assets/{assetId}/owners
        ?limit={10|25|50|100}&cursor={cursor}&sortOrder={Asc|Desc}

Legacy cookie-auth endpoint. Returns a paginated list of owners of a collectible
(1.0-limited) asset. This is the reference implementation of the Endpoint interface.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..endpoint import Endpoint, PageResult, RequestSpec, register
from ..provider import Provider, RobloxProvider


@register
class AssetOwnersEndpoint(Endpoint):
    name = "asset-owners"
    help = "inventory.roblox.com/v2/assets/{id}/owners — owners of a collectible asset"

    BASE = "https://inventory.roblox.com/v2/assets/{asset_id}/owners"
    MAX_PAGE = 100  # documented ceiling for the `limit` param

    def __init__(
        self,
        asset_id: int,
        total_copies: int | None = None,
        page_size: int = 100,
        sort_order: str = "Asc",
    ) -> None:
        self.asset_id = asset_id
        self._total = total_copies
        self.page_size = min(page_size, self.MAX_PAGE)
        self.sort_order = sort_order

    def _make_provider(self) -> Provider:
        return RobloxProvider()

    @classmethod
    def add_arguments(cls, p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--asset-id",
            type=int,
            required=True,
            help="asset id to probe (e.g. 20573078 for Shaggy)",
        )
        p.add_argument(
            "--total-copies",
            type=int,
            default=None,
            help="known total owners, for the wall-clock estimate (e.g. 1470000)",
        )
        p.add_argument(
            "--page-size",
            type=int,
            default=100,
            help="items per page; capped at 100 (the endpoint max)",
        )
        p.add_argument("--sort-order", choices=["Asc", "Desc"], default="Asc")

    @classmethod
    def from_args(cls, a: argparse.Namespace) -> AssetOwnersEndpoint:
        return cls(
            asset_id=a.asset_id,
            total_copies=a.total_copies,
            page_size=a.page_size,
            sort_order=a.sort_order,
        )

    def build_request(self, cursor: Any) -> RequestSpec:
        params = {"limit": self.page_size, "sortOrder": self.sort_order}
        if cursor:
            params["cursor"] = cursor
        return RequestSpec(url=self.BASE.format(asset_id=self.asset_id), params=params)

    def parse_page(self, response: Any) -> PageResult:
        body = response.json()
        data = body.get("data", [])
        return PageResult(count=len(data), next_cursor=body.get("nextPageCursor"))

    def total_items(self) -> int | None:
        return self._total

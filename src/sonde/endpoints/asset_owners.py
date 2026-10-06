"""endpoints/asset_owners.py — the asset-owners endpoint.

    GET https://inventory.roblox.com/v2/assets/{assetId}/owners
        ?limit={10|25|50|100}&cursor={cursor}&sortOrder={Asc|Desc}

Legacy cookie-auth endpoint. Returns a paginated list of owners of a collectible
(1.0-limited) asset. This is the reference implementation of the Endpoint interface.
"""

from __future__ import annotations

import argparse
from typing import Any, Self, override

from sonde.endpoint import (
    Endpoint,
    PageResult,
    RequestSpec,
    add_pagination_args,
    pagination_from_args,
    register,
)
from sonde.provider import Provider, RobloxProvider


@register
class AssetOwnersEndpoint(Endpoint):
    """The owners of one collectible asset, paged by cursor."""

    name = "asset-owners"
    help = "inventory.roblox.com/v2/assets/{id}/owners — owners of a collectible asset"

    BASE = "https://inventory.roblox.com/v2/assets/{asset_id}/owners"
    MAX_PAGE = 100  # documented ceiling for the `limit` param

    def __init__(
        self,
        asset_id: int,
        total_items: int | None = None,
        page_size: int = 100,
        sort_order: str = "Asc",
    ) -> None:
        """Set up the probe for one asset.

        Args:
            asset_id: The collectible asset whose owners are listed.
            total_items: The known owner count, for the wall-clock estimate.
            page_size: Owners per page, capped at `MAX_PAGE`.
            sort_order: `Asc` or `Desc`.
        """
        self.asset_id = asset_id
        self._total = total_items
        self.page_size = min(page_size, self.MAX_PAGE)
        self.sort_order = sort_order

    @override
    def _make_provider(self) -> Provider:
        return RobloxProvider()

    @override
    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--asset-id",
            type=int,
            required=True,
            help="asset id to probe (e.g. 20573078 for Shaggy)",
        )
        parser.add_argument("--sort-order", choices=["Asc", "Desc"], default="Asc")
        add_pagination_args(parser, page_max=cls.MAX_PAGE)

    @override
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Self:
        page_size, total_items = pagination_from_args(args, page_max=cls.MAX_PAGE)
        return cls(
            asset_id=args.asset_id,
            total_items=total_items,
            page_size=page_size,
            sort_order=args.sort_order,
        )

    @override
    def build_request(self, cursor: Any) -> RequestSpec:
        params: dict[str, Any] = {"limit": self.page_size, "sortOrder": self.sort_order}
        if cursor:
            params["cursor"] = cursor
        return RequestSpec(url=self.BASE.format(asset_id=self.asset_id), params=params)

    @override
    def parse_page(self, response: Any) -> PageResult:
        body = response.json()
        data = body.get("data", [])
        return PageResult(count=len(data), next_cursor=body.get("nextPageCursor"))

    @override
    def total_items(self) -> int | None:
        return self._total

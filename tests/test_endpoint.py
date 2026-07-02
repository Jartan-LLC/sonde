"""Tests for the Endpoint interface, registry, and the asset-owners implementation."""

import argparse

import pytest

from sonde import endpoint
from sonde.endpoint import Endpoint, PageResult, RequestSpec, register
from sonde.endpoints.asset_owners import AssetOwnersEndpoint
from tests.helpers import FakeResp


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_asset_owners_registered():
    assert "asset-owners" in endpoint.all_endpoints()
    assert endpoint.get("asset-owners") is AssetOwnersEndpoint


def test_register_requires_name():
    class NoName(Endpoint):
        def build_request(self, cursor):
            return RequestSpec(url="x")

        def parse_page(self, body):
            return PageResult(0)

    with pytest.raises(ValueError):
        register(NoName)


def test_register_rejects_duplicate():
    with pytest.raises(ValueError):
        register(AssetOwnersEndpoint)  # already registered under "asset-owners"


# --------------------------------------------------------------------------- #
# asset-owners behaviour
# --------------------------------------------------------------------------- #
def test_build_request_without_cursor():
    ep = AssetOwnersEndpoint(asset_id=20573078, page_size=100, sort_order="Asc")
    spec = ep.build_request(None)
    assert spec.method == "GET"
    assert spec.url.endswith("/v2/assets/20573078/owners")
    assert spec.params == {"limit": 100, "sortOrder": "Asc"}
    assert "cursor" not in spec.params


def test_build_request_with_cursor():
    ep = AssetOwnersEndpoint(asset_id=1)
    spec = ep.build_request("NEXT")
    assert spec.params["cursor"] == "NEXT"


def test_page_size_capped_at_100():
    ep = AssetOwnersEndpoint(asset_id=1, page_size=500)
    assert ep.page_size == 100
    assert ep.build_request(None).params["limit"] == 100


def test_parse_page():
    ep = AssetOwnersEndpoint(asset_id=1)
    resp = FakeResp(200, body={"data": [{"userId": 1}, {"userId": 2}], "nextPageCursor": "n"})
    page = ep.parse_page(resp)
    assert page.count == 2
    assert page.next_cursor == "n"


def test_parse_page_empty():
    ep = AssetOwnersEndpoint(asset_id=1)
    page = ep.parse_page(FakeResp(200, body={"data": [], "nextPageCursor": None}))
    assert page.count == 0
    assert page.next_cursor is None


def test_asset_owners_uses_roblox_provider():
    assert AssetOwnersEndpoint(asset_id=1).provider().name == "roblox"


def test_total_items():
    assert AssetOwnersEndpoint(asset_id=1, total_items=1470000).total_items() == 1470000
    assert AssetOwnersEndpoint(asset_id=1).total_items() is None


def test_from_args_roundtrip():
    ns = argparse.Namespace(asset_id=42, total_items=999, page_size=50, sort_order="Desc")
    ep = AssetOwnersEndpoint.from_args(ns)
    assert ep.asset_id == 42
    assert ep.total_items() == 999
    assert ep.page_size == 50
    assert ep.sort_order == "Desc"

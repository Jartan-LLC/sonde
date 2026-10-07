"""Tests for the Endpoint interface, registry, and the asset-owners implementation."""

import argparse
from collections.abc import Callable, Iterator
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from sonde import cli, endpoint
from sonde.endpoint import Endpoint, PageResult, PluginError, RequestSpec, register
from sonde.endpoints.asset_owners import AssetOwnersEndpoint
from tests.helpers import FakeEndpoint, FakeResp


def test_asset_owners_registered():
    assert "asset-owners" in endpoint.all_endpoints()
    assert endpoint.get("asset-owners") is AssetOwnersEndpoint


def test_register_requires_name():
    class NoName(Endpoint):
        def build_request(self, cursor: Any) -> RequestSpec:
            return RequestSpec(url="x")

        def parse_page(self, response: Any) -> PageResult:
            return PageResult(0)

    with pytest.raises(ValueError, match="must set a unique `name`"):
        register(NoName)


def test_register_rejects_duplicate():
    with pytest.raises(ValueError, match="duplicate endpoint name"):
        register(AssetOwnersEndpoint)  # already registered under "asset-owners"


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


def test_add_pagination_args_defaults():
    p = argparse.ArgumentParser()
    endpoint.add_pagination_args(p, page_max=100)
    a = p.parse_args([])
    assert a.page_size == 100
    assert a.total_items is None
    a2 = p.parse_args(["--page-size", "40", "--total-items", "7"])
    assert a2.page_size == 40
    assert a2.total_items == 7


def test_pagination_from_args_clamps():
    over = argparse.Namespace(page_size=500, total_items=999)
    assert endpoint.pagination_from_args(over, page_max=100) == (100, 999)  # clamped
    under = argparse.Namespace(page_size=50, total_items=None)
    assert endpoint.pagination_from_args(under, page_max=100) == (50, None)


class ClashingEndpoint(FakeEndpoint):
    """A plugin endpoint whose name a built-in already has."""

    name = "asset-owners"


type Install = Callable[..., None]


@pytest.fixture
def install_plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[Install]:
    """Fake the installed `sonde.endpoints` entry points, registering into a copy."""
    monkeypatch.setattr(endpoint, "_REGISTRY", dict(endpoint._REGISTRY))
    endpoint._load_plugins.cache_clear()

    def install(*values: str) -> None:
        eps = [EntryPoint(f"plugin{i}", v, "sonde.endpoints") for i, v in enumerate(values)]

        def entry_points(group: str) -> list[EntryPoint]:
            return eps

        monkeypatch.setattr(endpoint, "entry_points", entry_points)

    yield install
    endpoint._load_plugins.cache_clear()


def test_plugin_endpoint_is_registered(install_plugins: Install):
    install_plugins("tests.helpers:FakeEndpoint")
    assert endpoint.get("fake-test") is FakeEndpoint


def test_plugin_already_registered_by_its_decorator_is_accepted(install_plugins: Install):
    install_plugins("sonde.endpoints.asset_owners:AssetOwnersEndpoint")
    assert endpoint.get("asset-owners") is AssetOwnersEndpoint


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("tests.no_such_module:Endpoint", "'plugin0' .* failed to load"),
        ("tests.helpers:make_probe", "'plugin0' .* is not an Endpoint subclass"),
        ("tests.test_endpoint:ClashingEndpoint", "'plugin0' .*duplicate endpoint name"),
    ],
)
def test_broken_plugin_raises_naming_it(install_plugins: Install, value: str, error: str):
    install_plugins(value)
    with pytest.raises(PluginError, match=error):
        endpoint.all_endpoints()


def test_cli_exits_2_on_a_broken_plugin(
    install_plugins: Install, capsys: pytest.CaptureFixture[str]
):
    install_plugins("tests.no_such_module:Endpoint")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 2
    assert "sonde: entry point 'plugin0'" in capsys.readouterr().err

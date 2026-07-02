"""Tests for the GitHub stargazers endpoint — page-number requests and, crucially,
HEADER-based (Link) pagination via the full response object."""

import argparse

from sonde.endpoints.github_stargazers import (
    GitHubStargazersEndpoint,
    _next_page_from_link,
)
from tests.helpers import FakeResp


LINK_WITH_NEXT = (
    "<https://api.github.com/repositories/1/stargazers?per_page=100&page=2>; "
    'rel="next", '
    "<https://api.github.com/repositories/1/stargazers?per_page=100&page=34>; "
    'rel="last"'
)
LINK_LAST_PAGE = (
    "<https://api.github.com/repositories/1/stargazers?per_page=100&page=33>; "
    'rel="prev", '
    "<https://api.github.com/repositories/1/stargazers?per_page=100&page=1>; "
    'rel="first"'
)


def test_link_parse_next():
    assert _next_page_from_link(LINK_WITH_NEXT) == 2


def test_link_parse_no_next():
    assert _next_page_from_link(LINK_LAST_PAGE) is None
    assert _next_page_from_link(None) is None
    assert _next_page_from_link("") is None


def test_build_request_page_numbers():
    ep = GitHubStargazersEndpoint(owner="anthropics", repo="x", per_page=100)
    spec = ep.build_request(None)
    assert spec.url.endswith("/repos/anthropics/x/stargazers")
    assert spec.params == {"per_page": 100, "page": 1}
    assert ep.build_request(7).params["page"] == 7


def test_parse_page_reads_link_header():
    ep = GitHubStargazersEndpoint(owner="a", repo="b")
    resp = FakeResp(200, headers={"Link": LINK_WITH_NEXT}, body=[{"login": "u"}] * 100)
    page = ep.parse_page(resp)
    assert page.count == 100
    assert page.next_cursor == 2  # pagination pulled from the header, not body


def test_parse_page_last_page_no_cursor():
    ep = GitHubStargazersEndpoint(owner="a", repo="b")
    resp = FakeResp(200, headers={"Link": LINK_LAST_PAGE}, body=[{"login": "u"}] * 12)
    page = ep.parse_page(resp)
    assert page.count == 12
    assert page.next_cursor is None


def test_uses_github_provider():
    assert GitHubStargazersEndpoint(owner="a", repo="b").provider().name == "github"


def test_from_args():
    ns = argparse.Namespace(owner="anthropics", repo="sdk", total=9000, per_page=50)
    ep = GitHubStargazersEndpoint.from_args(ns)
    assert ep.owner == "anthropics" and ep.repo == "sdk"
    assert ep.total_items() == 9000 and ep.per_page == 50

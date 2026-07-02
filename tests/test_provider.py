"""Tests for the Provider abstraction: classification, rate-limit parsing, auth —
for the generic, Roblox, and GitHub providers."""

import time

from sonde.core import RClass
from sonde.provider import Provider, RobloxProvider, GitHubProvider
from tests.helpers import FakeResp, RLH_420, RLH_15


# --------------------------------------------------------------------------- #
# Generic provider (== Roblox classification + IETF header parsing)
# --------------------------------------------------------------------------- #
def test_generic_classify():
    p = Provider()
    assert p.classify(FakeResp(200)) == RClass.OK
    assert p.classify(FakeResp(429)) == RClass.THROTTLED
    assert p.classify(FakeResp(500)) == RClass.ERROR
    assert p.classify(FakeResp(403)) == RClass.ERROR  # plain 403 is not throttle here


def test_ietf_parse_420():
    rl = Provider().parse_rate_limit(RLH_420)
    assert (rl["limit"], rl["window_s"]) == (420, 60)
    assert rl["remaining"] == 419 and rl["reset_s"] == 2


def test_ietf_parse_15():
    rl = Provider().parse_rate_limit(RLH_15)
    assert (rl["limit"], rl["window_s"]) == (15, 60)
    assert rl["remaining"] == 14 and rl["reset_s"] == 21


def test_ietf_parse_absent():
    assert Provider().parse_rate_limit({"server": "x"}) == {}


def test_ietf_smallest_window_binds():
    rl = Provider().parse_rate_limit({"x-ratelimit-limit": "1000;w=3600, 50;w=60, 100;w=600"})
    assert (rl["limit"], rl["window_s"]) == (50, 60)


def test_ietf_no_window():
    rl = Provider().parse_rate_limit({"x-ratelimit-limit": "500, 999"})
    assert rl["window_s"] is None and rl["limit"] == 500


# --------------------------------------------------------------------------- #
# Roblox provider
# --------------------------------------------------------------------------- #
def test_roblox_auth_cookie_and_bearer(monkeypatch):
    monkeypatch.setenv("ROBLOX_COOKIE", "SEKRET")
    monkeypatch.setenv("ROBLOX_BEARER", "TOK")
    h = RobloxProvider().auth_headers()
    assert h["Cookie"] == ".ROBLOSECURITY=SEKRET"
    assert h["Authorization"] == "Bearer TOK"


def test_roblox_auth_anonymous(monkeypatch):
    monkeypatch.delenv("ROBLOX_COOKIE", raising=False)
    monkeypatch.delenv("ROBLOX_BEARER", raising=False)
    assert RobloxProvider().auth_headers() == {}


def test_roblox_uses_ietf_parse():
    # Roblox inherits the generic IETF parser unchanged
    assert RobloxProvider().parse_rate_limit(RLH_420)["window_s"] == 60


# --------------------------------------------------------------------------- #
# GitHub provider
# --------------------------------------------------------------------------- #
def test_github_classify_403_throttle():
    p = GitHubProvider()
    assert p.classify(FakeResp(200)) == RClass.OK
    assert p.classify(FakeResp(429)) == RClass.THROTTLED
    # 403 with remaining 0 -> throttled (GitHub's primary limit)
    assert p.classify(FakeResp(403, headers={"x-ratelimit-remaining": "0"})) == RClass.THROTTLED
    # 403 with Retry-After -> throttled (secondary limit)
    assert p.classify(FakeResp(403, headers={"Retry-After": "60"})) == RClass.THROTTLED
    # plain 403 (e.g. genuinely forbidden) -> error, NOT throttle
    assert p.classify(FakeResp(403, headers={"x-ratelimit-remaining": "42"})) == RClass.ERROR


def test_github_parse_epoch_reset():
    now = int(time.time())
    hdr = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": str(now + 1800),  # epoch, 30 min out
    }
    rl = GitHubProvider().parse_rate_limit(hdr)
    assert rl["limit"] == 5000 and rl["remaining"] == 4999
    assert rl["window_s"] == 3600  # injected known default
    assert 1795 <= rl["reset_s"] <= 1800  # epoch converted to seconds-until


def test_github_parse_custom_window():
    rl = GitHubProvider(window_s=60).parse_rate_limit(
        {"x-ratelimit-limit": "30", "x-ratelimit-reset": str(int(time.time()) + 30)}
    )
    assert rl["window_s"] == 60


def test_github_auth_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    h = GitHubProvider().auth_headers()
    assert h["Authorization"] == "Bearer ghp_xxx"
    assert h["Accept"] == "application/vnd.github+json"

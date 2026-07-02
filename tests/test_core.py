"""Tests for core: budget, session, response handling, fetch. (Rate-limit header
parsing and auth moved to the Provider — see test_provider.py.)"""

import threading

from sonde import core
from sonde.core import RClass
from tests.helpers import FakeResp, FakeEndpoint


# --------------------------------------------------------------------------- #
# RClass defaulting
# --------------------------------------------------------------------------- #
def test_default_rclass():
    assert core.default_rclass(200) == RClass.OK
    assert core.default_rclass(429) == RClass.THROTTLED
    assert core.default_rclass(-1) == RClass.BUDGET
    assert core.default_rclass(500) == RClass.ERROR


def test_result_derives_rclass_from_status():
    assert core.Result(200, 0.0).rclass == RClass.OK
    assert core.Result(429, 0.0).rclass == RClass.THROTTLED
    # explicit rclass wins
    assert core.Result(403, 0.0, rclass=RClass.THROTTLED).rclass == RClass.THROTTLED


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def test_budget_basic():
    b = core.Budget(max_requests=3)
    assert [b.take() for _ in range(4)] == [True, True, True, False]
    assert b.used == 3 and b.remaining() == 0


def test_budget_thread_safe():
    b = core.Budget(max_requests=1000)

    def worker():
        while b.take():
            pass

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.used == 1000


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def test_build_session_pool_and_cookie_policy():
    s = core.build_session(
        max_conns=80, headers={"Cookie": ".ROBLOSECURITY=X", "Accept": "application/json"}
    )
    adapter = s.get_adapter("https://inventory.roblox.com")
    assert adapter._pool_maxsize == 80
    assert s.headers["Cookie"] == ".ROBLOSECURITY=X"
    assert s.cookies.get_policy().__class__.__name__ == "DefaultCookiePolicy"


def test_build_session_min_pool():
    s = core.build_session(max_conns=2)
    assert s.get_adapter("https://x")._pool_maxsize == 10


# --------------------------------------------------------------------------- #
# _parse_response (uses the endpoint's provider to classify)
# --------------------------------------------------------------------------- #
def test_parse_response_ok():
    resp = FakeResp(
        200,
        headers={"x-ratelimit-remaining": "5"},
        body={"data": [1, 2, 3], "nextPageCursor": "abc"},
    )
    res = core._parse_response(resp, 0.1, FakeEndpoint())
    assert res.rclass == RClass.OK
    assert res.count == 3 and res.next_cursor == "abc"


def test_parse_response_throttled():
    resp = FakeResp(429, headers={"Retry-After": "5"})
    res = core._parse_response(resp, 0.1, FakeEndpoint())
    assert res.rclass == RClass.THROTTLED
    assert res.retry_after == 5.0


def test_parse_response_bad_json_is_ok_but_flagged():
    resp = FakeResp(200, body=None)  # .json() raises
    res = core._parse_response(resp, 0.1, FakeEndpoint())
    assert res.rclass == RClass.OK
    assert "parse_page failed" in res.error


def test_parse_response_error_captures_text():
    resp = FakeResp(500, text="boom")
    res = core._parse_response(resp, 0.1, FakeEndpoint())
    assert res.rclass == RClass.ERROR
    assert "boom" in res.error


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def test_fetch_budget_exhausted():
    res = core.fetch(session=None, endpoint=FakeEndpoint(), cursor=None, budget=core.Budget(0))
    assert res.rclass == RClass.BUDGET


def test_fetch_wires_endpoint_request():
    captured = {}

    class FakeSession:
        def request(self, method, url, params=None, json=None, timeout=None):
            captured.update(method=method, url=url, params=params)
            return FakeResp(200, body={"data": [1], "nextPageCursor": None})

    res = core.fetch(FakeSession(), FakeEndpoint(), cursor="CUR", budget=core.Budget(5))
    assert res.rclass == RClass.OK and res.count == 1
    assert captured["method"] == "GET"
    assert captured["params"]["cursor"] == "CUR"

"""
endpoints/github_stargazers.py — a non-Roblox endpoint, to prove the tool generalises.

    GET https://api.github.com/repos/{owner}/{repo}/stargazers?per_page=100&page=N

Exercises the parts Roblox doesn't:
  * GitHubProvider — throttles with 403 (+ x-ratelimit-remaining: 0), epoch reset.
  * Token auth via GITHUB_TOKEN (Authorization header).
  * HEADER-based pagination — the next page comes from the `Link` response header,
    not the body, so parse_page reads response.headers.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from ..endpoint import Endpoint, PageResult, RequestSpec, register
from ..provider import GitHubProvider, Provider


def _next_page_from_link(link_header: str | None) -> int | None:
    """Extract the `page` number of the rel="next" URL from a GitHub Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.split(";")
        if len(seg) < 2:
            continue
        url = seg[0].strip().strip("<>")
        if 'rel="next"' in part:
            m = re.search(r"[?&]page=(\d+)", url)
            if m:
                return int(m.group(1))
    return None


@register
class GitHubStargazersEndpoint(Endpoint):
    name = "github-stargazers"
    help = "api.github.com/repos/{owner}/{repo}/stargazers — users who starred a repo"

    BASE = "https://api.github.com/repos/{owner}/{repo}/stargazers"
    MAX_PAGE = 100

    def __init__(
        self,
        owner: str,
        repo: str,
        total: int | None = None,
        per_page: int = 100,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self._total = total
        self.per_page = min(per_page, self.MAX_PAGE)

    def _make_provider(self) -> Provider:
        return GitHubProvider()

    @classmethod
    def add_arguments(cls, p: argparse.ArgumentParser) -> None:
        p.add_argument("--owner", required=True, help="repo owner/org, e.g. 'anthropics'")
        p.add_argument("--repo", required=True, help="repo name, e.g. 'anthropic-sdk-python'")
        p.add_argument("--total", type=int, default=None, help="known stargazer count")
        p.add_argument("--per-page", type=int, default=100, help="items per page (max 100)")

    @classmethod
    def from_args(cls, a: argparse.Namespace) -> GitHubStargazersEndpoint:
        return cls(owner=a.owner, repo=a.repo, total=a.total, per_page=a.per_page)

    def build_request(self, cursor: Any) -> RequestSpec:
        page = cursor or 1  # GitHub uses page-number pagination
        return RequestSpec(
            url=self.BASE.format(owner=self.owner, repo=self.repo),
            params={"per_page": self.per_page, "page": page},
        )

    def parse_page(self, response: Any) -> PageResult:
        data = response.json()
        count = len(data) if isinstance(data, list) else len(data.get("items", []))
        next_cursor = _next_page_from_link(response.headers.get("Link"))
        return PageResult(count=count, next_cursor=next_cursor)

    def total_items(self) -> int | None:
        return self._total

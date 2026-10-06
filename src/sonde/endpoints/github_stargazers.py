"""endpoints/github_stargazers.py — a non-Roblox endpoint, to prove the tool generalises.

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
from typing import Any, Self, override

from sonde.endpoint import (
    Endpoint,
    PageResult,
    RequestSpec,
    add_pagination_args,
    pagination_from_args,
    register,
)
from sonde.provider import GitHubProvider, Provider


def _next_page_from_link(link_header: str | None) -> int | None:
    """Extract the `page` number of the rel="next" URL from a GitHub Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        url, sep, _ = part.partition(";")
        if not sep:
            continue
        url = url.strip().strip("<>")
        if 'rel="next"' in part:
            m = re.search(r"[?&]page=(\d+)", url)
            if m:
                return int(m.group(1))
    return None


@register
class GitHubStargazersEndpoint(Endpoint):
    """The users who starred one repository, paged by the Link header."""

    name = "github-stargazers"
    help = "api.github.com/repos/{owner}/{repo}/stargazers — users who starred a repo"

    BASE = "https://api.github.com/repos/{owner}/{repo}/stargazers"
    MAX_PAGE = 100

    def __init__(
        self,
        owner: str,
        repo: str,
        total_items: int | None = None,
        page_size: int = 100,
    ) -> None:
        """Set up the probe for one repository.

        Args:
            owner: The repository's owner or organization.
            repo: The repository's name.
            total_items: The known star count, for the wall-clock estimate.
            page_size: Users per page, capped at `MAX_PAGE`.
        """
        self.owner = owner
        self.repo = repo
        self._total = total_items
        self.page_size = min(page_size, self.MAX_PAGE)

    @override
    def _make_provider(self) -> Provider:
        return GitHubProvider()

    @override
    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--owner", required=True, help="repo owner/org, e.g. 'anthropics'")
        parser.add_argument("--repo", required=True, help="repo name, e.g. 'anthropic-sdk-python'")
        add_pagination_args(parser, page_max=cls.MAX_PAGE)

    @override
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Self:
        page_size, total_items = pagination_from_args(args, page_max=cls.MAX_PAGE)
        return cls(owner=args.owner, repo=args.repo, total_items=total_items, page_size=page_size)

    @override
    def build_request(self, cursor: Any) -> RequestSpec:
        page = cursor or 1  # GitHub uses page-number pagination
        return RequestSpec(
            url=self.BASE.format(owner=self.owner, repo=self.repo),
            params={"per_page": self.page_size, "page": page},
        )

    @override
    def parse_page(self, response: Any) -> PageResult:
        data: list[Any] | dict[str, Any] = response.json()
        count = len(data) if isinstance(data, list) else len(data.get("items", ()))
        next_cursor = _next_page_from_link(response.headers.get("Link"))
        return PageResult(count=count, next_cursor=next_cursor)

    @override
    def total_items(self) -> int | None:
        return self._total

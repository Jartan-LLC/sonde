# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

Initial release.

### Added

- Five-phase probe pipeline — sanity, sequential, burst, sweep, and estimate —
  that measures an HTTP API's rate limit, burst ceiling, recovery window, and
  fastest sustainable request interval, then combines them into a recommended
  interval and a full-scrape wall-clock estimate.
- Pluggable endpoint framework: subclass `Endpoint`, decorate with `@register`,
  and the endpoint becomes a CLI subcommand.
- Two built-in endpoints: `asset-owners` (Roblox collectible owners) and
  `github-stargazers` (GitHub repository stargazers).
- Provider abstraction for parsing rate-limit response headers, with a generic
  200/429 + IETF-header provider and GitHub/Roblox specializations.
- CLI with endpoint-agnostic probe options (`--max-requests`, `--seq-cap`,
  burst/recovery/sweep tuning, `--margin`) and per-endpoint options.
- Concurrent burst phase driven by `httpx` on a single asyncio event loop, with
  adaptive geometric-backoff measurement of the throttle recovery window.
- JSON report output to a file or stdout (`--output -`).
- Structured logging subsystem with `plain` and `json` formats and `-v`/`-q`
  verbosity control; logs go to stderr, the report to `--output`.
- Type annotations across the public API, with a PEP 561 `py.typed` marker so
  downstream type checkers see them.
- Public extension API re-exported from the top-level `sonde` package
  (`Endpoint`, `RequestSpec`, `PageResult`, `register`, `Provider`).
- Defined process exit codes: `0` success, `2` precondition failure (bad
  arguments, unwritable `--output`, or an unusable endpoint response), `1`
  unexpected crash, `130` interrupted.
- Redaction of configured credentials from log output.
- Docker image and PyPI packaging.

[Unreleased]: https://github.com/Jartan-LLC/sonde/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Jartan-LLC/sonde/releases/tag/v0.1.0

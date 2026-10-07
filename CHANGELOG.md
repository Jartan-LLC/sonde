# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-07

### Added

- Python 3.14 is tested and listed as supported.
- Endpoints from installed packages: a package that declares an `Endpoint` subclass under
  the `sonde.endpoints` entry-point group adds a `sonde` subcommand (see the README's
  "Adding an Endpoint"). If an installed plugin fails to load, every `sonde` command exits
  2 with an error naming it.

### Changed

- `requests` is capped at `<3` and `httpx` at `<1`.
- Building the image from a checkout needs BuildKit, Docker's default builder since 23.0.
- The `latest` image tag moves only for the highest release, so a patch to an older
  release line doesn't take it.

### Removed

- The `0` image tag. It stays on 0.1.0: pull `0.2`, a full version, or `latest`.

### Fixed

- A burst size that repeats the last one in `--burst-sizes`, such as the first 50 in
  `10,50,50`, gets its cooldown.
- The sweep's floor message no longer prints `Nones` when the first interval tried
  throttles.
- `--burst-sizes` and `--sweep-intervals` reject zero and negative values, which crashed
  the probe or produced a meaningless report row.
- The startup `Auth` line no longer says `credentials set` for a `github-stargazers` run
  without `GITHUB_TOKEN`.

### Security

- A credential echoed in a response header is redacted in the report too
  (`sanity.headers` and `ratelimit_headers.raw`), not only in the logs.
- An error message cut at its 200-character limit no longer keeps part of a credential.
- An echoed credential is redacted even when the response's charset or a JSON encoder
  altered it, unless it was JSON-escaped twice, as in JSON nested inside a JSON string.
- A credential echoed in a UTF-16 or UTF-32 body is no longer redacted. 0.1.0 redacted it
  when the response declared that charset.

## [0.1.0] - 2026-07-02

Initial release.

### Added

- Five-phase probe pipeline — sanity, sequential, burst, sweep, and estimate —
  that measures an HTTP API's rate limit, burst ceiling, recovery window, and
  fastest sustainable request interval, then combines them into a recommended
  interval and a full-scrape wall-clock estimate.
- Pluggable endpoint framework: subclass `Endpoint`, decorate with `@register`,
  and the endpoint becomes a CLI subcommand. Paginated endpoints share the
  `--page-size` / `--total-items` flags via `add_pagination_args`.
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
  (`Endpoint`, `RequestSpec`, `PageResult`, `register`, `Provider`,
  `add_pagination_args`, `pagination_from_args`).
- Defined process exit codes: `0` success, `2` precondition failure (bad
  arguments, unwritable `--output`, or an unusable endpoint response), `1`
  unexpected crash, `130` interrupted.
- Redaction of configured credentials from log output.
- Docker image and PyPI packaging.

[Unreleased]: https://github.com/Jartan-LLC/sonde/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Jartan-LLC/sonde/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Jartan-LLC/sonde/releases/tag/v0.1.0

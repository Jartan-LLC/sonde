# Sonde

[![PyPI](https://img.shields.io/pypi/v/sonde)](https://pypi.org/project/sonde/)
[![CI](https://github.com/Jartan-LLC/sonde/actions/workflows/ci.yml/badge.svg)](https://github.com/Jartan-LLC/sonde/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Probe any HTTP API for its rate limits, burst ceiling, and full-scrape time. Provider-pluggable, safe by default.

## Install

```bash
pip install sonde
```

With optional async burst support (httpx):

```bash
pip install 'sonde[httpx]'
```

From source:

```bash
git clone https://github.com/Jartan-LLC/sonde.git
cd sonde
pip install -e .
```

Docker:

```bash
docker build -t sonde .
```

## Quick Start

Probe the Roblox asset-owners endpoint:

```bash
export ROBLOX_COOKIE="your_roblosecurity_cookie"
sonde asset-owners --asset-id 20573078 --total-copies 1470000
```

Probe GitHub stargazers:

```bash
export GITHUB_TOKEN="ghp_..."
sonde github-stargazers --owner anthropics --repo anthropic-sdk-python --total 5000
```

Anonymous probing (no auth) works too -- you'll just hit lower rate limits:

```bash
sonde github-stargazers --owner torvalds --repo linux --total 190000
```

Results are written to `sonde_report.json` by default:

```bash
sonde asset-owners --asset-id 20573078 --output my_report.json
```

## How It Works

Sonde runs five phases against the target endpoint, then combines the measurements into a safe rate estimate.

| Phase | What it does |
|---|---|
| **Sanity** | One request. Validates auth, reads rate-limit response headers (e.g. `x-ratelimit-limit`, `x-ratelimit-remaining`), and records items-per-page for the scrape-time estimate. |
| **Sequential** | Fires back-to-back requests (up to `--seq-cap`, default 150) until the first 429 or the cap. Measures baseline throughput and how many requests the API allows before throttling. |
| **Burst** | Fires N truly-concurrent requests (default sizes: 10, 20, 40, 80) using a thread pool (or async httpx with `--use-httpx`). After the first throttled burst, measures the **recovery window** -- how long until requests succeed again -- via adaptive geometric backoff. |
| **Sweep** | Drains the rate-limit bucket, then paces requests at progressively faster intervals (default: 8s down to 0.15s) to find the fastest sustainable interval from empty. Skipped by default when authoritative rate-limit headers are present (override with `--force-sweep`). |
| **Estimate** | Combines all measurements into a recommended request interval and, if a total item count is known, a wall-clock full-scrape estimate. |

### How the estimate is produced

The estimate phase uses a priority ladder to determine the safe rate:

1. **Authoritative headers** -- If the API returned `x-ratelimit-limit` and a window, use those directly (e.g. 100 requests per 60s).
2. **Swept floor** -- If the sweep found a fastest sustainable interval, use that.
3. **Token-bucket inference** -- If burst results show a clean burst size and a measured recovery window, infer the bucket rate.
4. **Sequential fallback** -- Use the observed sequential throughput before the first 429.

The final recommended interval applies a safety margin (default 80%, configurable with `--margin`), meaning the recommended pace is ~25% slower than the measured ceiling.

## Endpoints

### asset-owners

Roblox `inventory.roblox.com/v2/assets/{id}/owners` -- paginated list of owners of a collectible asset.

| Option | Required | Default | Description |
|---|---|---|---|
| `--asset-id` | Yes | -- | Asset ID to probe (e.g. `20573078`) |
| `--total-copies` | No | None | Known total owners, for wall-clock estimate |
| `--page-size` | No | 100 | Items per page (capped at 100) |
| `--sort-order` | No | Asc | `Asc` or `Desc` |

**Auth:** Set `ROBLOX_COOKIE` (legacy web-session) and/or `ROBLOX_BEARER` (Open Cloud) environment variables.

### github-stargazers

GitHub `api.github.com/repos/{owner}/{repo}/stargazers` -- users who starred a repository.

| Option | Required | Default | Description |
|---|---|---|---|
| `--owner` | Yes | -- | Repository owner/org (e.g. `anthropics`) |
| `--repo` | Yes | -- | Repository name (e.g. `anthropic-sdk-python`) |
| `--total` | No | None | Known stargazer count, for wall-clock estimate |
| `--per-page` | No | 100 | Items per page (max 100) |

**Auth:** Set `GITHUB_TOKEN` environment variable. Without it, you get the anonymous rate limit (60 requests/hour).

## Adding an Endpoint

1. Create a new module in `src/sonde/endpoints/`.
2. Subclass `Endpoint` and implement `build_request(cursor)` and `parse_page(response)`.
3. Decorate with `@register` and set a unique `name` (becomes the CLI subcommand).
4. Override `_make_provider()` to return the appropriate `Provider` (or use the generic one for standard 200/429 + IETF headers).
5. Optionally implement `total_items()` for scrape-time estimates, `add_arguments()` / `from_args()` for CLI options, and `extra_headers()` for endpoint-specific headers.
6. Import the new module in `src/sonde/endpoints/__init__.py` so it registers on package load.

Minimal example:

```python
from sonde.endpoint import Endpoint, RequestSpec, PageResult, register

@register
class MyEndpoint(Endpoint):
    name = "my-endpoint"
    help = "one-line description for --help"

    def build_request(self, cursor):
        return RequestSpec(url="https://api.example.com/items", params={"page": cursor or 1})

    def parse_page(self, response):
        data = response.json()
        return PageResult(count=len(data["items"]), next_cursor=data.get("next_page"))
```

## CLI Reference

Common options shared by all endpoints:

| Option | Default | Description |
|---|---|---|
| `--max-requests` | 1200 | Hard global cap across all phases (safety budget) |
| `--seq-cap` | 150 | Max sequential requests before stopping |
| `--skip-burst` | off | Skip the concurrent burst phase |
| `--use-httpx` | off | Use async httpx for bursts instead of threaded requests (requires `pip install 'sonde[httpx]'`; falls back to threaded if missing) |
| `--burst-sizes` | `10,20,40,80` | Comma-separated list of concurrent burst sizes |
| `--burst-cooldown` | 60.0 | Fallback seconds between bursts if the recovery window can't be measured |
| `--recovery-step` | 0.25 | Initial poll delay when measuring the throttle window (grows geometrically) |
| `--recovery-max` | 90.0 | Give up measuring the window after this many seconds |
| `--recovery-polls` | 15 | Max polls during recovery measurement |
| `--skip-sweep` | off | Skip the sustained-interval sweep phase |
| `--force-sweep` | off | Run the sweep even when authoritative rate-limit headers are present |
| `--sweep-intervals` | `8,5,3,2,1.2,0.6,0.3,0.15` | Inter-request intervals (seconds) to test, slow to fast |
| `--sweep-count` | 20 | Paced requests per interval after draining |
| `--sweep-drain` | 500 | Cap on rapid requests used to empty the bucket before each interval |
| `--sweep-tolerance` | 0.1 | Max fraction of 429s for an interval to count as sustainable |
| `--margin` | 0.8 | Safety margin: pace at 80% of the measured max rate (0.8 = 25% slower than ceiling) |
| `--output` | `sonde_report.json` | Path for the JSON report (use `-` for stdout) |
| `-v` / `--verbose` | off | Show per-request detail (sets log level to DEBUG) |
| `-q` / `--quiet` | off | Only show warnings and errors (sets log level to WARNING) |
| `--log-format` | `plain` | Log line format: `plain` (message-only) or `json` (structured) |

`-v` and `-q` are mutually exclusive. Logs always go to stderr; the report goes to `--output`.

### Piping and machine-readable output

Use `--output -` to write the JSON report to stdout instead of a file. Combine with `-q` to suppress INFO-level log noise on stderr:

```bash
sonde asset-owners --asset-id 20573078 --output - -q | jq .estimate
```

Use `--log-format json` for structured log lines on stderr (keys: `timestamp`, `level`, `logger`, `message`, plus `exc` on error lines), useful for log aggregators or CI pipelines:

```bash
sonde asset-owners --asset-id 20573078 --log-format json 2>sonde.log
```

## Docker

Build:

```bash
docker build -t sonde .
```

Run (mount current directory so the report lands on the host):

```bash
docker run --rm -v "$(pwd):/data" -e ROBLOX_COOKIE sonde \
    asset-owners --asset-id 20573078 --total-copies 1470000
```

```bash
docker run --rm -v "$(pwd):/data" -e GITHUB_TOKEN sonde \
    github-stargazers --owner anthropics --repo anthropic-sdk-python --total 5000
```

The container writes `sonde_report.json` to `/data` (the mounted volume).

## Development

```bash
pip install -e '.[dev]'
```

Run tests and linting:

```bash
pytest
ruff check .
ruff format --check .
```

## License

[MIT](LICENSE)

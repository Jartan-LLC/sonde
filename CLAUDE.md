# Sonde

Probe any HTTP API for its rate limits, burst ceiling, and full-scrape time. Python CLI tool, Docker, PyPI package.

## Rules

The project rules live in `GUARDRAILS.md`, ranked by how firmly each holds; this import
loads them into every session:

@GUARDRAILS.md

## Verify

Run `make check` before declaring work done — it runs CI's lint, test, build and audit
checks:

```bash
make check
```

Individual targets (`make lint`, `make test`, …) speed up the inner loop; `make help`
lists them.

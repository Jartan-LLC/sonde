# Contributing

## Setup

```bash
uv pip install -e '.[dev]'
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/),
which is this project's installer, resolver and build frontend in place of pip. Run it
inside a virtualenv (`uv venv && source .venv/bin/activate`), or pass `--system` to
install into the active interpreter -- uv refuses a non-venv target otherwise.

## Verify before opening a PR

```bash
ruff check .
ruff format --check .
pytest
```

CI runs the same checks plus a Docker build and a `uv build` / `twine check` of the
distribution. All must pass before merge.

## Adding an endpoint

See [Adding an Endpoint](README.md#adding-an-endpoint) in the README. In short: subclass
`Endpoint`, decorate with `@register`, and import the module in `src/sonde/endpoints/__init__.py`.

## Conventions

- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
  `docs:`, `refactor:`, `chore:`).
- Public API changes and behavior changes go in `CHANGELOG.md` under `## [Unreleased]`.
- Report security issues privately via [SECURITY.md](.github/SECURITY.md), not a public issue.

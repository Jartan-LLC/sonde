# uv installs the package here, same as in CI and the devcontainer. It arrives
# as a build stage rather than the shorter
# `COPY --from=ghcr.io/astral-sh/uv:0.12.5` because Dependabot's Dockerfile
# parser only reads `FROM` lines -- an inline COPY reference is a pin nobody
# bumps. Named `uv-bin` so it does not collide with the `uv` binary in a later RUN.
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv-bin

FROM python:3.12-slim AS base
COPY --from=uv-bin /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# `--system` because the container is the isolation; no venv needed inside it.
RUN uv pip install --system --no-cache .

RUN useradd --create-home sonde
USER sonde

WORKDIR /data

ENTRYPOINT ["sonde"]

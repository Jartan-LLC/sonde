# Build stage, not an inline COPY --from: Dependabot only parses FROM lines.
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv-bin

FROM python:3.12-slim AS base
COPY --from=uv-bin /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN uv pip install --system --no-cache .

RUN useradd --create-home sonde
USER sonde

WORKDIR /data

ENTRYPOINT ["sonde"]

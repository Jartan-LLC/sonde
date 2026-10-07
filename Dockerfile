# Build stage, not an inline COPY --from: Dependabot only parses FROM lines.
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv-bin

FROM python:3.12-slim AS base

# Unbuffered stdout, so output reaches docker logs at once and a killed process loses none.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# Mounted for this step only, so uv stays out of the published image.
RUN --mount=from=uv-bin,source=/uv,target=/bin/uv uv pip install --system --no-cache .

RUN useradd --create-home --uid 1000 sonde
USER sonde

WORKDIR /data

ENTRYPOINT ["sonde"]

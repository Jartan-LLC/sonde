FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir '.[httpx]'

RUN useradd --create-home sonde
USER sonde

WORKDIR /data

ENTRYPOINT ["sonde"]

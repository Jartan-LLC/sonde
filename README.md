# Sonde

Probe any HTTP API for its rate limits, burst ceiling, and full-scrape time. Provider-pluggable, safe by default.

## Install

```bash
pip install sonde
```

Or from source:

```bash
git clone https://github.com/Jartan-LLC/sonde.git
cd sonde
pip install -e .
```

## Usage

```bash
sonde <endpoint> [options]
```

## Development

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
```

## License

[MIT](LICENSE)

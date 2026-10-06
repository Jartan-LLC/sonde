# Getting started

## Install

Requires Python 3.12+.

```bash
pip install sonde
```

## Probe an endpoint

```bash
export GITHUB_TOKEN="ghp_..."  # optional; anonymous probing hits lower limits
sonde github-stargazers --owner anthropics --repo anthropic-sdk-python --total-items 5000
```

sonde runs its phases, logs what it measures, and writes the full report to
`sonde_report.json` (`--output -` prints it instead).

The [README](https://github.com/Jartan-LLC/sonde#readme) covers each phase, the built-in
endpoints, adding your own, and every CLI option.

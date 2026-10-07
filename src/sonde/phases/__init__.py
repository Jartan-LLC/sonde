"""The generic rate-limit probing engine, split into modules by phase.

Every probing phase works against a `Probe` and drives its endpoint through
`core.fetch` (or, for the concurrent burst, an async httpx client); the estimate does
no I/O and works from the `Measurements`. Nothing here knows about any specific
endpoint. The phases, by module:

- `sequential`: sanity (one request, reading auth and the rate-limit headers), then
  back-to-back requests until throttled.
- `burst`: bursts of concurrent requests, and the recovery window after the first
  throttle.
- `sweep`: the fastest sustained interval that stays unthrottled (a fallback).
- `estimate`: a safe rate and wall-clock estimate from the measurements.

`probe` holds what the probing phases share.
"""

from sonde.phases.burst import BurstConfig, BurstRow, phase_burst
from sonde.phases.estimate import Measurements, phase_estimate
from sonde.phases.probe import Probe
from sonde.phases.sequential import SequentialSummary, phase_sanity, phase_seq
from sonde.phases.sweep import SweepConfig, SweepRow, phase_sweep

__all__ = [
    "BurstConfig",
    "BurstRow",
    "Measurements",
    "Probe",
    "SequentialSummary",
    "SweepConfig",
    "SweepRow",
    "phase_burst",
    "phase_estimate",
    "phase_sanity",
    "phase_seq",
    "phase_sweep",
]

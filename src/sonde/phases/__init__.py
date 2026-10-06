"""The generic rate-limit probing engine, in a module per phase.

Every probing phase works against a `Probe` and drives its endpoint through
`core.fetch` (or, for the concurrent burst, an async httpx client); the estimate does
no I/O and works from the `Measurements`. Nothing here knows about any specific
endpoint. Phases:

  sanity     one request; read auth + x-ratelimit headers
  sequential back-to-back requests until the first 429
  burst      N concurrent requests (async httpx on one event loop)
  recovery   after a 429, measure how long until requests succeed again
  sweep      find the fastest sustained interval that stays 429-free (fallback)
  estimate   turn the measurements into a safe rate + wall-clock estimate
"""

from sonde.phases.burst import BurstConfig, phase_burst
from sonde.phases.estimate import Measurements, phase_estimate
from sonde.phases.probe import Probe
from sonde.phases.sequential import phase_sanity, phase_seq
from sonde.phases.sweep import SweepConfig, phase_sweep

__all__ = [
    "BurstConfig",
    "Measurements",
    "Probe",
    "SweepConfig",
    "phase_burst",
    "phase_estimate",
    "phase_sanity",
    "phase_seq",
    "phase_sweep",
]

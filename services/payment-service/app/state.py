"""Mutable fault-injection state for Part E experiments.

All of it is process-local and defaults to "no fault", so a restart of the
container is always a full recovery. `/chaos/reset` is the panic button.
"""

import threading

LOCK = threading.Lock()

DEFAULT_CHAOS = {
    # Fraction of charges given an artificial delay (0.2 == every fifth request).
    "latency_probability": 0.0,
    # How long that delay is, in seconds.
    "latency_seconds": 0.5,
    # Fraction of charges answered with HTTP 503 instead of a decision.
    "failure_rate": 0.0,
}

CHAOS = dict(DEFAULT_CHAOS)


def reset_chaos() -> dict:
    with LOCK:
        CHAOS.update(DEFAULT_CHAOS)
        return dict(CHAOS)


def snapshot() -> dict:
    with LOCK:
        return dict(CHAOS)

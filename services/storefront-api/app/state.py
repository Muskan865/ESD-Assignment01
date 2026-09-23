"""Experiment toggles owned by the storefront.

Process-local and defaulting to off, so restarting the container is a complete
recovery. Nothing here affects normal shop behaviour when left alone.
"""

import threading

from .config import CARDINALITY_DEMO_LABEL

LOCK = threading.Lock()

DEFAULT = {
    # Part E.2 - when true, every request also increments
    # demo_requests_total{request_id="..."} with a fresh id, creating one new
    # time series per request. Off by default.
    "cardinality_demo": False,
    # The assignment caps the experiment at 100 unique ids.
    "cardinality_limit": 100,
}

STATE = dict(DEFAULT)

# The actual request ids that have been used as a label value. The cap is on
# the SIZE OF THIS SET, not on a per-activation counter: what costs Prometheus
# memory is the number of distinct series, so toggling the demo off and on
# again must not be allowed to create another 100.
EMITTED_IDS: set[str] = set()


def snapshot() -> dict:
    with LOCK:
        return dict(STATE, cardinality_series=len(EMITTED_IDS),
                    cardinality_label=CARDINALITY_DEMO_LABEL)


def should_emit(request_id: str) -> bool:
    """Claim one slot in the cardinality experiment, if any are left."""
    with LOCK:
        if not STATE["cardinality_demo"]:
            return False
        if request_id in EMITTED_IDS:
            return False
        if len(EMITTED_IDS) >= STATE["cardinality_limit"]:
            return False
        EMITTED_IDS.add(request_id)
        return True


def reset() -> dict:
    """Full reset: switch the demo off AND forget the ids already used.

    Forgetting the ids is what makes the experiment re-runnable. The metric's
    label children are cleared separately by the caller.
    """
    with LOCK:
        STATE.update(DEFAULT)
        EMITTED_IDS.clear()
        return dict(STATE, cardinality_series=0,
                    cardinality_label=CARDINALITY_DEMO_LABEL)

"""Outbound HTTP to payment-service, instrumented on every call.

The Summary, the Histogram and the Counter are all recorded here, in one place,
so the outbound dependency has its own signal independent of our own status
codes: the storefront can be returning 200s while payment is degrading.
"""

import time

import requests

from .config import PAYMENT_SERVICE_URL, PAYMENT_TIMEOUT_SECONDS
from .telemetry import PAYMENT_CALL_LATENCY, PAYMENT_CALL_SUMMARY, PAYMENT_CALLS

_session = requests.Session()


class PaymentOutcome:
    APPROVED = "approved"
    DECLINED = "declined"
    ERROR = "error"
    TIMEOUT = "timeout"


def charge(order_id: int, amount_cents: int, method: str, request_id: str) -> tuple[str, dict]:
    """Charge one order. Returns (outcome, detail).

    Never raises: a dependency failure is a business outcome here, not a crash.
    """
    started = time.perf_counter()
    outcome = PaymentOutcome.ERROR
    detail: dict = {}
    try:
        response = _session.post(
            f"{PAYMENT_SERVICE_URL}/payment/charge",
            json={"order_id": order_id, "amount_cents": amount_cents, "method": method},
            # Propagating the id is what makes one checkout followable across
            # both services in Kibana.
            headers={"x-request-id": request_id},
            timeout=PAYMENT_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            outcome = PaymentOutcome.APPROVED
        elif response.status_code == 402:
            outcome = PaymentOutcome.DECLINED
        else:
            outcome = PaymentOutcome.ERROR
        detail = {"status_code": response.status_code}
    except requests.Timeout:
        outcome = PaymentOutcome.TIMEOUT
        detail = {"error": "timeout", "timeout_seconds": PAYMENT_TIMEOUT_SECONDS}
    except requests.RequestException as exc:
        outcome = PaymentOutcome.ERROR
        detail = {"error": type(exc).__name__}
    finally:
        elapsed = time.perf_counter() - started
        PAYMENT_CALL_SUMMARY.labels(outcome=outcome).observe(elapsed)
        PAYMENT_CALL_LATENCY.labels(outcome=outcome).observe(elapsed)
        PAYMENT_CALLS.labels(outcome=outcome).inc()
        detail["duration_ms"] = round(elapsed * 1000, 2)
    return outcome, detail


def post_chaos(path: str, payload: dict) -> dict:
    """Forward a fault-injection request to payment-service."""
    response = _session.post(f"{PAYMENT_SERVICE_URL}{path}", json=payload, timeout=5.0)
    response.raise_for_status()
    return response.json()


def get_chaos_status() -> dict:
    response = _session.get(f"{PAYMENT_SERVICE_URL}/chaos/status", timeout=5.0)
    response.raise_for_status()
    return response.json()

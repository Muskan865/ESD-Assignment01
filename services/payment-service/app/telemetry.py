"""Prometheus instruments owned by the payment service."""

from prometheus_client import Counter, Gauge, Histogram

from .config import LATENCY_BUCKETS

# --- application metrics ----------------------------------------------------
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests handled by this service.",
    ["method", "route", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Wall-clock duration of inbound HTTP requests.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "Requests currently being processed by this service.",
)

# --- business metrics -------------------------------------------------------
CHARGES = Counter(
    "payment_charges_total",
    "Charge attempts by outcome.",
    ["outcome"],  # bounded: approved | declined | error
)
CHARGE_AMOUNT = Histogram(
    "payment_charge_amount_dollars",
    "Value of approved charges, in dollars.",
    buckets=(5, 10, 25, 50, 100, 200, 500),
)

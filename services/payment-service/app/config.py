"""Runtime configuration for the payment service."""

import os
import socket

SERVICE_NAME = os.getenv("SERVICE_NAME", "payment-service")
INSTANCE = socket.gethostname()

# Bucket edges chosen around the behaviour we actually want to resolve:
# a healthy charge lands near 20-60 ms, and the Part E fault adds 500 ms.
# Without the 0.5/0.75 edges the injected delay would be invisible inside one
# very wide bucket and p95 would be unreadable.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0)

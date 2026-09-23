# Offline tests

Two smoke-test suites that run the services **in-process**, with no Docker and
no database, using FastAPI's `TestClient`. They exist to check the application
logic quickly — the real end-to-end verification is `shopctl verify` plus the
two experiments in `README.md`.

```bash
pip install fastapi httpx prometheus-client requests
python tests/test_payment_service.py     # 22 checks
python tests/test_storefront_api.py      # 57 checks
```

Each prints one `PASS`/`FAIL` line per check and exits non-zero on failure.
(These are the only things in this project that need `pip install`; the
`shopctl` control script is standard-library only.)

### What they cover

**`test_payment_service.py`** — health and `/metrics`; the approve/decline
split; `x-request-id` propagation; both fault injectors actually taking effect;
`/chaos/reset` restoring normal service; input validation; and that unmatched
URLs collapse to `route="unmatched"` instead of leaking raw paths into metric
labels; and that a crash is answered with a clean 500 and logged as one JSON
line carrying `error.type`, `error.message` and the whole `error.stack_trace`.

**`test_storefront_api.py`** — stubs Postgres with an in-memory fake and stubs
the payment client, then checks the whole checkout control flow: the happy path
and each failure path (out of stock, unknown SKU, declined, provider error,
timeout) with the right status code *and* the right
`shop_orders_rejected_total{reason=...}`; that every metric type is recorded
(Counter, Gauge, Histogram, Summary); that the "awaiting fulfilment" gauge goes
up on placement and back down on both cancel and fulfil; that route templates
rather than order ids reach metric labels; and the Part E.2 cardinality
behaviour — the cap holds at 100 **distinct** ids even across toggling, the
bounded counter stays at 2 series, `reset` clears the label children so the
experiment can be re-run, and the same counter built with the label removed
(`CARDINALITY_DEMO_LABEL=none`) gives exactly one series for 100 requests.

It also covers the fixes that came out of Part E.1: every statement that locks
product rows (checkout, cancel, restock) locks them `ORDER BY sku`; a full
connection pool makes a request wait and then raise `PoolTimeout` rather than
fail instantly, and releases its slot afterwards; `PoolTimeout` becomes a 503
with a `database busy` log; and an unhandled exception becomes a 500 with one
structured `unhandled exception` log that keeps the request id.

### What they do not cover

The SQL itself is stubbed, so these tests cannot catch a syntax error or a bad
query plan. Prometheus scraping, Filebeat parsing, Elasticsearch indexing and
the Grafana dashboard are all out of scope here — those are verified by running
the stack.

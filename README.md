# Tiny Shop — Assignment 1: Observability

**Enterprise Software Development, Fall 2026**

A very small e-commerce application, fully instrumented with Prometheus metrics
and a JSON logging pipeline into Elasticsearch, plus two reproducible
experiments that break it on purpose and measure what happens.

The written answers for Parts A–E are in **[`REPORT.md`](REPORT.md)**.
The architecture diagram and the metric/log walkthroughs are in
**[`docs/architecture.md`](docs/architecture.md)**.

---

## What you get

| | |
| :-- | :-- |
| **The app** | A storefront: browse a catalogue, add to a cart, place an order, cancel or fulfil it. Orders are stored in Postgres. Payment goes through a separate service. |
| **Metrics** | 23 instrument declarations (20 distinct metric names) across both services — all four Prometheus types — scraped by Prometheus, shown on one provisioned Grafana dashboard with 27 panels. |
| **Host metrics** | Node Exporter: CPU, memory, disk, and network for the machine running the containers. |
| **Logs** | One JSON line per event → Docker → Filebeat → Elasticsearch → Kibana. |
| **Experiments** | A scripted slow-dependency incident, and a scripted cardinality explosion. |

---

## Prerequisites

- **Docker Engine with the Compose v2 plugin.** Check with `docker compose version` —
  it must print a version. The old `docker-compose` v1 script is not supported.
- **Python 3.8 or newer** for the control script. It uses the standard library
  only, so there is **nothing to `pip install`**. The commands below say
  `python3`; on Windows, where `python3` is often only a Microsoft Store stub,
  use `python` instead.
- **About 6 GB of free RAM and 5 GB of free disk.** Elasticsearch and Kibana are
  by far the largest things here; the application containers are tiny.

---

## 1. Start

From the root of this folder:

```bash
docker compose up -d --build
```

The first build takes a few minutes (it downloads Python, Postgres, Prometheus,
Grafana, Node Exporter and the three Elastic images). Subsequent starts take
seconds.

Startup is ordered by healthchecks: the storefront waits for Postgres *and*
payment-service to report healthy, and Filebeat waits for Elasticsearch, so
your first request cannot race a half-booted backend.

### Then verify everything is actually up

```bash
python3 scripts/shopctl.py verify
```

Expected output — every line `OK`, every Prometheus target `UP`:

```
  OK    storefront-api           200  (12 ms)
  OK    storefront ready (db)    200  (8 ms)
  OK    payment-service          200  (5 ms)
  OK    prometheus               200  (4 ms)
  OK    grafana                  200  (11 ms)
  OK    elasticsearch            200  (19 ms)
  OK    kibana                   200  (88 ms)
  OK    node-exporter            200  (14 ms)

  Prometheus scrape targets:
    UP    storefront-api
    UP    payment-service
    UP    node-exporter
    UP    prometheus
```

Elasticsearch and Kibana are the slowest to come up — give them 60–90 seconds
on a first start before worrying about a `FAIL`.

### Import the Kibana data view and saved searches (one command, once)

```bash
python3 scripts/shopctl.py setup
```

This imports `telemetry/kibana/saved-searches.ndjson`: the `tiny-shop-logs-*`
data view (with `@timestamp` as the time field, which Discover needs in order to
show anything) and six saved searches: errors, 5xx responses, payment failures,
checkout requests, slow checkouts, and orders over $100. In Discover, use
**Open** to load one. It is safe to re-run; existing objects are overwritten.
The same file can be imported by hand under *Stack Management → Saved objects →
Import*.

---

## 2. Use it

| What | Where |
| :-- | :-- |
| **The shop** | **<http://localhost:8000>** — browse, add to cart, place, cancel, fulfil |
| **The dashboard** | **<http://localhost:3000/d/tiny-shop>** — login `admin` / `admin` |
| **Logs** | **<http://localhost:5601/app/discover>** — pick "Tiny Shop Logs" |
| API docs (Swagger) | <http://localhost:8000/docs> and <http://localhost:8001/docs> |
| Raw metrics | <http://localhost:8000/metrics>, <http://localhost:8001/metrics> |
| Prometheus targets | <http://localhost:9090/targets> |
| PromQL console | <http://localhost:9090/graph> |
| Node Exporter raw | <http://localhost:9100/metrics> |
| Elasticsearch indices | <http://localhost:9200/_cat/indices?v> |

Grafana may show a "change password" screen — click **Skip**. The password is
pinned to `admin` in `docker-compose.yml`. Anonymous viewing is also enabled, so
the dashboard opens without logging in at all.

> **The dashboard is empty on a fresh start, and that is correct** — no traffic
> has been sent yet. Place an order in the browser, or run the load generator
> below, and the panels fill within one or two 5-second scrapes.

### Generate some traffic

```bash
# 10 checkouts per second for 30 seconds
python3 scripts/shopctl.py load --rps 10 --duration 30
```

The generator places orders with random baskets, cancels about 10% of them and
fulfils about 35%, so the "awaiting fulfilment" gauge moves in both directions
rather than climbing forever. It also restocks the shelves every 15 seconds —
otherwise 227 units of starting inventory would sell out in half a minute and
every later checkout would fail with `out_of_stock`, which would contaminate the
experiments.

### Look at what happened

```bash
# live metrics + which faults are active
python3 scripts/shopctl.py status

# any PromQL expression
python3 scripts/shopctl.py query 'sum by (reason) (rate(shop_orders_rejected_total[1m])) * 60'

# any Elasticsearch/Kibana query
python3 scripts/shopctl.py logs 'message:"order placed"' --size 2
```

---

## 3. Test — the two experiments

Both are fully scripted, print their results to the terminal, and write a
timestamped JSON record into `results/`. Both are safe and both undo themselves.

### Part E.1 — a slow payment provider

```bash
python3 scripts/shopctl.py experiment
```

Runs three stages — **baseline → fault injected → fault removed** — each with
60 seconds of traffic (about 12 Prometheus scrapes). Every metric is read with
the PromQL query evaluated at the moment that stage's traffic ended, so each
`rate(...[1m])` window covers exactly that stage. Takes about 4–5 minutes. The
fault is a 500 ms delay on every fifth payment call. Add `--tag <name>` to label
the result file.

To drive it by hand instead:

```bash
python3 scripts/shopctl.py fault latency --probability 0.2 --seconds 0.5
python3 scripts/shopctl.py load --rps 10 --duration 60
python3 scripts/shopctl.py fault off          # always safe to run
```

### Offline unit tests (optional)

79 in-process checks of the application logic, no Docker required:

```bash
pip install fastapi httpx prometheus-client requests
python tests/test_payment_service.py
python tests/test_storefront_api.py
```

See `tests/README.md` for what they cover.

### Part E.2 — cardinality explosion

```bash
python3 scripts/shopctl.py cardinality
```

Follows the assignment's steps: sends exactly 100 requests to a counter
labelled with the request id and measures the series growth; then **removes the
label and restarts `storefront-api`** (it recreates the container with
`CARDINALITY_DEMO_LABEL=none`), repeats the 100 requests and compares after
another scrape; then, as an extra, sends 100 requests through a bounded label.
Finally it restarts the storefront in its default configuration. Takes about a
minute, and needs the `docker` CLI on your PATH because it restarts a container.

> This is deliberately capped at 100 series. It will not hurt Prometheus.

### Undoing a fault

`python3 scripts/shopctl.py fault off` clears every injected fault in both
services. So does restarting the containers — all fault state is in-process and
defaults to off:

```bash
docker compose restart storefront-api payment-service
```

If the cardinality experiment is interrupted while the label is removed, put the
storefront back in its default configuration with:

```bash
docker compose up -d --force-recreate storefront-api
```

---

## 4. Clean up

```bash
# stop the containers, keep the data (orders, metrics history, log indices)
docker compose down

# stop and delete everything, including all volumes — a full factory reset
docker compose down -v
```

`down -v` removes the Postgres data, the Prometheus TSDB, the Elasticsearch
indices and the Filebeat read-offset registry. The next `up` starts from an
empty database with the five seed products restored. The Grafana datasource and
dashboard are provisioned from files, so they come back either way.

To also reclaim the built images:

```bash
docker compose down -v --rmi local
```

---

## Layout

```
docker-compose.yml              the whole stack, 9 containers
db/init.sql                     schema + seed products

services/storefront-api/        the shop (FastAPI, port 8000)
  app/telemetry.py              EVERY metric is declared here
  app/routes.py                 business metrics + business logs recorded here
  app/application.py            middleware: app metrics + request logs
  app/structured_logging.py     the JSON log format
  app/clients.py                outbound calls to payment, instrumented
  app/db.py                     Postgres pool, query timing
  app/static/index.html         the storefront page

services/payment-service/       mock payment provider (FastAPI, port 8001)
  app/routes.py                 charges, and the fault-injection endpoints

telemetry/prometheus/           scrape config
telemetry/grafana/              datasource + dashboard provisioning
telemetry/grafana/dashboards/   the dashboard JSON (27 panels)
telemetry/filebeat/             log shipping config + retention policy
telemetry/kibana/               data view + saved searches (imported by `setup`)

scripts/shopctl.py              traffic, faults, experiments, verification
tests/                          offline smoke tests (79 checks, no Docker)
results/                        experiment records (JSON + terminal output) behind REPORT.md Part E
docs/architecture.md            architecture diagram + metric/log walkthroughs
REPORT.md                       the written answers, Parts A-E
```

---

## Troubleshooting

**Every Grafana panel says "Data source not found".**
The provisioned datasource uid did not load. `docker compose up -d --force-recreate grafana`.

**Grafana panels are empty but show no error.**
Almost always correct: there is no traffic yet. Run
`python3 scripts/shopctl.py load --rps 10 --duration 30`.

**Kibana says "no data views".**
Run `python3 scripts/shopctl.py setup`. If it reports that the index does not
exist, send traffic first — the index is created by the first log document.

**Kibana shows a data view but zero documents.**
Check the time picker (it defaults to the last 15 minutes) and check Filebeat:
`docker compose logs filebeat | tail -40`.

**Elasticsearch container exits immediately.**
Almost always memory. It is pinned to a 512 MB heap here; make sure Docker
Desktop has at least 4 GB allocated.

**`storefront-api` restarts in a loop.**
Check it can reach Postgres: `docker compose logs storefront-api | tail -30`.
The service retries for 10 seconds at startup before giving up.

**Port already in use.**
Something else on your machine holds 8000, 3000, 5601, 9090, 9200 or 5432.
Change the left-hand side of the `ports:` mapping in `docker-compose.yml`.

---

## A note on security

Elasticsearch and Kibana run with authentication **disabled**, and Grafana ships
with the password `admin` and anonymous viewing on. That is deliberate — it
makes this stack a single `docker compose up` for a grader — and it is only
acceptable because nothing here is reachable from outside your machine. Do not
expose these ports, and do not reuse this configuration anywhere real.

The application never logs card numbers, tokens, emails, addresses or any other
secret or personal data. See `REPORT.md` Part C.

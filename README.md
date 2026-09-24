# Tiny Shop — Assignment 1: Observability

**Enterprise Software Development, Fall 2026**

A very small e-commerce application, fully instrumented with Prometheus metrics
and a JSON logging pipeline into Elasticsearch, plus two reproducible
experiments that break it on purpose and measure what happens.

---

## It includes

| | |
| :-- | :-- |
| **The app** | A storefront: browse a catalogue, add to a cart, place an order, cancel or fulfil it. Orders are stored in Postgres. Payment goes through a separate service. |
| **Metrics** | 23 instrument declarations (20 distinct metric names) across both services — all four Prometheus types — scraped by Prometheus, shown on one provisioned Grafana dashboard with 27 panels. |
| **Host metrics** | Node Exporter: CPU, memory, disk, and network for the machine running the containers. |
| **Logs** | One JSON line per event → Docker → Filebeat → Elasticsearch → Kibana. |
| **Experiments** | A scripted slow-dependency incident, and a scripted cardinality explosion. |

---

## Prerequisites

- **Docker Engine with the Compose v2 plugin.** 
- **Python 3.8 or newer** 
- **About 6 GB of free RAM and 5 GB of free disk.** 
---

## 1. Start

From the root of this folder:

```bash
docker compose up -d --build
```

### Then verify everything is actually up

```bash
python3 scripts/shopctl.py verify
```

### Import the Kibana data view and saved searches (one command, once)

```bash
python3 scripts/shopctl.py setup
```
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

### Part E.2 — cardinality explosion

```bash
python3 scripts/shopctl.py cardinality
```

Sends exactly 100 requests to a counter
labelled with the request id and measures the series growth; then **removes the
label and restarts `storefront-api`** (it recreates the container with
`CARDINALITY_DEMO_LABEL=none`), repeats the 100 requests and compares after
another scrape; then, as an extra, sends 100 requests through a bounded label.
Finally it restarts the storefront in its default configuration. Takes about a
minute.

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
telemetry/filebeat/             log shipping config (daily indices, no ILM)
telemetry/kibana/               data view + saved searches (imported by `setup`)

scripts/shopctl.py              traffic, faults, experiments, verification
tests/                          offline smoke tests (79 checks, no Docker)
results/                        experiment records (JSON + terminal output) behind REPORT.md Part E
docs/architecture.md            architecture diagram + metric/log walkthroughs
REPORT.md                       the written answers, Parts A-E
```

---

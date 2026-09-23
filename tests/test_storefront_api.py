"""Smoke-test storefront-api with a stubbed Postgres and a stubbed payment client.

This exercises the checkout control flow, metric recording and log emission.
It does NOT validate the SQL itself - that is checked against real Postgres.
"""
import contextlib
import io
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "services" / "storefront-api"))

# --- stub psycopg2 before app.db imports it -------------------------------
fake_psycopg2 = types.ModuleType("psycopg2")
fake_extras = types.ModuleType("psycopg2.extras")
fake_pool = types.ModuleType("psycopg2.pool")
fake_extras.RealDictCursor = object
class _Pool:
    def __init__(self, *a, **k): pass
fake_pool.ThreadedConnectionPool = _Pool
fake_psycopg2.extras = fake_extras
fake_psycopg2.pool = fake_pool
sys.modules["psycopg2"] = fake_psycopg2
sys.modules["psycopg2.extras"] = fake_extras
sys.modules["psycopg2.pool"] = fake_pool

from fastapi.testclient import TestClient  # noqa: E402
from app import db, clients  # noqa: E402
real_db_cursor = db.cursor  # kept to test the pool-wait logic itself

# --- in-memory database ----------------------------------------------------
PRODUCTS = {
    "KB-01": {"sku": "KB-01", "name": "Mechanical Keyboard", "price_cents": 4900, "stock": 40},
    "MS-01": {"sku": "MS-01", "name": "Wireless Mouse", "price_cents": 1900, "stock": 60},
    "OUT-01": {"sku": "OUT-01", "name": "Sold Out Thing", "price_cents": 500, "stock": 0},
}
ORDERS = {}
NEXT_ID = [1000]
COMMITS = []
PRODUCT_LOCKS = []  # every statement that row-locks products


class FakeConn:
    def commit(self): COMMITS.append("commit")
    def rollback(self): COMMITS.append("rollback")


class FakeCursor:
    def __init__(self): self._rows = []; self.connection = FakeConn()

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM products" in s and "FOR UPDATE" in s:
            PRODUCT_LOCKS.append(s)
        if "FROM products WHERE sku = ANY" in s:
            self._rows = [dict(PRODUCTS[k]) for k in params[0] if k in PRODUCTS]
        elif s.startswith("SELECT sku, stock FROM products"):
            self._rows = [{"sku": p["sku"], "stock": p["stock"]} for p in PRODUCTS.values()]
        elif s.startswith("SELECT sku, name, price_cents, stock FROM products ORDER BY"):
            self._rows = [dict(p) for p in PRODUCTS.values()]
        elif "COUNT(*) AS n FROM orders" in s:
            self._rows = [{"n": sum(1 for o in ORDERS.values() if o["status"] == "PLACED")}]
        elif s.startswith("INSERT INTO orders"):
            NEXT_ID[0] += 1
            ORDERS[NEXT_ID[0]] = {"status": "PLACED", "total_cents": params[0],
                                  "item_count": params[1]}
            self._rows = [{"id": NEXT_ID[0]}]
        elif s.startswith("INSERT INTO order_items"):
            self._rows = []
        elif s.startswith("UPDATE products SET stock = stock -"):
            PRODUCTS[params[1]]["stock"] -= params[0]
            self._rows = []
        elif s.startswith("UPDATE products SET stock = %s"):
            for p in PRODUCTS.values():
                p["stock"] = params[0]
            self._rows = []
        elif "SELECT status, total_cents FROM orders WHERE id" in s:
            o = ORDERS.get(params[0])
            self._rows = [{"status": o["status"], "total_cents": o["total_cents"]}] if o else []
        elif "SELECT status FROM orders WHERE id" in s:
            o = ORDERS.get(params[0])
            self._rows = [{"status": o["status"]}] if o else []
        elif s.startswith("UPDATE orders SET status = 'CANCELLED'"):
            ORDERS[params[0]]["status"] = "CANCELLED"; self._rows = []
        elif s.startswith("UPDATE orders SET status = 'FULFILLED'"):
            ORDERS[params[0]]["status"] = "FULFILLED"; self._rows = []
        elif s.startswith("UPDATE products p SET stock"):
            self._rows = []
        elif "SELECT id, status, total_cents" in s:
            self._rows = [{"id": k, "status": v["status"], "total_cents": v["total_cents"],
                           "item_count": v["item_count"], "customer": "test",
                           "created_at": "2026-09-22T00:00:00Z"}
                          for k, v in sorted(ORDERS.items(), reverse=True)]
        elif s.startswith("SELECT sku FROM products ORDER BY sku FOR UPDATE"):
            self._rows = [{"sku": k} for k in sorted(PRODUCTS)]
        elif s.startswith("SELECT p.sku FROM products p JOIN order_items"):
            self._rows = []
        elif "SELECT 1 AS ok" in s:
            self._rows = [{"ok": 1}]
        else:
            raise AssertionError("unhandled SQL: " + s)

    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows


@contextlib.contextmanager
def fake_cursor(operation, commit=False):
    cur = FakeCursor()
    try:
        yield cur
        COMMITS.append("commit" if commit else "rollback")
    except Exception:
        COMMITS.append("rollback")
        raise


db.cursor = fake_cursor
db.init_pool = lambda *a, **k: None
db.ping = lambda: True
db.close_pool = lambda: None

# patch the already-imported references inside routes
import app.routes as routes  # noqa: E402
routes.db.cursor = fake_cursor

PAYMENT_MODE = {"outcome": clients.PaymentOutcome.APPROVED}


def fake_charge(order_id, amount_cents, method, request_id):
    return PAYMENT_MODE["outcome"], {"status_code": 200, "duration_ms": 12.3}


routes.clients.charge = fake_charge
routes.clients.post_chaos = lambda path, payload: {"stubbed": path}
routes.clients.get_chaos_status = lambda: {"stubbed": True}

from app.application import app  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def metric_value(text, needle):
    for line in text.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[1])
    return None


# silence the JSON log lines during the run, but capture them for inspection
log_buffer = io.StringIO()
real_stdout = sys.stdout

with TestClient(app) as c:
    m0 = c.get("/metrics").text

    r = c.get("/health")
    check("GET /health -> 200", r.status_code == 200)

    r = c.get("/ready")
    check("GET /ready -> 200", r.status_code == 200, r.json())

    r = c.get("/")
    check("GET / serves the storefront page", r.status_code == 200 and "Tiny Shop" in r.text)

    r = c.get("/api/products")
    check("GET /api/products lists products", r.status_code == 200
          and len(r.json()["products"]) == 3)

    # --- happy path ---
    placed0 = metric_value(m0, 'shop_orders_placed_total{payment_method="card"}') or 0
    r = c.post("/api/checkout", json={"items": [{"sku": "KB-01", "quantity": 2}],
                                      "customer": "ayesha", "payment_method": "card"})
    check("checkout -> 201", r.status_code == 201, r.json())
    body = r.json()
    check("checkout returns order_id + total", body.get("total_dollars") == 98.0, body)
    check("checkout returns a request_id", len(body.get("request_id", "")) == 12, body)
    order_id = body["order_id"]

    m = c.get("/metrics").text
    check("shop_orders_placed_total incremented",
          metric_value(m, 'shop_orders_placed_total{payment_method="card"}') == placed0 + 1)
    check("shop_revenue_dollars_total incremented",
          metric_value(m, "shop_revenue_dollars_total") == 98.0)
    check("shop_orders_awaiting_fulfilment == 1",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 1.0)
    check("shop_order_value_dollars (Histogram) recorded",
          metric_value(m, "shop_order_value_dollars_count") == 1.0)
    check("shop_items_per_order (Summary) recorded",
          metric_value(m, "shop_items_per_order_count") == 1.0)
    check("shop_checkout_duration_seconds{outcome=placed} recorded",
          metric_value(m, 'shop_checkout_duration_seconds_count{outcome="placed"}') == 1.0)
    check("stock gauge reflects the sale",
          metric_value(m, 'shop_stock_units{sku="KB-01"}') == 38.0)
    check("Summary type present for payment_call_duration_seconds",
          "payment_call_duration_seconds" in m)

    # --- out of stock ---
    r = c.post("/api/checkout", json={"items": [{"sku": "OUT-01", "quantity": 1}]})
    check("out-of-stock checkout -> 409", r.status_code == 409, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=out_of_stock} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="out_of_stock"}') == 1.0)
    check("checkout duration recorded for rejection",
          metric_value(m, 'shop_checkout_duration_seconds_count{outcome="rejected"}') == 1.0)

    # --- unknown sku ---
    r = c.post("/api/checkout", json={"items": [{"sku": "NOPE-99", "quantity": 1}]})
    check("unknown sku -> 404", r.status_code == 404, r.status_code)

    # --- payment declined ---
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.DECLINED
    r = c.post("/api/checkout", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("declined payment -> 402", r.status_code == 402, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=payment_declined} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_declined"}') == 1.0)

    # --- payment error (the Part E failure mode) ---
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.ERROR
    r = c.post("/api/checkout", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("payment provider error -> 502", r.status_code == 502, r.status_code)
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.TIMEOUT
    r = c.post("/api/checkout", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("payment timeout -> 502", r.status_code == 502, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=payment_error} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_error"}') == 1.0)
    check("rejected{reason=payment_timeout} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_timeout"}') == 1.0)
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.APPROVED

    # --- cancel: gauge goes back down ---
    r = c.post(f"/api/orders/{order_id}/cancel")
    check("cancel order -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("shop_orders_cancelled_total incremented",
          metric_value(m, 'shop_orders_cancelled_total{reason="customer_request"}') == 1.0)
    check("awaiting_fulfilment gauge back to 0",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 0.0)

    # --- fulfil ---
    r = c.post("/api/checkout", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    oid2 = r.json()["order_id"]
    r = c.post(f"/api/orders/{oid2}/fulfil")
    check("fulfil order -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("awaiting_fulfilment gauge back to 0 after fulfil",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 0.0)

    # --- double cancel is rejected ---
    r = c.post(f"/api/orders/{oid2}/cancel")
    check("cancelling a FULFILLED order -> 409", r.status_code == 409, r.status_code)

    # --- route templating: no order id in metric labels ---
    m = c.get("/metrics").text
    check('route template used, not raw id',
          'route="/api/orders/{order_id}/cancel"' in m)
    check("no raw order id leaked into metric labels", f'route="/api/orders/{oid2}' not in m)

    # --- restock ---
    r = c.post("/admin/restock", json={"level": 500})
    check("restock -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("stock gauge updated by restock",
          metric_value(m, 'shop_stock_units{sku="KB-01"}') == 500.0)

    # --- cardinality demo off by default ---
    m = c.get("/metrics").text
    check("demo_requests_total has no series by default",
          'demo_requests_total{request_id=' not in m)

    r = c.post("/admin/chaos/cardinality", json={"active": True})
    check("cardinality toggle on -> 200", r.status_code == 200, r.json())
    for _ in range(5):
        c.get("/api/products")
    m = c.get("/metrics").text
    series = m.count("demo_requests_total{request_id=")
    check("cardinality demo creates one series per request", series >= 5, f"series={series}")

    c.post("/admin/chaos/cardinality", json={"active": False})
    before = m.count("demo_requests_total{request_id=")
    for _ in range(5):
        c.get("/api/products")
    m2 = c.get("/metrics").text
    check("toggling off stops NEW series",
          m2.count("demo_requests_total{request_id=") == before)

    for i in range(3):
        c.post("/admin/demo/safe-counter?tier=" + ("vip" if i == 0 else "standard"))
    m = c.get("/metrics").text
    check("bounded counter stays at 2 series",
          m.count("demo_requests_safe_total{tier=") == 2,
          m.count("demo_requests_safe_total{tier="))

    # --- toggling off and on again must NOT allow another 100 ---
    c.post("/admin/chaos/cardinality", json={"active": True})
    for _ in range(140):
        c.get("/api/products")
    m = c.get("/metrics").text
    capped = m.count("demo_requests_total{request_id=")
    check("cardinality caps at 100 DISTINCT ids across toggles", capped == 100,
          f"series={capped}")
    c.post("/admin/chaos/cardinality", json={"active": False})

    # --- reset clears the leaked label children, making a re-run possible ---
    r = c.post("/admin/chaos/reset")
    check("reset -> 200", r.status_code == 200, r.status_code)
    m = c.get("/metrics").text
    check("reset clears demo series from /metrics",
          m.count("demo_requests_total{request_id=") == 0,
          m.count("demo_requests_total{request_id="))
    c.post("/admin/chaos/cardinality", json={"active": True})
    for _ in range(120):
        c.get("/api/products")
    m = c.get("/metrics").text
    again = m.count("demo_requests_total{request_id=")
    check("experiment is re-runnable after reset (100 again)", again == 100,
          f"series={again}")
    c.post("/admin/chaos/reset")

    # --- step 3 of E.2: the same counter built WITHOUT the label ---
    from prometheus_client import CollectorRegistry, generate_latest
    from app.telemetry import make_demo_counter
    reg = CollectorRegistry()
    unlabelled = make_demo_counter("none", registry=reg)
    for _ in range(100):
        unlabelled.inc()
    text = generate_latest(reg).decode()
    samples = [ln for ln in text.splitlines() if ln.startswith("demo_requests_total")]
    check("label removed -> 100 requests make exactly 1 series",
          samples == ["demo_requests_total 100.0"], samples)

    # --- 404 collapses ---
    c.get("/api/orders/99999/nonexistent-endpoint")
    m = c.get("/metrics").text
    check('unmatched routes collapse', 'route="unmatched"' in m)

# --- every product row lock is taken in sku order (deadlock fix, Part E.1) ---
check("checkout, cancel and restock all lock products",
      any("sku = ANY" in q for q in PRODUCT_LOCKS)
      and any("JOIN order_items" in q for q in PRODUCT_LOCKS)
      and any(q.startswith("SELECT sku FROM products") for q in PRODUCT_LOCKS),
      len(PRODUCT_LOCKS))
check("every product lock is ORDER BY sku",
      all("ORDER BY sku FOR UPDATE" in q or "ORDER BY p.sku FOR UPDATE" in q
          for q in PRODUCT_LOCKS), [q for q in PRODUCT_LOCKS if "ORDER BY" not in q][:1])

# --- a crash inside a route is logged as structured JSON, not just a 500 ---
import logging  # noqa: E402


class PoolExhausted(Exception):
    pass


@contextlib.contextmanager
def exhausted_cursor(operation, commit=False):
    raise PoolExhausted("connection pool exhausted")
    yield


captured = io.StringIO()
capture = logging.StreamHandler(captured)
logging.getLogger("storefront-api").addHandler(capture)
with TestClient(app, raise_server_exceptions=False) as c:
    routes.db.cursor = exhausted_cursor
    r = c.get("/api/products", headers={"x-request-id": "crash-test-1"})
    routes.db.cursor = fake_cursor
logging.getLogger("storefront-api").removeHandler(capture)

check("crash -> 500", r.status_code == 500, r.status_code)
crash_logs = [json.loads(ln) for ln in captured.getvalue().splitlines()
              if '"unhandled exception"' in ln]
check("crash emits one 'unhandled exception' log", len(crash_logs) == 1, len(crash_logs))
check("crash response still carries x-request-id", r.headers.get("x-request-id") == "crash-test-1")
if crash_logs:
    doc = crash_logs[0]
    check("crash log carries error.type, message, request_id and route",
          doc.get("error", {}).get("type") == "PoolExhausted"
          and doc["error"]["message"] == "connection pool exhausted"
          and doc.get("request_id") == "crash-test-1"
          and doc.get("url", {}).get("route") == "/api/products"
          and doc["log"]["level"] == "error", doc)
    check("crash log holds the whole stack in ONE field",
          "Traceback" in doc["error"].get("stack_trace", "")
          and "exhausted_cursor" in doc["error"]["stack_trace"])

# --- the pool waits for a free connection, then times out as PoolTimeout ---
import threading  # noqa: E402
import time  # noqa: E402


class _Conn:
    def cursor(self, cursor_factory=None):
        return contextlib.nullcontext(object())
    def commit(self): pass
    def rollback(self): pass


class _OnePool:
    def getconn(self): return _Conn()
    def putconn(self, conn): pass


db._POOL, db._SLOTS = _OnePool(), threading.BoundedSemaphore(1)
db.DB_POOL_WAIT_SECONDS = 0.2
holding, release = threading.Event(), threading.Event()


def hold_connection():
    with real_db_cursor("hold"):
        holding.set()
        release.wait(2)


t = threading.Thread(target=hold_connection)
t.start()
holding.wait(2)
t0 = time.perf_counter()
try:
    with real_db_cursor("second"):
        pass
    timed_out = False
except db.PoolTimeout:
    timed_out = True
waited = time.perf_counter() - t0
check("pool full -> waits, then PoolTimeout", timed_out and waited >= 0.15,
      f"waited={waited:.2f}s")
release.set()
t.join()
with real_db_cursor("after-release"):
    pass
check("slot is returned after use", db._SLOTS.acquire(blocking=False))
db._SLOTS.release()
db._POOL = db._SLOTS = None

# --- PoolTimeout surfaces as a clean 503 with a structured log ---


@contextlib.contextmanager
def busy_cursor(operation, commit=False):
    raise db.PoolTimeout("no free database connection within 2.0s")
    yield


captured = io.StringIO()
capture = logging.StreamHandler(captured)
logging.getLogger("storefront-api").addHandler(capture)
with TestClient(app, raise_server_exceptions=False) as c:
    routes.db.cursor = busy_cursor
    r = c.get("/api/products", headers={"x-request-id": "busy-test-1"})
    routes.db.cursor = fake_cursor
logging.getLogger("storefront-api").removeHandler(capture)
check("pool timeout -> 503", r.status_code == 503, r.status_code)
busy = [json.loads(ln) for ln in captured.getvalue().splitlines() if '"database busy"' in ln]
check("pool timeout logs 'database busy' with error.type and request_id",
      len(busy) == 1 and busy[0]["error"]["type"] == "PoolTimeout"
      and busy[0]["request_id"] == "busy-test-1", busy)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

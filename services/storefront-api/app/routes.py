"""Shop endpoints. This is where the business metrics and business logs live."""

import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import clients, db
from .state import LOCK, STATE, reset as reset_state, snapshot
from .structured_logging import log_event
from .telemetry import (
    CHECKOUT_DURATION,
    DEMO_HAS_ID_LABEL,
    DEMO_REQUESTS,
    DEMO_REQUESTS_SAFE,
    ITEMS_PER_ORDER,
    ORDERS_AWAITING_FULFILMENT,
    ORDERS_CANCELLED,
    ORDERS_PLACED,
    ORDERS_REJECTED,
    ORDER_VALUE,
    REVENUE,
    STOCK_UNITS,
)

router = APIRouter()


# ------------------------------------------------------------------ schemas
class CartLine(BaseModel):
    sku: str = Field(min_length=1, max_length=16)
    quantity: int = Field(gt=0, le=20)


class CheckoutRequest(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=20)
    # A display name only. Never an email, phone number or address -- those are
    # personal data and this value reaches the log index.
    customer: str = Field(default="guest", min_length=1, max_length=40)
    payment_method: str = Field(default="card", pattern="^(card|wallet|cod)$")


# ------------------------------------------------------------ gauge helpers
def refresh_stock_gauge() -> None:
    """Re-publish shop_stock_units from the database.

    A Gauge is current state, not an event stream, so the safe way to keep it
    honest is to read the source of truth rather than to increment blindly.
    """
    with db.cursor("list_products") as cur:
        cur.execute("SELECT sku, stock FROM products")
        for row in cur.fetchall():
            STOCK_UNITS.labels(sku=row["sku"]).set(row["stock"])


def refresh_open_orders_gauge() -> None:
    """Re-publish shop_orders_awaiting_fulfilment from the database."""
    with db.cursor("count_open") as cur:
        cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'PLACED'")
        ORDERS_AWAITING_FULFILMENT.set(cur.fetchone()["n"])


# ------------------------------------------------------------------- catalog
@router.get("/api/products", tags=["shop"])
def list_products():
    """The product catalogue shown on the storefront page."""
    with db.cursor("list_products") as cur:
        cur.execute("SELECT sku, name, price_cents, stock FROM products ORDER BY name")
        products = cur.fetchall()
    return {"products": products}


# ------------------------------------------------------------------ checkout
@router.post("/api/checkout", tags=["shop"], status_code=201)
def checkout(payload: CheckoutRequest, request: Request):
    """Place an order: reserve stock, charge payment, persist the order.

    Every exit path records shop_checkout_duration_seconds{outcome=...} and
    exactly one business log line, so the funnel adds up:
        placed + rejected == checkout attempts.
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
    started = time.perf_counter()
    outcome = "error"

    try:
        skus = [line.sku for line in payload.items]
        with db.cursor("create_order", commit=False) as cur:
            # FOR UPDATE takes a row lock so two concurrent checkouts cannot
            # both sell the last unit. ORDER BY sku makes every transaction
            # lock rows in the same order; without it two baskets holding the
            # same SKUs in a different order deadlock (seen in Part E.1).
            cur.execute(
                "SELECT sku, name, price_cents, stock FROM products "
                "WHERE sku = ANY(%s) ORDER BY sku FOR UPDATE",
                (skus,),
            )
            found = {row["sku"]: row for row in cur.fetchall()}

            missing = [sku for sku in skus if sku not in found]
            if missing:
                outcome = "rejected"
                ORDERS_REJECTED.labels(reason="unknown_sku").inc()
                log_event(
                    "checkout rejected: unknown sku",
                    level="warn",
                    event={"action": "checkout", "outcome": "rejected",
                           "reason": "unknown_sku"},
                    order={"skus": missing},
                    request_id=request_id,
                )
                raise HTTPException(status_code=404, detail="unknown sku")

            short = [
                line.sku for line in payload.items
                if found[line.sku]["stock"] < line.quantity
            ]
            if short:
                outcome = "rejected"
                ORDERS_REJECTED.labels(reason="out_of_stock").inc()
                log_event(
                    "checkout rejected: out of stock",
                    level="warn",
                    event={"action": "checkout", "outcome": "rejected",
                           "reason": "out_of_stock"},
                    order={"skus": short},
                    request_id=request_id,
                )
                raise HTTPException(status_code=409, detail="out of stock")

            total_cents = sum(
                found[line.sku]["price_cents"] * line.quantity for line in payload.items
            )
            item_count = sum(line.quantity for line in payload.items)

            # Write the order as PLACED and decrement stock.
            cur.execute(
                "INSERT INTO orders (status, total_cents, item_count, customer) "
                "VALUES ('PLACED', %s, %s, %s) RETURNING id",
                (total_cents, item_count, payload.customer),
            )
            order_id = cur.fetchone()["id"]
            for line in payload.items:
                cur.execute(
                    "INSERT INTO order_items (order_id, sku, quantity, price_cents) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, line.sku, line.quantity, found[line.sku]["price_cents"]),
                )
                cur.execute(
                    "UPDATE products SET stock = stock - %s WHERE sku = %s",
                    (line.quantity, line.sku),
                )

            # Charge. Still inside the transaction: if payment fails the whole
            # thing rolls back, so stock is never silently lost.
            pay_outcome, pay_detail = clients.charge(
                order_id=order_id,
                amount_cents=total_cents,
                method=payload.payment_method,
                request_id=request_id,
            )

            if pay_outcome != clients.PaymentOutcome.APPROVED:
                reason = {
                    clients.PaymentOutcome.DECLINED: "payment_declined",
                    clients.PaymentOutcome.TIMEOUT: "payment_timeout",
                }.get(pay_outcome, "payment_error")
                outcome = "rejected"
                ORDERS_REJECTED.labels(reason=reason).inc()
                log_event(
                    "checkout rejected: payment not approved",
                    level="warn" if reason == "payment_declined" else "error",
                    event={"action": "checkout", "outcome": "rejected", "reason": reason},
                    order={"id": order_id,
                           "value_dollars": round(total_cents / 100.0, 2)},
                    payment={"outcome": pay_outcome, **pay_detail},
                    request_id=request_id,
                )
                # Rolled back by the context manager (commit=False).
                status_code = 402 if reason == "payment_declined" else 502
                raise HTTPException(status_code=status_code, detail=reason)

            # Payment approved -- commit the transaction.
            cur.connection.commit()

        outcome = "placed"
        value_dollars = total_cents / 100.0
        ORDERS_PLACED.labels(payment_method=payload.payment_method).inc()
        ORDER_VALUE.observe(value_dollars)
        ITEMS_PER_ORDER.observe(item_count)
        REVENUE.inc(value_dollars)
        ORDERS_AWAITING_FULFILMENT.inc()
        refresh_stock_gauge()

        log_event(
            "order placed",
            event={"action": "checkout", "outcome": "placed"},
            order={
                "id": order_id,
                "value_dollars": round(value_dollars, 2),
                "item_count": item_count,
            },
            payment={"method": payload.payment_method, **pay_detail},
            request_id=request_id,
        )
        return {
            "order_id": order_id,
            "status": "PLACED",
            "total_dollars": round(value_dollars, 2),
            "item_count": item_count,
            "request_id": request_id,
        }

    finally:
        CHECKOUT_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)


# -------------------------------------------------------------------- orders
@router.get("/api/orders", tags=["shop"])
def list_orders(limit: int = 20):
    """Most recent orders, for the storefront page."""
    limit = max(1, min(limit, 100))
    with db.cursor("list_orders") as cur:
        cur.execute(
            "SELECT id, status, total_cents, item_count, customer, created_at "
            "FROM orders ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        orders = cur.fetchall()
    return {"orders": orders}


@router.post("/api/orders/{order_id}/cancel", tags=["shop"])
def cancel_order(order_id: int, request: Request):
    """Cancel a PLACED order and return its stock to the shelf."""
    request_id = getattr(request.state, "request_id", "-")
    with db.cursor("cancel_order", commit=True) as cur:
        cur.execute(
            "SELECT status, total_cents FROM orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such order")
        if row["status"] != "PLACED":
            raise HTTPException(status_code=409, detail="order is not cancellable")

        # Lock the product rows in sku order first, the same order checkout
        # uses, so a cancel and a checkout cannot deadlock each other.
        cur.execute(
            "SELECT p.sku FROM products p JOIN order_items oi ON oi.sku = p.sku "
            "WHERE oi.order_id = %s ORDER BY p.sku FOR UPDATE OF p",
            (order_id,),
        )
        cur.execute(
            "UPDATE products p SET stock = p.stock + oi.quantity "
            "FROM order_items oi WHERE oi.order_id = %s AND oi.sku = p.sku",
            (order_id,),
        )
        cur.execute(
            "UPDATE orders SET status = 'CANCELLED', updated_at = now() WHERE id = %s",
            (order_id,),
        )
        total_cents = row["total_cents"]

    ORDERS_CANCELLED.labels(reason="customer_request").inc()
    ORDERS_AWAITING_FULFILMENT.dec()
    refresh_stock_gauge()
    log_event(
        "order cancelled",
        level="warn",
        event={"action": "cancel", "outcome": "cancelled",
               "reason": "customer_request"},
        order={"id": order_id, "value_dollars": round(total_cents / 100.0, 2)},
        request_id=request_id,
    )
    return {"order_id": order_id, "status": "CANCELLED"}


@router.post("/api/orders/{order_id}/fulfil", tags=["shop"])
def fulfil_order(order_id: int, request: Request):
    """Mark a PLACED order shipped. This is what brings the Gauge back down."""
    request_id = getattr(request.state, "request_id", "-")
    with db.cursor("fulfil_order", commit=True) as cur:
        cur.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such order")
        if row["status"] != "PLACED":
            raise HTTPException(status_code=409, detail="order is not fulfillable")
        cur.execute(
            "UPDATE orders SET status = 'FULFILLED', updated_at = now() WHERE id = %s",
            (order_id,),
        )

    ORDERS_AWAITING_FULFILMENT.dec()
    log_event(
        "order fulfilled",
        event={"action": "fulfil", "outcome": "fulfilled"},
        order={"id": order_id},
        request_id=request_id,
    )
    return {"order_id": order_id, "status": "FULFILLED"}


# ------------------------------------------------------- experiment controls
class LatencyFault(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    seconds: float = Field(default=0.5, ge=0.0, le=10.0)


class FailureFault(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)


class CardinalityToggle(BaseModel):
    active: bool


@router.post("/admin/chaos/payment-latency", tags=["experiments"])
def payment_latency(fault: LatencyFault):
    """Part E.1 -- delay a fraction of payment calls (forwarded downstream)."""
    result = clients.post_chaos("/chaos/latency", fault.model_dump())
    log_event("experiment: payment latency fault set", level="warn",
              fault=fault.model_dump())
    return result


@router.post("/admin/chaos/payment-failure", tags=["experiments"])
def payment_failure(fault: FailureFault):
    """Part E.1 -- fail a fraction of payment calls with HTTP 503."""
    result = clients.post_chaos("/chaos/failure", fault.model_dump())
    log_event("experiment: payment failure fault set", level="warn",
              fault=fault.model_dump())
    return result


@router.post("/admin/chaos/cardinality", tags=["experiments"])
def cardinality(toggle: CardinalityToggle):
    """Part E.2 -- start/stop writing the unbounded request_id metric label.

    Switching it OFF stops new series being created but deliberately does not
    delete the ones already made: that asymmetry is the lesson. Use
    /admin/chaos/reset to actually clear them.
    """
    with LOCK:
        STATE["cardinality_demo"] = toggle.active
    log_event("experiment: cardinality demo toggled", level="warn",
              active=toggle.active)
    return snapshot()


@router.post("/admin/chaos/reset", tags=["experiments"])
def reset_everything():
    """Clear every injected fault in both services. Always safe to call.

    This also drops the label children the cardinality demo created, so the
    experiment can be re-run. Note that removing them from /metrics does NOT
    delete what Prometheus already stored -- those series stay until they go
    stale and retention removes them.
    """
    payment = clients.post_chaos("/chaos/reset", {})
    local = reset_state()
    if DEMO_HAS_ID_LABEL:
        DEMO_REQUESTS.clear()
    log_event("experiment: reset", level="warn")
    return {"storefront": local, "payment_service": payment}


@router.get("/admin/chaos/status", tags=["experiments"])
def chaos_status():
    return {"storefront": snapshot(), "payment_service": clients.get_chaos_status()}


class RestockRequest(BaseModel):
    level: int = Field(default=500, ge=0, le=100000)


@router.post("/admin/restock", tags=["experiments"])
def restock(payload: RestockRequest, request: Request):
    """Set every SKU back to `level` units.

    A load test places hundreds of orders in a minute and would otherwise empty
    the shelves, after which every checkout fails with out_of_stock and the
    experiment measures the wrong thing. The traffic generator calls this
    periodically so the Part E fault is the only variable that changes.
    """
    request_id = getattr(request.state, "request_id", "-")
    with db.cursor("restock", commit=True) as cur:
        # Same sku lock order as checkout and cancel.
        cur.execute("SELECT sku FROM products ORDER BY sku FOR UPDATE")
        cur.execute("UPDATE products SET stock = %s", (payload.level,))
    refresh_stock_gauge()
    log_event(
        "inventory restocked",
        event={"action": "restock", "outcome": "ok"},
        inventory={"level": payload.level},
        request_id=request_id,
    )
    return {"status": "ok", "level": payload.level}


@router.post("/admin/demo/safe-counter", tags=["experiments"])
def safe_counter(tier: str = "standard"):
    """Part E.2 step 3 -- the bounded replacement for the demo counter."""
    tier = tier if tier in ("standard", "vip") else "standard"
    DEMO_REQUESTS_SAFE.labels(tier=tier).inc()
    return {"tier": tier}

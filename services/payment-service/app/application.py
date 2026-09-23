"""FastAPI composition root and the request-observability middleware."""

import time
import traceback
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import SERVICE_NAME
from .routes import router
from .structured_logging import log_event
from .telemetry import IN_FLIGHT, REQUEST_COUNT, REQUEST_LATENCY

app = FastAPI(title="Tiny Shop — Payment Service", version="1.0.0")

# Endpoints that are scraped or polled constantly. Recording them would bury the
# real traffic in both the metrics and the log index.
_UNINSTRUMENTED = {"/metrics", "/health"}


def _route_template(request: Request) -> str:
    """Return the ROUTE TEMPLATE, never the raw URL.

    `/orders/8231/cancel` and `/orders/8232/cancel` must both record as
    `/orders/{order_id}/cancel`. Using the raw path as a metric label is exactly
    the cardinality explosion demonstrated in Part E.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # No route matched (404) — collapse every unknown URL into one bucket so a
    # scanner hitting random paths cannot create unbounded series.
    return "unmatched"


@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path in _UNINSTRUMENTED:
        return await call_next(request)

    # Honour an inbound request id so one checkout can be followed across both
    # services in Kibana; mint one if this is the edge.
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    IN_FLIGHT.inc()
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    except Exception as exc:
        # One JSON line with the whole stack, instead of a plain-text traceback
        # that Filebeat would ship as one unparseable document per line.
        log_event(
            "unhandled exception",
            level="error",
            error={"type": type(exc).__name__,
                   "message": str(exc).splitlines()[0][:200] if str(exc) else "",
                   "stack_trace": traceback.format_exc()},
            url={"path": request.url.path, "route": _route_template(request)},
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content={"detail": "internal error"},
                            headers={"x-request-id": request_id})
    finally:
        duration = time.perf_counter() - started
        IN_FLIGHT.dec()
        route = _route_template(request)
        REQUEST_COUNT.labels(request.method, route, str(status)).inc()
        REQUEST_LATENCY.labels(request.method, route).observe(duration)
        log_event(
            "request completed",
            level="error" if status >= 500 else "info",
            http={
                "request": {"method": request.method},
                "response": {"status_code": status},
            },
            url={"path": request.url.path, "route": route},
            event={"duration_ms": round(duration * 1000, 2)},
            request_id=request_id,
        )


@app.get("/health", tags=["ops"])
def health():
    """Liveness probe. Also drives the Compose healthcheck."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/metrics", tags=["ops"])
def metrics():
    """Prometheus exposition endpoint — the raw text Prometheus scrapes."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.on_event("startup")
def announce():
    log_event("service started", service_version="1.0.0")


app.include_router(router)

"""Enterprise FastAPI server for Data Shell Gateway.

The app assembly owns only cross-cutting concerns: lifespan (startup
recovery, seeding, retention), middleware (security headers, correlation IDs,
bounded request bodies), observability probes (/health /ready /metrics), and
the static enterprise console. Endpoint groups live in
``datashell/server/routers/`` and shared process state in
``datashell/server/state.py``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from datashell import __version__
from datashell.contract.models import DataContract
from datashell.contract.registry import ContractRegistry
from datashell.core.alerts import emit_startup_alerts, retry_pending_alerts_async
from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.core.observability import configure_otel
from datashell.core.observability import http_server_span as http_server_span_context
from datashell.core.security import (
    APIKeyMetadata,
    Capability,
    preflight_authenticate_scope,
    require_scope,
    security_registry_ready,
)
from datashell.core.telemetry import (
    HTTP_IN_FLIGHT_REQUESTS,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    PENDING_LEDGER_ENTRIES,
    PUBLICATION_RECOVERY_ERRORS,
    PUBLICATION_RECOVERY_JOURNALS,
    STORAGE_FREE_BYTES,
    STORAGE_TOTAL_BYTES,
    telemetry_logger,
)
from datashell.pipeline.runner import recover_pending_publications_and_ledger
from datashell.server.middleware import RequestBodyLimitMiddleware
from datashell.server.routers import admin, contracts, ledger, runs, settlements
from datashell.server.state import registry


def seed_default_contracts(reg: ContractRegistry) -> None:
    """Seed default contracts on startup so fresh deployments are immediately usable."""
    # Source checkouts and installed wheels place bundled examples in different
    # locations. In the container, examples are copied to /app/examples while
    # the importable package lives under site-packages.
    candidates = [Path(__file__).resolve().parent.parent.parent / "examples", Path.cwd() / "examples"]
    examples_dir = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if examples_dir is not None:
        for contract_file in examples_dir.glob("*_contract.json"):
            try:
                with open(contract_file, encoding="utf-8") as f:
                    data = json.load(f)
                # The examples directory also ships settlement commercial
                # contracts, which share the *_contract.json suffix but are a
                # different schema registered through the settlement API/CLI.
                # Only files carrying the data-contract envelope are seeded
                # into the shared data-contract catalog.
                if not (isinstance(data, dict) and {"metadata", "spec"} <= data.keys()):
                    telemetry_logger.info(
                        "Skipping non-data-contract example file",
                        file=contract_file.name,
                    )
                    continue
                c = DataContract(**data)
                existing = reg.get_latest(c.metadata.name)
                if not existing:
                    reg.save_contract(c)
                    existing = c
                    telemetry_logger.info("Default contract auto-seeded", contract=c.metadata.name)
                # Only expose a global contract as a shared default when it is
                # the exact bundled example. Never implicitly share an older
                # tenant-owned contract that happens to have the same name.
                if existing.model_dump(by_alias=True) == c.model_dump(by_alias=True):
                    reg.mark_shared_contract(c.metadata.name)
            except Exception as e:
                telemetry_logger.warning(
                    "Failed to auto-seed example contract",
                    file=contract_file.name, error_type=type(e).__name__,
                )
                if settings.is_production_mode:
                    # A production deployment must not silently become ready
                    # with a partially initialized shared contract catalog.
                    raise RuntimeError("Production contract catalog initialization failed") from e

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Recovery is a startup gate: do not mutate contract state, accept requests,
    # or report readiness until every interrupted publication has been examined
    # AND every published-but-unrecorded run has been reconciled into the ledger.
    # In particular, avoid seeding contracts at module import time because an
    # ASGI server imports the module before it runs the lifespan startup phase.
    configure_otel()
    recovery = recover_pending_publications_and_ledger(settings.output_dir)
    # Expose the recovery outcome as metrics so an unreconciled state is
    # alertable, not only visible in a log line.
    PUBLICATION_RECOVERY_JOURNALS.set(float(recovery.get("journals", 0) or 0))
    PUBLICATION_RECOVERY_ERRORS.set(float(recovery.get("journal_errors", 0) or 0))
    PENDING_LEDGER_ENTRIES.set(float(recovery.get("skipped", 0) or 0))
    if recovery.get("replayed"):
        telemetry_logger.info("Reconciled pending ledger entries at startup", **recovery)
    if recovery.get("skipped"):
        # Unreconciled ledger entries are a real inconsistency, not a no-op.
        # They stay durable on disk for operator repair and are surfaced here
        # so the condition is visible in logs and alerts.
        telemetry_logger.error(
            "Unreconciled pending ledger entries remain after startup recovery",
            **recovery,
        )
    if recovery.get("journal_errors"):
        telemetry_logger.error(
            "Publication journal recovery reported errors",
            **recovery,
        )
    # Turn the same recovery state into human notifications. Delivery is
    # asynchronous and durable; a failed delivery never blocks readiness and
    # leaves a pending record for the next retry.
    emit_startup_alerts(recovery)
    retry_pending_alerts_async()
    seed_default_contracts(registry)
    # Enforce configured ledger retention at startup. A non-positive retention
    # keeps history forever, so this is a no-op by default.
    try:
        pruned = BoundaryLedger().prune()
        if any(pruned.values()):
            telemetry_logger.info("Boundary ledger retention pruned rows", **pruned)
    except Exception as exc:  # noqa: BLE001 - retention must not block startup
        telemetry_logger.warning("Boundary ledger retention failed", error_type=type(exc).__name__)
    telemetry_logger.info(
        "Data Shell gateway started",
        environment=settings.environment,
        version=__version__,
    )
    try:
        yield
    finally:
        telemetry_logger.info("Data Shell gateway shutting down", version=__version__)


# Interactive API documentation is useful locally but unnecessarily expands the
# public attack surface in production. Production operators can use the generated
# OpenAPI schema from a trusted build artifact instead.
app = FastAPI(
    title="Data Shell Enterprise Gateway",
    description="Contract-driven perimeter boundary enforcement platform. Bad data never lands, and never leaves.",
    version=f"{__version__}-enterprise",
    lifespan=lifespan,
    docs_url=None if settings.is_production_mode else "/docs",
    redoc_url=None if settings.is_production_mode else "/redoc",
    openapi_url=None if settings.is_production_mode else "/openapi.json",
)

# CORS Middleware: In production, disallow wildcard '*' with credentials to protect browser security
cors_origins = [o.strip() for o in settings.allowed_origins if o.strip()]
if settings.is_production_mode and (not cors_origins or "*" in cors_origins):
    # Defense in depth in case settings are constructed or mutated unusually.
    raise RuntimeError("Production CORS requires an explicit ALLOWED_ORIGINS allowlist")
allow_creds = "*" not in cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_credentials=allow_creds,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
)

# Bound multipart parsing before FastAPI/Starlette spools the upload to disk.
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=settings.max_request_body_bytes,
    max_concurrent_requests=settings.max_concurrent_requests,
    max_control_body_bytes=settings.max_control_body_bytes,
    preflight_auth=preflight_authenticate_scope,
)



# ----------------- Middleware -----------------

SECURITY_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Attach baseline hardening headers to every gateway response.

    ``Cache-Control: no-store`` is set only when the handler did not already
    choose a caching policy (e.g. credential issuance sets its own), so
    authenticated payloads are never stored by shared caches by default.
    """
    response: Response = await call_next(request)
    for header, value in SECURITY_RESPONSE_HEADERS.items():
        response.headers.setdefault(header, value)
    if "cache-control" not in {key.lower() for key in response.headers}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def correlation_id_and_tracing(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Assign correlation IDs and emit bounded-cardinality request telemetry."""
    supplied_id = request.headers.get("X-Request-ID", "")
    corr_id = supplied_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_id) else f"trace_{uuid.uuid4().hex}"
    request.state.correlation_id = corr_id
    start_time = time.perf_counter()
    method = request.method.upper()
    HTTP_IN_FLIGHT_REQUESTS.inc()
    status_code = "500"
    with http_server_span_context(method) as span:
        try:
            response: Response = await call_next(request)
            status_code = str(response.status_code)
            latency = time.perf_counter() - start_time
            route_obj = request.scope.get("route")
            route_label = getattr(route_obj, "path", "unmatched")
            HTTP_REQUESTS_TOTAL.labels(method=method, route=route_label, status_code=status_code).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route_label).observe(latency)
            response.headers["X-Request-ID"] = corr_id
            response.headers["X-Response-Time-Ms"] = f"{latency * 1000.0:.2f}"
            if span is not None:
                span.update_name(f"{method} {route_label}")
                span.set_attribute("http.route", route_label)
                span.set_attribute("http.response.status_code", int(status_code))
            if int(status_code) >= 500:
                telemetry_logger.error(
                    "HTTP request failed", correlation_id=corr_id, method=method,
                    route=route_label, status_code=status_code,
                )
            return response
        except Exception:
            route_obj = request.scope.get("route")
            route_label = getattr(route_obj, "path", "unmatched")
            HTTP_REQUESTS_TOTAL.labels(method=method, route=route_label, status_code="500").inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route_label).observe(
                time.perf_counter() - start_time)
            telemetry_logger.error(
                "HTTP request raised an unhandled exception", correlation_id=corr_id,
                method=method, route=route_label, status_code="500",
            )
            raise
        finally:
            HTTP_IN_FLIGHT_REQUESTS.dec()


# ----------------- Core Observability Endpoints -----------------

@app.get("/health", summary="Liveness probe")
def healthcheck() -> dict[str, Any]:
    """Liveness probe for orchestrators, load balancers, and Kubernetes."""
    return {"status": "healthy", "service": "datashell"}


@app.get("/ready", summary="Readiness probe")
def readinesscheck() -> dict[str, Any]:
    """Readiness probe: fail closed when required persistent directories are unavailable."""
    required_dirs = (
        settings.data_dir, settings.contracts_dir, settings.output_dir,
        settings.audit_dir, settings.quarantine_dir,
    )
    if any(not path.is_dir() or not os.access(path, os.W_OK | os.X_OK) for path in required_dirs):
        raise HTTPException(status_code=503, detail="Persistent storage is not ready")
    try:
        disk = os.statvfs(settings.output_dir)
        available_bytes = disk.f_bavail * disk.f_frsize
        total_bytes = disk.f_blocks * disk.f_frsize
        STORAGE_FREE_BYTES.set(available_bytes)
        STORAGE_TOTAL_BYTES.set(total_bytes)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Persistent storage capacity cannot be checked") from exc
    if available_bytes < settings.min_free_disk_bytes:
        raise HTTPException(
            status_code=503,
            detail="Persistent storage is below the configured free-space safety threshold")
    if not security_registry_ready():
        raise HTTPException(status_code=503, detail="Credential registry is not ready")
    if not BoundaryLedger().check_integrity():
        raise HTTPException(status_code=503, detail="Boundary ledger is not ready")
    return {"status": "ready", "service": "datashell"}


@app.get("/metrics", summary="Prometheus metrics scrape")
def prometheus_metrics(
    client: APIKeyMetadata = Security(require_scope(Capability.METRICS_READ))
) -> Response:
    """Prometheus telemetry scrape endpoint (authenticated)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ----------------- Routers -----------------

app.include_router(contracts.router)
app.include_router(runs.router)
app.include_router(ledger.router)
app.include_router(settlements.router)
app.include_router(admin.router)

# Startup introspection: every endpoint that serves business data under /api
# must authenticate through a capability guard (require_scope). The check runs
# in lifespan, so a router endpoint that ships without an authorization
# dependency fails the process at boot instead of serving unauthenticated data.

def _iter_api_routes(application: FastAPI) -> Iterator[APIRoute]:
    """Yield every APIRoute, flattening included routers across Starlette versions."""
    stack: list[Any] = list(application.router.routes)
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, APIRoute):
            yield node
        else:
            stack.extend(getattr(node, "routes", None) or ())
            nested = getattr(node, "original_router", None)
            if nested is not None:
                stack.extend(getattr(nested, "routes", None) or ())


def assert_routes_require_scope(application: FastAPI) -> None:
    """Fail startup when an /api route declares no capability guard.

    Guards are recognized by their ``required_capability`` marker attribute,
    set by :func:`require_scope`. Probes (/health, /ready), the public
    document root, and the authenticated metrics scrape are the only
    non-/api routes by design.
    """
    unprotected: list[str] = []
    for route in _iter_api_routes(application):
        if not route.path.startswith("/api/"):
            continue
        guarded = any(
            getattr(dependency.call, "required_capability", None) is not None
            for dependency in route.dependant.dependencies
        )
        if not guarded and route.path not in unprotected:
            unprotected.append(route.path)
    if unprotected:
        raise RuntimeError(
            "API endpoints ship without an authorization scope: "
            + ", ".join(sorted(set(unprotected))))


assert_routes_require_scope(app)


# ----------------- Enterprise Web Console -----------------
#
# The console is a static, self-contained document (no external requests, no
# template engine). It renders only after the operator supplies an API key;
# every dashboard fetch goes to the same real gateway endpoints documented in
# the OpenAPI schema.

CONSOLE_HTML_PATH = Path(__file__).resolve().parent / "static" / "console.html"


@app.get("/", summary="Serve the enterprise console", response_class=HTMLResponse)
def serve_enterprise_console() -> str:
    """Serve the live enterprise console for this gateway deployment."""
    return CONSOLE_HTML_PATH.read_text(encoding="utf-8")

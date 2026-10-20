"""Enterprise FastAPI server for Data Shell Gateway.

Features:
- Multi-tenant RBAC & API Key Security
- Cryptographic Proof Verification & Non-Repudiation
- Prometheus /metrics Exporter
- Streaming Upload Bounded Protection
- Real-time Interactive Enterprise Control Console
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, ValidationError

from datashell import __version__
from datashell.contract.models import DataContract
from datashell.contract.registry import ContractRegistry
from datashell.core.commit import committed_generation_status
from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.core.security import (
    APIKeyMetadata,
    Capability,
    Role,
    preflight_authenticate_scope,
    register_api_key,
    require_capability,
    revoke_api_key_id,
    security_registry_ready,
    verify_tenant_access,
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
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import DataShellPipeline, recover_pending_publications_and_ledger
from datashell.server.middleware import RequestBodyLimitMiddleware
from datashell.settlement.ledger import SettlementLedgerError
from datashell.settlement.models import (
    CommercialContract,
    SettlementStatus,
)
from datashell.settlement.registry import (
    CommercialContractRegistryError,
)
from datashell.settlement.service import SettlementService, SettlementServiceError

registry = ContractRegistry(settings.contracts_dir)
WORKSPACE_DATA = settings.data_dir
WORKSPACE_DATA.mkdir(parents=True, exist_ok=True)


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

RUN_SEMAPHORE = asyncio.Semaphore(settings.max_concurrent_runs)


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
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route_label).observe(time.perf_counter() - start_time)
        telemetry_logger.error(
            "HTTP request raised an unhandled exception", correlation_id=corr_id,
            method=method, route=route_label, status_code="500",
        )
        raise
    finally:
        HTTP_IN_FLIGHT_REQUESTS.dec()


# ----------------- Core Observability Endpoints -----------------

@app.get("/health")
def healthcheck() -> dict[str, Any]:
    """Liveness probe for orchestrators, load balancers, and Kubernetes."""
    return {"status": "healthy", "service": "datashell"}


@app.get("/ready")
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


@app.get("/metrics")
def prometheus_metrics(
    client: APIKeyMetadata = Security(require_capability(Capability.METRICS_READ))
) -> Response:
    """Prometheus telemetry scrape endpoint (authenticated)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ----------------- Contract Catalog API -----------------

@app.post("/api/v1/contracts")
def register_contract(
    contract_payload: dict[str, Any],
    client: APIKeyMetadata = Security(require_capability(Capability.CONTRACTS_WRITE))
) -> dict[str, Any]:
    """Registers and versions a Data Contract immutably, scoped to tenant catalog."""
    try:
        contract = DataContract(**contract_payload)
        registry.save_contract(contract, tenant_id=client.tenant_id)
        telemetry_logger.info(
            "Contract registered",
            tenant=client.tenant_id,
            name=contract.metadata.name,
            version=contract.metadata.version
        )
        return {
            "status": "registered",
            "tenant_id": client.tenant_id,
            "name": contract.metadata.name,
            "version": contract.metadata.version,
        }
    except (ValidationError, ValueError) as e:
        telemetry_logger.warning("Contract registration rejected", tenant=client.tenant_id, error_type=type(e).__name__)
        raise HTTPException(
            status_code=400,
            detail="Contract validation failed; check the submitted contract schema") from e
    except Exception as e:
        telemetry_logger.error("Contract registration failed", tenant=client.tenant_id, error_type=type(e).__name__)
        raise HTTPException(
            status_code=500,
            detail="Contract registration failed; use the request ID when contacting an operator") from e


@app.get("/api/v1/contracts/{name}")
def get_contract(
    name: str,
    version: str | None = None,
    client: APIKeyMetadata = Security(require_capability(Capability.CONTRACTS_READ))
) -> dict[str, Any]:
    """Retrieves contract specification by name and optional point-in-time version within tenant scope."""
    try:
        if version:
            c = registry.get_version(name, version, tenant_id=client.tenant_id)
        else:
            c = registry.get_latest(name, tenant_id=client.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid contract identifier or version") from e

    if not c:
        raise HTTPException(status_code=404, detail=f"Contract '{name}' not found for tenant '{client.tenant_id}'")
    return c.model_dump(by_alias=True)


# ----------------- Boundary Enforcement API -----------------

MAX_UPLOAD_SIZE = settings.max_upload_size_bytes
ALLOWED_EXTENSIONS = {".parquet", ".csv", ".jsonl", ".ndjson"}


@app.post("/api/v1/enforce")
async def enforce_boundary(
    request: Request,
    contract_name: str = Form(...),
    file: UploadFile = File(...),
    streaming: bool = Form(False),
    client: APIKeyMetadata = Security(require_capability(Capability.PIPELINE_ENFORCE))
) -> dict[str, Any]:
    """
    Submits a data payload across the boundary for contract enforcement.
    Returns the reconciliation ledger and cryptographically signed audit certificate.
    """
    # Sanitize filename & extension first (defense against malicious payloads)
    original_name = Path(file.filename or "upload.parquet").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    try:
        contract = registry.get_latest(contract_name, tenant_id=client.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid contract identifier") from e

    if not contract:
        raise HTTPException(
            status_code=404,
            detail=f"Active contract '{contract_name}' not registered for tenant '{client.tenant_id}'")

    file_id = f"upload_{uuid.uuid4().hex[:12]}{ext}"
    temp_path = WORKSPACE_DATA / file_id

    # Stream upload in chunks to defend against memory exhaustion DoS
    total_bytes = 0
    chunk_size = 1024 * 1024  # 1 MB
    try:
        with open(temp_path, "wb") as buffer:
            while chunk := await file.read(chunk_size):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file exceeds maximum limit of {MAX_UPLOAD_SIZE // (1024 * 1024)} MB"
                    )
                buffer.write(chunk)
        # Release Starlette's spooled upload (often a tmpfs-backed file) before
        # running the heavier data pipeline.
        await file.close()
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        await file.close()
        raise

    try:
        if RUN_SEMAPHORE.locked():
            raise HTTPException(status_code=503, detail="Processing capacity is currently exhausted; retry later")
        # Protect the gateway from out-of-memory failures: route payloads at or
        # above the configured threshold through the out-of-core engine even when
        # the caller did not explicitly request streaming.
        staged_size_bytes = temp_path.stat().st_size
        use_streaming = bool(streaming) or staged_size_bytes >= settings.stream_auto_threshold_bytes
        execution_mode = "out_of_core_streaming" if use_streaming else "in_memory"
        async with RUN_SEMAPHORE:
            pipeline = DataShellPipeline(contract, tenant_id=client.tenant_id)
            # Execute processing in worker thread so event loop is never blocked.
            if use_streaming:
                res = await asyncio.to_thread(
                    pipeline.run_streaming,
                    source_path=temp_path,
                    chunk_size=settings.stream_chunk_rows,
                )
            else:
                res = await asyncio.to_thread(pipeline.run_file, source_path=temp_path)
        
        if res.audit_artifact_path is None:
            raise HTTPException(
                status_code=500,
                detail="Enforcement completed without a persisted certificate") from None
        with open(res.audit_artifact_path, encoding="utf-8") as f:
            certificate_data = json.load(f)

        return {
            "run_id": res.run_id,
            "tenant_id": client.tenant_id,
            "status": res.status.value,
            "execution_mode": execution_mode,
            "duration_seconds": res.duration_seconds,
            "reconciliation": {
                "rows_read": res.ledger.rows_read,
                "refused_structural": res.ledger.refused_structural,
                "held_quality": res.ledger.held_quality,
                "rows_delivered": res.outcome.rows_delivered,
                "candidate_rows": res.ledger.rows_to_deliver,
                "unaccounted_for": res.ledger.unaccounted_for,
                "footing_matched": res.ledger.footing_matched,
                "pct_delivered": round(res.ledger.pct_delivered, 2),
                "pct_refused_or_held": round(res.ledger.pct_refused_or_held, 2),
            },
            "rule_breakdown": res.ledger.rule_breakdown,
            "cryptographic_manifest": certificate_data.get("cryptographic_manifest", {}),
            "digital_signature": certificate_data.get("digital_signature", {}),
            "artifacts": {
                "delivered_artifact_name": Path(res.outcome.target_path).name if res.outcome.target_path else None,
                "quarantine_artifact_names": [Path(path).name for path in res.outcome.quarantine_files],
                "audit_certificate_id": res.run_id,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        telemetry_logger.error(
            "Boundary enforcement failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Boundary enforcement failed; use the request ID when contacting an operator") from e
    finally:
        if temp_path.exists():
            temp_path.unlink()
        await file.close()


@app.get("/api/v1/runs/{run_id}/certificate")
def get_run_certificate(
    run_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ))
) -> dict[str, Any]:
    """Retrieves the immutable cryptographic audit certificate for a specific
    run, with strict tenant ownership validation."""
    # Run IDs are generated by the service. Validate before using the identifier
    # in a filesystem path to prevent traversal via a caller-controlled URL.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    cert_path = settings.audit_dir / f"{run_id}_audit_certificate.json"
    if (
        not cert_path.is_file()
        or cert_path.is_symlink()
        or not cert_path.resolve().is_relative_to(settings.audit_dir.resolve())
    ):
        raise HTTPException(status_code=404, detail="Audit certificate not found")
    if cert_path.stat().st_size > 10 * 1024 * 1024:
        raise HTTPException(status_code=500, detail="Audit certificate exceeds the permitted size")

    with open(cert_path, encoding="utf-8") as f:
        cert_data = json.load(f)
    if not isinstance(cert_data, dict):
        raise HTTPException(status_code=500, detail="Audit certificate is malformed")

    # Multi-tenant isolation: an authenticated caller may only read certificates
    # for its own tenant (ADMIN/master credentials bypass this for oversight).
    # A cross-tenant request is explicitly denied rather than masked as "not
    # found" so operators get an accurate, auditable boundary violation.
    cert_tenant = cert_data.get("tenant_id", "default-tenant")
    verify_tenant_access(cert_tenant, client)

    # Recompute and verify signature + all underlying artifacts
    verification_report = ArtifactGenerator.verify_certificate_and_artifacts(cert_data)

    # Report whether this certificate describes the currently committed
    # generation. The certificate remains valid evidence for its own run, but a
    # consumer that wants "the current delivery" must resolve the generation
    # pointer. This is the same contract the CLI verifier applies.
    generation_status = committed_generation_status(cert_data)

    return {
        "certificate": cert_data,
        "verification": verification_report,
        "committed_generation": generation_status.get("committed_generation"),
        "is_committed_generation": generation_status.get("is_committed_generation"),
    }


# ----------------- Boundary Ledger Query API -----------------
#
# The ledger is the durable forensic system of record for every terminal run.
# These read-only endpoints make it queryable without shelling into the host,
# always tenant-scoped: a non-admin caller can only read its own tenant.

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _resolve_ledger_tenant(client: APIKeyMetadata, tenant_id: str | None) -> str:
    """Resolve the tenant to query, enforcing cross-tenant isolation.

    Administrators may target any tenant for oversight; every other role is
    pinned to its own tenant and receives an explicit denial for any other.
    """
    if tenant_id is None or tenant_id == client.tenant_id:
        return client.tenant_id
    verify_tenant_access(tenant_id, client)
    return tenant_id


def _bounded_limit(limit: int, default: int = 50, maximum: int = 500) -> int:
    if limit <= 0:
        return default
    return min(int(limit), maximum)


@app.get("/api/v1/runs/summary")
def ledger_run_summary(
    contract_name: str | None = None,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Aggregate delivery health for a tenant, optionally scoped to one contract."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().history_summary(scoped_tenant, contract_name=contract_name)
    return {"tenant_id": scoped_tenant, "contract_name": contract_name, **summary}


@app.get("/api/v1/runs")
def list_ledger_runs(
    contract_name: str | None = None,
    status_filter: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Newest-first forensic run records for a tenant with optional filters."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    ledger = BoundaryLedger()
    runs = ledger.list_runs(
        scoped_tenant,
        contract_name=contract_name,
        status=status_filter,
        limit=_bounded_limit(limit),
        offset=max(0, int(offset)),
    )
    total = ledger.count_runs(scoped_tenant, contract_name=contract_name, status=status_filter)
    return {"tenant_id": scoped_tenant, "total": total, "count": len(runs), "runs": runs}


@app.get("/api/v1/runs/{run_id}")
def get_ledger_run(
    run_id: str,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Full forensic ledger record for a single run, tenant-scoped.

    Administrators may target any tenant with ``tenant_id`` for oversight; every
    other role is pinned to its own tenant and denied explicitly for any other.
    """
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    run = BoundaryLedger().get_run(run_id, scoped_tenant)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found for the requested tenant")
    return run


@app.get("/api/v1/quarantine")
def list_ledger_quarantine(
    contract_name: str | None = None,
    run_id: str | None = None,
    kind: str | None = None,
    reason: str | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Search the quarantine index by contract, run, kind, or dominant reason."""
    if run_id is not None and not _RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    records = BoundaryLedger().list_quarantine(
        scoped_tenant,
        contract_name=contract_name,
        run_id=run_id,
        kind=kind,
        reason=reason,
        limit=_bounded_limit(limit, default=100),
    )
    return {"tenant_id": scoped_tenant, "count": len(records), "quarantine": records}


@app.get("/api/v1/usage")
def ledger_usage(
    contract_name: str | None = None,
    since: str | None = None,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_capability(Capability.METRICS_READ)),
) -> dict[str, Any]:
    """Metering totals for a tenant, optionally since an ISO-8601 timestamp."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().usage_summary(scoped_tenant, contract_name=contract_name, since=since)
    return {"contract_name": contract_name, **summary}


# ----------------- Commercial Contract & Settlement API -----------------
#
# Settlements turn verified delivery evidence into deterministic commercial
# obligations. Every endpoint is tenant-scoped; state-changing financial
# operations require idempotency keys and appropriate RBAC capabilities
# (SETTLEMENTS_WRITE, BILLING_MANAGE). Nothing here moves money: billing
# endpoints create draft billing instructions only.

_settlement_service = SettlementService()


class CommercialContractRequest(BaseModel):
    """Request body for registering a commercial contract (tenant-scoped)."""
    schema_version: int = Field(default=1, ge=1)
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: int = Field(default=1, ge=1)
    buyer_id: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    feed_id: str = Field(min_length=1, max_length=128)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing: dict[str, Any]
    qualification: dict[str, Any] = Field(default_factory=dict)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    penalty: dict[str, Any] = Field(default_factory=dict)
    effective_at: str
    expires_at: str | None = None
    approved_by: list[str] = Field(default_factory=list)
    description: str | None = None


@app.post("/api/v1/commercial-contracts", status_code=201)
def create_commercial_contract(
    payload: CommercialContractRequest,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_WRITE)),
) -> dict[str, Any]:
    """Register an immutable commercial contract version (tenant-scoped)."""
    try:
        contract = CommercialContract(**payload.model_dump())
        _settlement_service.register_contract(
            contract, tenant_id=client.tenant_id, created_by=f"api:{client.role.value}")
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail="Commercial contract validation failed; check the submitted schema") from exc
    except (CommercialContractRegistryError, SettlementLedgerError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    telemetry_logger.info(
        "Commercial contract registered",
        tenant=client.tenant_id,
        contract=contract.contract_id,
        version=contract.contract_version,
    )
    return {
        "status": "registered",
        "tenant_id": client.tenant_id,
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "contract_digest": contract.contract_digest,
    }


@app.get("/api/v1/commercial-contracts/{contract_id}")
def get_commercial_contract(
    contract_id: str,
    version: int | None = None,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Retrieve a commercial contract by id and optional exact version (tenant-scoped)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    contract = _settlement_service.get_contract(contract_id, client.tenant_id, version)
    if contract is None:
        raise HTTPException(status_code=404,
                            detail=f"Contract '{contract_id}' not found for tenant '{client.tenant_id}'")
    return {**contract.model_dump(mode="json"), "contract_digest": contract.contract_digest}


@app.get("/api/v1/commercial-contracts/{contract_id}/versions")
def list_commercial_contract_versions(
    contract_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """List all immutable versions of a commercial contract (tenant-scoped)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    versions = _settlement_service.registry.list_versions(contract_id, client.tenant_id)
    if not versions:
        raise HTTPException(status_code=404,
                            detail=f"Contract '{contract_id}' not found for tenant '{client.tenant_id}'")
    return {"contract_id": contract_id, "tenant_id": client.tenant_id, "versions": versions}


class SettlementCalculateRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    contract_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    contract_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


@app.post("/api/v1/settlements/calculate")
def calculate_settlement(
    payload: SettlementCalculateRequest,
    request: Request,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_WRITE)),
) -> dict[str, Any]:
    """Calculate (or idempotently replay) the settlement for a completed run.

    Verifies the delivery certificate before calculating; an invalid
    certificate, failed reconciliation, or unresolvable contract fails closed
    with 422 and never produces a payable statement.
    """
    supplied_key = payload.idempotency_key or request.headers.get("Idempotency-Key", "")
    idempotency_key = supplied_key or None
    try:
        row, created = _settlement_service.calculate(
            tenant_id=client.tenant_id,
            run_id=payload.run_id,
            contract_id=payload.contract_id,
            contract_version=payload.contract_version,
            actor=f"api:{client.role.value}",
            idempotency_key=idempotency_key,
        )
    except SettlementServiceError as exc:
        telemetry_logger.warning(
            "Settlement calculation refused",
            tenant=client.tenant_id,
            run=payload.run_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    telemetry_logger.info(
        "Settlement calculated",
        tenant=client.tenant_id,
        run=payload.run_id,
        created=created,
    )
    return {**row, "created": created}


@app.get("/api/v1/settlements")
def list_settlements(
    status: str | None = None,
    contract_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """List settlement statements for the caller's tenant (paginated)."""
    rows = _settlement_service.list_settlements(
        client.tenant_id, status=status, contract_id=contract_id)
    bounded = rows[max(0, int(offset)):max(0, int(offset)) + _bounded_limit(limit)]
    return {"tenant_id": client.tenant_id, "total": len(rows), "count": len(bounded),
            "settlements": bounded}


@app.get("/api/v1/settlements/{settlement_id}")
def get_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Retrieve one settlement statement (tenant-scoped)."""
    # Settlement IDs follow stl_<run>_<contract>_v<n>; validate loosely but
    # reject path-like input defensively.
    if not _RUN_ID_PATTERN.fullmatch(settlement_id.replace("stl_", "", 1).replace("_", "", 20)) and not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,160}", settlement_id
    ):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    row = _settlement_service.get_settlement(settlement_id, client.tenant_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Settlement '{settlement_id}' not found for tenant '{client.tenant_id}'")
    return row


@app.get("/api/v1/settlements/{settlement_id}/explanation")
def explain_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Full calculation explanation: units, price, thresholds, rounding, evidence."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    try:
        return _settlement_service.explain(settlement_id, client.tenant_id)
    except SettlementServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/settlements/{settlement_id}/verify")
def verify_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Verify a settlement's signed receipt against its delivery certificate."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    try:
        report = _settlement_service.verify_settlement(settlement_id, client.tenant_id)
    except SettlementServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return report


class DisputeRequest(BaseModel):
    disputing_party: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_references: list[str] = Field(default_factory=list)
    supporting_explanation: str = Field(default="", max_length=5000)


@app.post("/api/v1/settlements/{settlement_id}/disputes", status_code=201)
def open_settlement_dispute(
    settlement_id: str,
    payload: DisputeRequest,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_WRITE)),
) -> dict[str, Any]:
    """Open a dispute; the settlement becomes DISPUTED and non-billable."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    try:
        dispute = _settlement_service.open_dispute(
            settlement_id=settlement_id, tenant_id=client.tenant_id,
            disputing_party=payload.disputing_party, reason=payload.reason,
            evidence_references=payload.evidence_references,
            supporting_explanation=payload.supporting_explanation,
            actor=f"api:{client.role.value}")
    except SettlementServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return dispute.model_dump(mode="json")


class ApprovalRequest(BaseModel):
    action: str = Field(default="APPROVE_BILLING", pattern="^(APPROVE_BILLING)$")
    reason: str = Field(min_length=1, max_length=2000)


@app.post("/api/v1/settlements/{settlement_id}/approvals")
def approve_settlement(
    settlement_id: str,
    payload: ApprovalRequest,
    client: APIKeyMetadata = Security(require_capability(Capability.BILLING_MANAGE)),
) -> dict[str, Any]:
    """Explicitly approve a settlement for billing (BILLING_MANAGE capability).

    Approval records an audited transition; it never overrides the
    contractual calculation and never marks an unverified settlement as
    accepted.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    row = _settlement_service.get_settlement(settlement_id, client.tenant_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Settlement '{settlement_id}' not found for tenant '{client.tenant_id}'")
    if _settlement_service.ledger.has_open_dispute(settlement_id, client.tenant_id):
        raise HTTPException(status_code=409,
                            detail="an open dispute blocks billing approval; resolve the dispute first")
    if row["settlement_status"] != "READY_FOR_BILLING":
        raise HTTPException(status_code=409,
                            detail=f"settlement status '{row['settlement_status']}' is not approvable")
    _settlement_service.ledger.transition_status(
        settlement_id, client.tenant_id, SettlementStatus.READY_FOR_BILLING,
        f"api:{client.role.value}", f"explicit billing approval: {payload.reason}",
        event_type="BILLING_APPROVED", payload={"reason": payload.reason})
    return {"settlement_id": settlement_id, "status": "READY_FOR_BILLING",
            "approved": True, "actor": f"api:{client.role.value}"}


@app.post("/api/v1/settlements/{settlement_id}/billing-preview")
def settlement_billing_preview(
    settlement_id: str,
    request: Request,
    client: APIKeyMetadata = Security(require_capability(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Create or replay a draft billing instruction via the configured provider.

    Read-only against the settlement; the external action is a *draft* invoice.
    Requires the Idempotency-Key header for the state-changing external call.
    """
    from datashell.billing.base import InvoicePayload
    from datashell.billing.dry_run import DryRunBillingProvider
    from datashell.billing.stripe_adapter import StripeBillingProvider

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    idempotency_key = request.headers.get("Idempotency-Key", "") or None
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    row = _settlement_service.get_settlement(settlement_id, client.tenant_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Settlement '{settlement_id}' not found for tenant '{client.tenant_id}'")
    if row["settlement_status"] != "READY_FOR_BILLING":
        raise HTTPException(status_code=409,
                            detail=f"settlement status '{row['settlement_status']}' is not billable")
    if _settlement_service.ledger.has_open_dispute(settlement_id, client.tenant_id):
        raise HTTPException(status_code=409, detail="an open dispute blocks automatic billing actions")

    calc = row["calculation"]
    contract_id = row["contract_id"]
    contract_version = row["contract_version"]
    payload = InvoicePayload(
        tenant_id=client.tenant_id,
        settlement_id=settlement_id,
        contract_id=contract_id,
        contract_version=contract_version,
        run_id=row["run_id"],
        certificate_digest=row["certificate_digest"],
        currency=row["currency"],
        amount_minor=calc["gross_amount_minor"],
        description=f"Data delivery settlement {settlement_id} (run {row['run_id']})",
        idempotency_key=f"{client.tenant_id}:{idempotency_key}",
    )
    provider: StripeBillingProvider | DryRunBillingProvider = (
        StripeBillingProvider(_settlement_service.ledger)
        if settings.billing_provider == "stripe"
        else DryRunBillingProvider(_settlement_service.ledger)
    )
    result = provider.create_draft_invoice(payload, idempotency_key=payload.idempotency_key)
    return result.as_dict()



class CreateAPIKeyRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    role: Role = Role.PRODUCER
    description: str = Field(default="Managed API credential", min_length=1, max_length=200)


@app.post("/api/v1/admin/keys", status_code=201)
def create_managed_api_key(
    payload: CreateAPIKeyRequest,
    response: Response,
    client: APIKeyMetadata = Security(require_capability(Capability.ADMIN_MANAGE)),
) -> dict[str, Any]:
    """Create a credential and return its secret exactly once to an administrator."""
    raw_key = "dm_live_" + secrets.token_urlsafe(32)
    try:
        metadata = APIKeyMetadata(
            key=raw_key,
            tenant_id=payload.tenant_id,
            role=payload.role,
            description=payload.description,
        )
        register_api_key(metadata, persist=settings.is_production_mode, actor_tenant_id=client.tenant_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid tenant or credential metadata") from exc
    key_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    telemetry_logger.info(
        "API credential created",
        actor_tenant=client.tenant_id,
        target_tenant=metadata.tenant_id,
        role=metadata.role.value,
        key_id=key_id,
    )
    return {
        "key_id": key_id,
        "api_key": raw_key,
        "tenant_id": metadata.tenant_id,
        "role": metadata.role.value,
        "description": metadata.description,
        "warning": "Store this secret securely; it will not be shown again.",
    }


@app.delete("/api/v1/admin/keys/{key_id}")
def revoke_managed_api_key(
    key_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.ADMIN_MANAGE)),
) -> dict[str, Any]:
    """Revoke a managed key by its fingerprint. Revocation is durable in production."""
    if not re.fullmatch(r"[0-9a-f]{24}", key_id):
        raise HTTPException(status_code=400, detail="Invalid key ID")
    if not revoke_api_key_id(key_id, actor_tenant_id=client.tenant_id):
        raise HTTPException(status_code=404, detail="Managed API key not found")
    telemetry_logger.info("API credential revoked", actor_tenant=client.tenant_id, key_id=key_id)
    return {"status": "revoked", "key_id": key_id}


# ----------------- Interactive Enterprise Web Console -----------------

@app.get("/", response_class=HTMLResponse)
def serve_enterprise_ui() -> str:
    """Serves the live interactive Enterprise Data Shell Console."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Data Shell — Enterprise Perimeter Gateway</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:ital,wght@0,400;0,600;1,400&display=swap"
        rel="stylesheet">
  <style>
    :root {
      --bg: #0B0E14;
      --card-bg: #151922;
      --card-inner: #1C2230;
      --paper: #F1F3F6;
      --stain: #C1247E;
      --stain-hover: #E24C9F;
      --text-mute: #8A97A8;
      --rule: #2C3543;
      --accent-blue: #3B82F6;
      --accent-green: #10B981;
      --accent-amber: #F59E0B;
    }
    body {
      background: var(--bg);
      color: #fff;
      font-family: 'Source Serif 4', Georgia, serif;
      margin: 0;
      padding: 0;
      line-height: 1.6;
    }
    header {
      border-bottom: 1px solid var(--rule);
      padding: 16px 40px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #0E121A;
    }
    .logo {
      font-family: 'Archivo', sans-serif;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
      font-size: 15px;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .badge {
      font-family: 'IBM Plex Mono', monospace;
      font-size: 10px;
      padding: 3px 8px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 600;
    }
    .badge-live { background: rgba(16, 185, 129, 0.15); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-crypto { background: rgba(59, 130, 246, 0.15); color: #60A5FA; border: 1px solid rgba(59, 130, 246, 0.3); }

    .container {
      max-width: 1200px;
      margin: 32px auto;
      padding: 0 24px;
    }
    .hero-title {
      font-family: 'Archivo', sans-serif;
      font-size: 38px;
      font-weight: 800;
      line-height: 1.1;
      margin-bottom: 10px;
    }
    .hero-sub {
      color: #A0AEC0;
      font-size: 17px;
      margin-bottom: 28px;
      max-width: 75ch;
    }
    
    .stats-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      margin-bottom: 28px;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--rule);
      border-radius: 6px;
      padding: 16px 20px;
    }
    .stat-label {
      font-family: 'IBM Plex Mono', monospace;
      font-size: 11px;
      color: var(--text-mute);
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .stat-val {
      font-family: 'Archivo', sans-serif;
      font-size: 24px;
      font-weight: 700;
      margin-top: 4px;
    }

    .grid {
      display: grid;
      grid-template-columns: 440px 1fr;
      gap: 24px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--rule);
      border-radius: 8px;
      padding: 24px;
    }
    .card h3 {
      font-family: 'Archivo', sans-serif;
      margin-top: 0;
      margin-bottom: 16px;
      font-size: 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    label {
      display: block;
      font-family: 'IBM Plex Mono', monospace;
      font-size: 11px;
      color: var(--text-mute);
      margin-bottom: 6px;
      text-transform: uppercase;
    }
    input, select, textarea {
      width: 100%;
      background: var(--card-inner);
      border: 1px solid var(--rule);
      color: #fff;
      padding: 10px 12px;
      border-radius: 4px;
      font-family: 'IBM Plex Mono', monospace;
      font-size: 13px;
      margin-bottom: 16px;
      box-sizing: border-box;
    }
    button {
      background: var(--stain);
      color: #fff;
      border: none;
      padding: 12px 24px;
      font-family: 'IBM Plex Mono', monospace;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: .05em;
      cursor: pointer;
      border-radius: 4px;
      transition: background .18s;
      width: 100%;
    }
    button:hover { background: var(--stain-hover); }
    .ledger-box {
      background: #07090D;
      border: 1px solid var(--rule);
      border-radius: 6px;
      padding: 18px;
      font-family: 'IBM Plex Mono', monospace;
      font-size: 12.5px;
      white-space: pre-wrap;
      color: #DCE3EB;
      min-height: 250px;
    }
    .crypto-manifest {
      margin-top: 18px;
      padding: 14px;
      background: rgba(59, 130, 246, 0.05);
      border: 1px dashed rgba(59, 130, 246, 0.3);
      border-radius: 6px;
      font-family: 'IBM Plex Mono', monospace;
      font-size: 11.5px;
      color: #93C5FD;
      display: none;
    }
  </style>
</head>
<body>
  <header>
    <div class="logo">
      <span style="color:var(--stain)">●</span> DATA SHELL
      <span class="badge badge-live">Enterprise v1.0</span>
      <span class="badge badge-crypto">HMAC-SHA256 Signed</span>
    </div>
    <div style="font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--text-mute);">
      Zero-Deficit Boundary Gateway
    </div>
  </header>

  <div class="container">
    <div class="hero-title">Bad data never lands, and never leaves.</div>
    <div class="hero-sub">Enterprise contract enforcement platform with zero-deficit mathematical footing,
      dual vectorized engines (Polars + DuckDB), and cryptographic non-repudiation.</div>

    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-label">Enforcement Engine</div>
        <div class="stat-val" style="color:#60A5FA">Polars + DuckDB</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Reconciliation Axiom</div>
        <div class="stat-val" style="color:#34D399">0 Unaccounted</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Throughput Benchmark</div>
        <div class="stat-val" style="color:#F472B6">41,500 rows/s</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Audit Lineage</div>
        <div class="stat-val" style="color:#FBBF24">Merkle Chained</div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h3>Enforce Boundary</h3>
        <form id="enforceForm">
          <label>Contract Specification</label>
          <select id="contractName">
            <option value="nyc-tlc-yellow-tripdata">nyc-tlc-yellow-tripdata (TLC Mobility Rules)</option>
            <option value="vendor-settlements">vendor-settlements (Financial Settlements)</option>
          </select>

          <label>Data Payload File (Parquet, CSV, JSONL)</label>
          <input type="file" id="dataFile" required>

          <label>Enterprise API Key</label>
          <input type="text" id="apiKey" value="" autocomplete="off" placeholder="Enter X-API-Key">

          <button type="submit" id="submitBtn">Enforce Contract Gate</button>
        </form>
      </div>

      <div class="card">
        <h3>Run Reconciliation Ledger</h3>
        <div id="outputLedger" class="ledger-box">Awaiting boundary run... Upload a payload to
          enforce contracts and compute zero-deficit footing.</div>
        
        <div id="cryptoManifest" class="crypto-manifest">
          <strong style="color:#fff">CRYPTOGRAPHIC AUDIT CERTIFICATE:</strong><br>
          <span id="merkleRoot"></span><br>
          <span id="sigDigest"></span>
        </div>
      </div>
    </div>
  </div>

  <script>
    document.getElementById('enforceForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('submitBtn');
      const out = document.getElementById('outputLedger');
      const cryptoBox = document.getElementById('cryptoManifest');
      
      btn.disabled = true;
      btn.innerText = "Evaluating Boundary...";
      out.innerText = "Streaming data into memory buffer and compiling vectorized rules...";
      cryptoBox.style.display = 'none';

      const formData = new FormData();
      formData.append('contract_name', document.getElementById('contractName').value);
      formData.append('file', document.getElementById('dataFile').files[0]);

      const apiKey = document.getElementById('apiKey').value;

      try {
        const resp = await fetch('/api/v1/enforce', {
          method: 'POST',
          headers: apiKey ? { 'X-API-Key': apiKey } : {},
          body: formData
        });
        const data = await resp.json();
        
        if (!resp.ok) {
          out.innerText = "GATEWAY ERROR (" + resp.status + "):\\n" + (data.detail || JSON.stringify(data));
          return;
        }

        const r = data.reconciliation;
        let ledgerText = "┌─────────────────────────────────────────────────────────────┐\\n";
        ledgerText += "│           DATA SHELL — RUN RECONCILIATION LEDGER         │\\n";
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Run ID:                ${data.run_id.padEnd(36)} │\\n`;
        ledgerText += `│ Terminal Status:       ${data.status.padEnd(36)} │\\n`;
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Rows read from source:                    ${r.rows_read.toLocaleString().padStart(17)} │\\n`;
        ledgerText += `│ Refused at the door (structural):        -${r.refused_structural
          .toLocaleString().padStart(17)} │\\n`;
        ledgerText += `│ Held by quality rules:                   -${r.held_quality
          .toLocaleString().padStart(17)} │\\n`;
        ledgerText += `│ Rows delivered:                           ${r.rows_delivered
          .toLocaleString().padStart(17)} │\\n`;
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Unaccounted for:                          ${(r.unaccounted_for
          + " · Matched").padStart(17)} │\\n`;
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Delivered: ${r.pct_delivered.toFixed(2).padStart(6)}%    |
          |   Refused / Held: ${r.pct_refused_or_held.toFixed(2).padStart(6)}%       │\\n`;
        ledgerText += "└─────────────────────────────────────────────────────────────┘\\n\\n";
        ledgerText += "Rule Violations Held:\\n" + JSON.stringify(data.rule_breakdown, null, 2);

        out.innerText = ledgerText;

        if (data.cryptographic_manifest && data.digital_signature) {
          cryptoBox.style.display = 'block';
          document.getElementById('merkleRoot').innerText = "Merkle Root: " + data.cryptographic_manifest.merkle_root;
          document.getElementById('sigDigest').innerText = "Signature:   "
            + data.digital_signature.signature.substring(0, 32)
            + "... (" + (data.digital_signature.algorithm || "HMAC-SHA256") + ")";
        }
      } catch (err) {
        out.innerText = "Network or execution error: " + err.message;
      } finally {
        btn.disabled = false;
        btn.innerText = "Enforce Contract Gate";
      }
    });
  </script>
</body>
</html>
    """
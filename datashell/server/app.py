"""Enterprise FastAPI server for Data Shell Gateway.

Features:
- Multi-tenant RBAC & API Key Security
- Cryptographic Proof Verification & Non-Repudiation
- Prometheus /metrics Exporter
- Streaming Upload Bounded Protection
- Real-time Interactive Enterprise Control Console
"""

from __future__ import annotations
import json
import uuid
import time
import asyncio
import os
import re
import secrets
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, ValidationError

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Security, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from datashell.server.middleware import RequestBodyLimitMiddleware
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from datashell.core.config import settings
from datashell.core.commit import recover_pending_publications
from datashell import __version__
from datashell.core.security import (
    authenticate_client, require_capability, verify_tenant_access, Capability, APIKeyMetadata,
    API_KEY_STORE, Role, register_api_key, revoke_api_key_id, security_registry_ready,
    preflight_authenticate_scope,
)
from datashell.core.crypto import CryptoEngine
from datashell.core.ledger import BoundaryLedger
from datashell.core.telemetry import (
    telemetry_logger, HTTP_REQUESTS_TOTAL, HTTP_REQUEST_DURATION_SECONDS, HTTP_IN_FLIGHT_REQUESTS,
    STORAGE_FREE_BYTES, STORAGE_TOTAL_BYTES,
)
from datashell.contract.models import DataContract
from datashell.contract.registry import ContractRegistry
from datashell.pipeline.runner import DataShellPipeline
from datashell.pipeline.artifacts import ArtifactGenerator


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
                with open(contract_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
                telemetry_logger.warning("Failed to auto-seed example contract", file=contract_file.name, error_type=type(e).__name__)
                if settings.is_production_mode:
                    # A production deployment must not silently become ready
                    # with a partially initialized shared contract catalog.
                    raise RuntimeError("Production contract catalog initialization failed") from e

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recovery is a startup gate: do not mutate contract state, accept requests,
    # or report readiness until every interrupted publication has been examined.
    # In particular, avoid seeding contracts at module import time because an
    # ASGI server imports the module before it runs the lifespan startup phase.
    recover_pending_publications(settings.output_dir)
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

@app.middleware("http")
async def correlation_id_and_tracing(request: Request, call_next):
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
def healthcheck():
    """Liveness probe for orchestrators, load balancers, and Kubernetes."""
    return {"status": "healthy", "service": "data-shell"}


@app.get("/ready")
def readinesscheck():
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
        raise HTTPException(status_code=503, detail="Persistent storage is below the configured free-space safety threshold")
    if not security_registry_ready():
        raise HTTPException(status_code=503, detail="Credential registry is not ready")
    if not BoundaryLedger().check_integrity():
        raise HTTPException(status_code=503, detail="Boundary ledger is not ready")
    return {"status": "ready", "service": "data-shell"}


@app.get("/metrics")
def prometheus_metrics(
    client: APIKeyMetadata = Security(require_capability(Capability.METRICS_READ))
):
    """Prometheus telemetry scrape endpoint (authenticated)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ----------------- Contract Catalog API -----------------

@app.post("/api/v1/contracts")
def register_contract(
    contract_payload: Dict[str, Any],
    client: APIKeyMetadata = Security(require_capability(Capability.CONTRACTS_WRITE))
):
    """Registers and versions a Data Contract immutably, scoped to tenant catalog."""
    try:
        contract = DataContract(**contract_payload)
        saved_path = registry.save_contract(contract, tenant_id=client.tenant_id)
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
        raise HTTPException(status_code=400, detail="Contract validation failed; check the submitted contract schema") from e
    except Exception as e:
        telemetry_logger.error("Contract registration failed", tenant=client.tenant_id, error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail="Contract registration failed; use the request ID when contacting an operator") from e


@app.get("/api/v1/contracts/{name}")
def get_contract(
    name: str,
    version: Optional[str] = None,
    client: APIKeyMetadata = Security(require_capability(Capability.CONTRACTS_READ))
):
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
):
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
        raise HTTPException(status_code=404, detail=f"Active contract '{contract_name}' not registered for tenant '{client.tenant_id}'")

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
        
        with open(res.audit_artifact_path, "r", encoding="utf-8") as f:
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
        raise HTTPException(status_code=500, detail="Boundary enforcement failed; use the request ID when contacting an operator") from e
    finally:
        if temp_path.exists():
            temp_path.unlink()
        await file.close()


@app.get("/api/v1/runs/{run_id}/certificate")
def get_run_certificate(
    run_id: str,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ))
):
    """Retrieves the immutable cryptographic audit certificate for a specific run, with strict tenant ownership validation."""
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

    with open(cert_path, "r", encoding="utf-8") as f:
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
    
    return {
        "certificate": cert_data,
        "verification": verification_report
    }


# ----------------- Boundary Ledger Query API -----------------
#
# The ledger is the durable forensic system of record for every terminal run.
# These read-only endpoints make it queryable without shelling into the host,
# always tenant-scoped: a non-admin caller can only read its own tenant.

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _resolve_ledger_tenant(client: APIKeyMetadata, tenant_id: Optional[str]) -> str:
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
    contract_name: Optional[str] = None,
    tenant_id: Optional[str] = None,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
):
    """Aggregate delivery health for a tenant, optionally scoped to one contract."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().history_summary(scoped_tenant, contract_name=contract_name)
    return {"tenant_id": scoped_tenant, "contract_name": contract_name, **summary}


@app.get("/api/v1/runs")
def list_ledger_runs(
    contract_name: Optional[str] = None,
    status_filter: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
):
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
    tenant_id: Optional[str] = None,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
):
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
    contract_name: Optional[str] = None,
    run_id: Optional[str] = None,
    kind: Optional[str] = None,
    reason: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 100,
    client: APIKeyMetadata = Security(require_capability(Capability.AUDIT_READ)),
):
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
    contract_name: Optional[str] = None,
    since: Optional[str] = None,
    tenant_id: Optional[str] = None,
    client: APIKeyMetadata = Security(require_capability(Capability.METRICS_READ)),
):
    """Metering totals for a tenant, optionally since an ISO-8601 timestamp."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().usage_summary(scoped_tenant, contract_name=contract_name, since=since)
    return {"contract_name": contract_name, **summary}



class CreateAPIKeyRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    role: Role = Role.PRODUCER
    description: str = Field(default="Managed API credential", min_length=1, max_length=200)


@app.post("/api/v1/admin/keys", status_code=201)
def create_managed_api_key(
    payload: CreateAPIKeyRequest,
    response: Response,
    client: APIKeyMetadata = Security(require_capability(Capability.ADMIN_MANAGE)),
):
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
):
    """Revoke a managed key by its fingerprint. Revocation is durable in production."""
    if not re.fullmatch(r"[0-9a-f]{24}", key_id):
        raise HTTPException(status_code=400, detail="Invalid key ID")
    if not revoke_api_key_id(key_id, actor_tenant_id=client.tenant_id):
        raise HTTPException(status_code=404, detail="Managed API key not found")
    telemetry_logger.info("API credential revoked", actor_tenant=client.tenant_id, key_id=key_id)
    return {"status": "revoked", "key_id": key_id}


# ----------------- Interactive Enterprise Web Console -----------------

@app.get("/", response_class=HTMLResponse)
def serve_enterprise_ui():
    """Serves the live interactive Enterprise Data Shell Console."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Data Shell — Enterprise Perimeter Gateway</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
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
    <div class="hero-sub">Enterprise contract enforcement platform with zero-deficit mathematical footing, dual vectorized engines (Polars + DuckDB), and cryptographic non-repudiation.</div>

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
        <div id="outputLedger" class="ledger-box">Awaiting boundary run... Upload a payload to enforce contracts and compute zero-deficit footing.</div>
        
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
        ledgerText += `│ Refused at the door (structural):        -${r.refused_structural.toLocaleString().padStart(17)} │\\n`;
        ledgerText += `│ Held by quality rules:                   -${r.held_quality.toLocaleString().padStart(17)} │\\n`;
        ledgerText += `│ Rows delivered:                           ${r.rows_delivered.toLocaleString().padStart(17)} │\\n`;
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Unaccounted for:                          ${(r.unaccounted_for + " · Matched").padStart(17)} │\\n`;
        ledgerText += "├─────────────────────────────────────────────────────────────┤\\n";
        ledgerText += `│ Delivered: ${r.pct_delivered.toFixed(2).padStart(6)}%    |   Refused / Held: ${r.pct_refused_or_held.toFixed(2).padStart(6)}%       │\\n`;
        ledgerText += "└─────────────────────────────────────────────────────────────┘\\n\\n";
        ledgerText += "Rule Violations Held:\\n" + JSON.stringify(data.rule_breakdown, null, 2);

        out.innerText = ledgerText;

        if (data.cryptographic_manifest && data.digital_signature) {
          cryptoBox.style.display = 'block';
          document.getElementById('merkleRoot').innerText = "Merkle Root: " + data.cryptographic_manifest.merkle_root;
          document.getElementById('sigDigest').innerText = "Signature:   " + data.digital_signature.signature.substring(0, 32) + "... (" + (data.digital_signature.algorithm || "HMAC-SHA256") + ")";
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

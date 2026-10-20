"""Boundary enforcement and run-certificate retrieval endpoints."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Security, UploadFile

from datashell.core.commit import committed_generation_status
from datashell.core.config import settings
from datashell.core.security import APIKeyMetadata, Capability, require_capability, verify_tenant_access
from datashell.core.telemetry import telemetry_logger
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import EXECUTION_STREAMING, DataShellPipeline, choose_execution_backend
from datashell.server.state import ALLOWED_EXTENSIONS, MAX_UPLOAD_SIZE, RUN_SEMAPHORE, WORKSPACE_DATA, registry

router = APIRouter()


@router.post("/api/v1/enforce")
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
        execution_mode = choose_execution_backend(
            staged_size_bytes,
            force_streaming=True if streaming else None,
            ingest_mode=settings.ingest_mode,
            auto_threshold_bytes=settings.stream_auto_threshold_bytes,
        )
        use_streaming = execution_mode == EXECUTION_STREAMING
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


@router.get("/api/v1/runs/{run_id}/certificate")
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

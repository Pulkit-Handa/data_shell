"""Administrator endpoints: managed API-credential lifecycle."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Response, Security
from pydantic import BaseModel, Field

from datashell.core.config import settings
from datashell.core.security import (
    APIKeyMetadata,
    Capability,
    Role,
    register_api_key,
    require_scope,
    revoke_api_key_id,
)
from datashell.core.telemetry import telemetry_logger

router = APIRouter()


class CreateAPIKeyRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    role: Role = Role.PRODUCER
    description: str = Field(default="Managed API credential", min_length=1, max_length=200)


@router.post("/api/v1/admin/keys", summary="Issue a managed API credential", status_code=201)
def create_managed_api_key(
    payload: CreateAPIKeyRequest,
    response: Response,
    client: APIKeyMetadata = Security(require_scope(Capability.ADMIN_MANAGE)),
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


@router.delete("/api/v1/admin/keys/{key_id}", summary="Revoke a managed API credential")
def revoke_managed_api_key(
    key_id: str,
    client: APIKeyMetadata = Security(require_scope(Capability.ADMIN_MANAGE)),
) -> dict[str, Any]:
    """Revoke a managed key by its fingerprint. Revocation is durable in production."""
    if not re.fullmatch(r"[0-9a-f]{24}", key_id):
        raise HTTPException(status_code=400, detail="Invalid key ID")
    if not revoke_api_key_id(key_id, actor_tenant_id=client.tenant_id):
        raise HTTPException(status_code=404, detail="Managed API key not found")
    telemetry_logger.info("API credential revoked", actor_tenant=client.tenant_id, key_id=key_id)
    return {"status": "revoked", "key_id": key_id}

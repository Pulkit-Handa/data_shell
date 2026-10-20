"""Contract catalog endpoints (register, read, list for the caller's tenant)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Security
from pydantic import ValidationError

from datashell.contract.models import DataContract
from datashell.core.security import APIKeyMetadata, Capability, require_scope
from datashell.core.telemetry import telemetry_logger
from datashell.server.state import registry

router = APIRouter()


@router.post("/api/v1/contracts", summary="Register a data-contract version")
def register_contract(
    contract_payload: dict[str, Any],
    client: APIKeyMetadata = Security(require_scope(Capability.CONTRACTS_WRITE))
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


@router.get("/api/v1/contracts/{name}", summary="Read a data-contract version")
def get_contract(
    name: str,
    version: str | None = None,
    client: APIKeyMetadata = Security(require_scope(Capability.CONTRACTS_READ))
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


@router.get("/api/v1/contracts", summary="List visible contract names")
def list_contracts(
    client: APIKeyMetadata = Security(require_scope(Capability.CONTRACTS_READ)),
) -> dict[str, Any]:
    """Contract-name catalog visible to the caller's tenant (own + shared).

    Powers client discovery (CLI completion, the console contract selector):
    names and active versions only, never full contract payloads.
    """
    entries = registry.list_contracts(tenant_id=client.tenant_id)
    return {"tenant_id": client.tenant_id, "count": len(entries), "contracts": entries}

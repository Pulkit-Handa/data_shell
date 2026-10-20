"""Boundary-ledger query endpoints: run records, quarantine, usage metering."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Security

from datashell.core.ledger import BoundaryLedger
from datashell.core.security import APIKeyMetadata, Capability, require_scope, verify_tenant_access
from datashell.server.state import RUN_ID_PATTERN, bounded_limit

router = APIRouter()


# ----------------- Boundary Ledger Query API -----------------
#
# The ledger is the durable forensic system of record for every terminal run.
# These read-only endpoints make it queryable without shelling into the host,
# always tenant-scoped: a non-admin caller can only read its own tenant.

def _resolve_ledger_tenant(client: APIKeyMetadata, tenant_id: str | None) -> str:
    """Resolve the tenant to query, enforcing cross-tenant isolation.

    Administrators may target any tenant for oversight; every other role is
    pinned to its own tenant and receives an explicit denial for any other.
    """
    if tenant_id is None or tenant_id == client.tenant_id:
        return client.tenant_id
    verify_tenant_access(tenant_id, client)
    return tenant_id


@router.get("/api/v1/runs/summary", summary="Aggregate run health for a tenant")
def ledger_run_summary(
    contract_name: str | None = None,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_scope(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Aggregate delivery health for a tenant, optionally scoped to one contract."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().history_summary(scoped_tenant, contract_name=contract_name)
    return {"tenant_id": scoped_tenant, "contract_name": contract_name, **summary}


@router.get("/api/v1/runs", summary="List forensic run records")
def list_ledger_runs(
    contract_name: str | None = None,
    status_filter: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    client: APIKeyMetadata = Security(require_scope(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Newest-first forensic run records for a tenant with optional filters."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    ledger = BoundaryLedger()
    runs = ledger.list_runs(
        scoped_tenant,
        contract_name=contract_name,
        status=status_filter,
        limit=bounded_limit(limit),
        offset=max(0, int(offset)),
    )
    total = ledger.count_runs(scoped_tenant, contract_name=contract_name, status=status_filter)
    return {"tenant_id": scoped_tenant, "total": total, "count": len(runs), "runs": runs}


@router.get("/api/v1/runs/{run_id}", summary="Read one forensic run record")
def get_ledger_run(
    run_id: str,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_scope(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Full forensic ledger record for a single run, tenant-scoped.

    Administrators may target any tenant with ``tenant_id`` for oversight; every
    other role is pinned to its own tenant and denied explicitly for any other.
    """
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    run = BoundaryLedger().get_run(run_id, scoped_tenant)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found for the requested tenant")
    return run


@router.get("/api/v1/quarantine", summary="Search the quarantine index")
def list_ledger_quarantine(
    contract_name: str | None = None,
    run_id: str | None = None,
    kind: str | None = None,
    reason: str | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
    client: APIKeyMetadata = Security(require_scope(Capability.AUDIT_READ)),
) -> dict[str, Any]:
    """Search the quarantine index by contract, run, kind, or dominant reason."""
    if run_id is not None and not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    records = BoundaryLedger().list_quarantine(
        scoped_tenant,
        contract_name=contract_name,
        run_id=run_id,
        kind=kind,
        reason=reason,
        limit=bounded_limit(limit, default=100),
    )
    return {"tenant_id": scoped_tenant, "count": len(records), "quarantine": records}


@router.get("/api/v1/usage", summary="Metering totals for a tenant")
def ledger_usage(
    contract_name: str | None = None,
    since: str | None = None,
    tenant_id: str | None = None,
    client: APIKeyMetadata = Security(require_scope(Capability.METRICS_READ)),
) -> dict[str, Any]:
    """Metering totals for a tenant, optionally since an ISO-8601 timestamp."""
    scoped_tenant = _resolve_ledger_tenant(client, tenant_id)
    summary = BoundaryLedger().usage_summary(scoped_tenant, contract_name=contract_name, since=since)
    return {"contract_name": contract_name, **summary}

"""Commercial contract and settlement lifecycle endpoints (tenant-scoped)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Security
from pydantic import BaseModel, Field, ValidationError

from datashell.core.config import settings
from datashell.core.security import APIKeyMetadata, Capability, require_scope
from datashell.core.telemetry import telemetry_logger
from datashell.server.state import RUN_ID_PATTERN, bounded_limit
from datashell.settlement.ledger import SettlementLedgerError
from datashell.settlement.models import CommercialContract, SettlementStatus
from datashell.settlement.registry import CommercialContractRegistryError
from datashell.settlement.service import SettlementService, SettlementServiceError

router = APIRouter()


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


@router.post("/api/v1/commercial-contracts", summary="Register a commercial-contract version", status_code=201)
def create_commercial_contract(
    payload: CommercialContractRequest,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_WRITE)),
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


@router.get("/api/v1/commercial-contracts/{contract_id}", summary="Read a commercial-contract version")
def get_commercial_contract(
    contract_id: str,
    version: int | None = None,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Retrieve a commercial contract by id and optional exact version (tenant-scoped)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    contract = _settlement_service.get_contract(contract_id, client.tenant_id, version)
    if contract is None:
        raise HTTPException(status_code=404,
                            detail=f"Contract '{contract_id}' not found for tenant '{client.tenant_id}'")
    return {**contract.model_dump(mode="json"), "contract_digest": contract.contract_digest}


@router.get("/api/v1/commercial-contracts/{contract_id}/versions", summary="List commercial-contract versions")
def list_commercial_contract_versions(
    contract_id: str,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
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


@router.post("/api/v1/settlements/calculate", summary="Calculate (or idempotently replay) a settlement")
def calculate_settlement(
    payload: SettlementCalculateRequest,
    request: Request,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_WRITE)),
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


@router.get("/api/v1/settlements", summary="List settlement statements")
def list_settlements(
    status: str | None = None,
    contract_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """List settlement statements for the caller's tenant (paginated)."""
    rows = _settlement_service.list_settlements(
        client.tenant_id, status=status, contract_id=contract_id)
    bounded = rows[max(0, int(offset)):max(0, int(offset)) + bounded_limit(limit)]
    return {"tenant_id": client.tenant_id, "total": len(rows), "count": len(bounded),
            "settlements": bounded}


@router.get("/api/v1/settlements/{settlement_id}", summary="Read a settlement statement")
def get_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Retrieve one settlement statement (tenant-scoped)."""
    # Settlement IDs follow stl_<run>_<contract>_v<n>; validate loosely but
    # reject path-like input defensively.
    if not RUN_ID_PATTERN.fullmatch(settlement_id.replace("stl_", "", 1).replace("_", "", 20)) and not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,160}", settlement_id
    ):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    row = _settlement_service.get_settlement(settlement_id, client.tenant_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Settlement '{settlement_id}' not found for tenant '{client.tenant_id}'")
    return row


@router.get("/api/v1/settlements/{settlement_id}/explanation", summary="Explain a settlement calculation")
def explain_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Full calculation explanation: units, price, thresholds, rounding, evidence."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", settlement_id):
        raise HTTPException(status_code=400, detail="Invalid settlement ID")
    try:
        return _settlement_service.explain(settlement_id, client.tenant_id)
    except SettlementServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/settlements/{settlement_id}/verify", summary="Verify a settlement receipt")
def verify_settlement(
    settlement_id: str,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
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


@router.post("/api/v1/settlements/{settlement_id}/disputes", summary="Open a settlement dispute", status_code=201)
def open_settlement_dispute(
    settlement_id: str,
    payload: DisputeRequest,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_WRITE)),
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


@router.post("/api/v1/settlements/{settlement_id}/approvals", summary="Approve a settlement for billing")
def approve_settlement(
    settlement_id: str,
    payload: ApprovalRequest,
    client: APIKeyMetadata = Security(require_scope(Capability.BILLING_MANAGE)),
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
    try:
        _settlement_service.transition_status(
            settlement_id=settlement_id, tenant_id=client.tenant_id,
            to_status=SettlementStatus.READY_FOR_BILLING,
            actor=f"api:{client.role.value}",
            reason=f"explicit billing approval: {payload.reason}",
            event_type="BILLING_APPROVED", payload={"reason": payload.reason})
    except SettlementServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"settlement_id": settlement_id, "status": "READY_FOR_BILLING",
            "approved": True, "actor": f"api:{client.role.value}"}


@router.post("/api/v1/settlements/{settlement_id}/billing-preview", summary="Draft a billing instruction")
def settlement_billing_preview(
    settlement_id: str,
    request: Request,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
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


@router.get("/api/v1/settlement/preview", summary="Roll up the settlement portfolio for dashboards")
def settlement_preview(
    limit: int = 50,
    client: APIKeyMetadata = Security(require_scope(Capability.SETTLEMENTS_READ)),
) -> dict[str, Any]:
    """Dashboard-facing aggregate of the tenant's settlement portfolio.

    A bounded read-only roll-up of the durable settlement ledger: totals by
    currency, status distribution, and the newest statements. It performs no
    calculation, transition, or billing action.
    """
    rows = _settlement_service.list_settlements(client.tenant_id)
    recent = rows[:bounded_limit(limit)]
    totals: dict[str, dict[str, int]] = {}
    statuses: dict[str, int] = {}
    for row in rows:
        bucket = totals.setdefault(
            row["currency"], {"settlement_count": 0, "gross_amount_minor": 0})
        bucket["settlement_count"] += 1
        bucket["gross_amount_minor"] += int(row["gross_amount_minor"])
        statuses[row["settlement_status"]] = statuses.get(row["settlement_status"], 0) + 1
    return {
        "tenant_id": client.tenant_id,
        "total": len(rows),
        "count": len(recent),
        "status_counts": dict(sorted(statuses.items())),
        "totals_by_currency": {code: totals[code] for code in sorted(totals)},
        "settlements": [
            {
                "settlement_id": row["settlement_id"],
                "contract_id": row["contract_id"],
                "run_id": row["run_id"],
                "currency": row["currency"],
                "gross_amount_minor": row["gross_amount_minor"],
                "settlement_status": row["settlement_status"],
                "acceptance_status": row["acceptance_status"],
            }
            for row in recent
        ],
    }

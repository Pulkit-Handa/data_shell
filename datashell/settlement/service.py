"""Settlement service: orchestrates verification, calculation, persistence.

The single entry point used by the CLI and the HTTP API. Guarantees:

* Certificate verification happens before any calculation; an invalid
  certificate raises and never produces a statement.
* The exact immutable contract version is resolved before calculation.
* Idempotent replays: the same ``(tenant, idempotency_key)`` returns the
  original stored statement without creating a second one.
* Persistence failure fails closed — nothing is reported as calculated.
* Disputes transition the statement to DISPUTED (not silently payable).
* Corrections supersede: the new statement references the old one; history is
  preserved.
"""

from __future__ import annotations

import uuid
from typing import Any

from datashell.core.ledger import BoundaryLedger
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.settlement.certificate import (
    build_settlement_receipt,
    certificate_digest,
    verify_settlement_receipt,
)
from datashell.settlement.engine import (
    calculate_settlement,
)
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import (
    CommercialContract,
    Dispute,
    DisputeResolution,
    DisputeStatus,
    SettlementStatus,
)
from datashell.settlement.registry import CommercialContractRegistry, CommercialContractRegistryError


class SettlementServiceError(RuntimeError):
    """Raised when a settlement operation must fail closed."""


class SettlementService:
    def __init__(self, *, ledger: SettlementLedger | None = None,
                 boundary_ledger: BoundaryLedger | None = None,
                 contract_registry: CommercialContractRegistry | None = None):
        self.ledger = ledger or SettlementLedger()
        self.boundary = boundary_ledger or BoundaryLedger()
        self.registry = contract_registry or CommercialContractRegistry(ledger=self.ledger)

    # ------------------------------------------------------------- contracts

    def register_contract(self, contract: CommercialContract, *, tenant_id: str,
                          created_by: str) -> dict[str, Any]:
        return self.registry.register(contract, tenant_id=tenant_id, created_by=created_by)

    def get_contract(self, contract_id: str, tenant_id: str,
                     version: int | None = None) -> CommercialContract | None:
        return self.registry.resolve_version(contract_id, tenant_id, version)

    # ------------------------------------------------------------- calculation

    def calculate(
        self,
        *,
        tenant_id: str,
        run_id: str,
        contract_id: str,
        contract_version: int | None = None,
        actor: str,
        idempotency_key: str | None = None,
        unique_entities_delivered: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Calculate (or replay) the settlement for a completed run.

        Returns ``(statement_dict, created)``. Replays of the same idempotency
        key return ``(original, False)`` without touching billing state.
        """
        # 1. Locate the run evidence (tenant-scoped).
        run = self.boundary.get_run(run_id, tenant_id)
        if run is None:
            raise SettlementServiceError(f"run '{run_id}' not found for tenant '{tenant_id}'")

        # 2. Resolve the exact immutable contract version.
        try:
            contract = self.registry.resolve_version(contract_id, tenant_id, contract_version)
        except CommercialContractRegistryError as exc:
            raise SettlementServiceError(f"commercial contract reference is invalid: {exc}") from exc
        if contract is None:
            raise SettlementServiceError(
                f"commercial contract '{contract_id}'"
                + (f" v{contract_version}" if contract_version else "")
                + f" not found for tenant '{tenant_id}'"
            )

        # 3. Idempotency replay — before any expensive work.
        if idempotency_key:
            existing = self.ledger.find_by_idempotency_key(tenant_id, idempotency_key)
            if existing is not None:
                return existing, False

        # 4. Verify the delivery certificate before calculating anything.
        audit_path = run.get("audit_path")
        if not audit_path:
            raise SettlementServiceError("run has no audit certificate; refusing to calculate")
        from pathlib import Path
        cert_path = Path(audit_path)
        if not cert_path.is_file():
            raise SettlementServiceError("audit certificate file is missing; refusing to calculate")
        import json
        with open(cert_path, encoding="utf-8") as handle:
            cert_data = json.load(handle)
        if cert_data.get("tenant_id", tenant_id) != tenant_id:
            raise SettlementServiceError("certificate tenant mismatch; refusing to calculate")
        cert_report = ArtifactGenerator.verify_certificate_and_artifacts(cert_data)
        cert_valid = cert_report.get("status") == "AUTHENTIC_AND_UNMODIFIED"

        # 5. Fail closed on an invalid certificate.
        if not cert_valid:
            raise SettlementServiceError(
                f"certificate verification failed ({cert_report.get('status')}); "
                "settlement calculation refused"
            )

        # 6. Calculate (pure).
        statement = calculate_settlement(
            tenant_id=tenant_id,
            contract=contract,
            ledger_run=run,
            certificate_digest=certificate_digest(cert_data),
            certificate_valid=True,
            certificate_status=cert_report["status"],
            actor=actor,
            idempotency_key=idempotency_key,
            dispute_open=self.ledger.has_open_dispute(
                f"stl_{run_id}_{contract_id}_v{contract.contract_version}", tenant_id),
            unique_entities_delivered=unique_entities_delivered,
        )

        # 7. Persist immutably (idempotent on statement ID and key). The
        #    reported `created` flag reflects whether a new row was inserted.
        try:
            created = self.ledger.save_settlement(statement)
        except Exception as exc:
            raise SettlementServiceError(f"settlement persistence failed: {exc}") from exc

        stored = self.ledger.get_settlement(statement.settlement_id, tenant_id)
        if stored is None:
            raise SettlementServiceError(
                f"settlement '{statement.settlement_id}' vanished after persistence")
        return stored, created

    # -------------------------------------------------------------- inspection

    def get_settlement(self, settlement_id: str, tenant_id: str) -> dict[str, Any] | None:
        return self.ledger.get_settlement(settlement_id, tenant_id)

    def list_settlements(self, tenant_id: str, status: str | None = None,
                         contract_id: str | None = None) -> list[dict[str, Any]]:
        return self.ledger.list_settlements(tenant_id, status=status, contract_id=contract_id)

    def explain(self, settlement_id: str, tenant_id: str) -> dict[str, Any]:
        """Full calculation explanation with supporting evidence references."""
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        lines: list[str] = []
        calc = row["calculation"]
        from datashell.settlement.money import format_minor_units
        lines.append(f"Settlement {row['settlement_id']} — calculation explanation")
        lines.append(f"  Run:                {row['run_id']}")
        lines.append(f"  Contract:           {row['contract_id']} v{row['contract_version']}")
        lines.append(f"  Contract digest:    {row['contract_digest']}")
        lines.append(f"  Certificate digest: {row['certificate_digest']}")
        lines.append(f"  Pricing model:      {calc['pricing_model']}")
        lines.append(f"  Qualified units:    {calc['qualified_units']:,}")
        if calc.get("unit_price_minor") is not None:
            lines.append(
                f"  Unit price:         {format_minor_units(calc['unit_price_minor'], row['currency'])} "
                f"{row['currency']}")
        for tier in calc.get("tiers_applied") or []:
            lines.append(
                f"  Tier: {tier['units']:,} units x {tier['unit_price_minor']} minor "
                f"= {tier['amount_minor']:,} minor"
            )
        if calc.get("proration_ratio"):
            lines.append(f"  Proration ratio:    {calc['proration_ratio']}")
        lines.append(f"  Gross amount:       {format_minor_units(calc['gross_amount_minor'], row['currency'])} "
                     f"{row['currency']} ({calc['gross_amount_minor']:,} minor)")
        if calc.get("penalty_minor"):
            lines.append(f"  Penalty:            {format_minor_units(calc['penalty_minor'], row['currency'])} "
                         f"{row['currency']} — {calc.get('penalty_reason')}")
        lines.append(f"  Rounding:           {calc['rounding_mode']} applied once, at the final stage")
        lines.append(f"  Calculation engine: v{calc['calculation_version']}")
        lines.append(f"  Acceptance:         {row['acceptance_status']}")
        lines.append(f"  Settlement status:  {row['settlement_status']}")
        if row.get("reason_codes"):
            lines.append(f"  Reason codes:       {', '.join(row['reason_codes'])}")
        if row.get("explanation"):
            lines.append(f"  Explanation:        {row['explanation']}")
        lines.append("  Evidence: boundary ledger run record + signed delivery certificate "
                     "(verification precedes any billing eligibility)")
        return {"settlement_id": settlement_id, "explanation": lines}

    # ------------------------------------------------------- receipt + verify

    def issue_receipt(self, settlement_id: str, tenant_id: str) -> dict[str, Any]:
        """Build the signed settlement receipt for a stored statement."""
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        run = self.boundary.get_run(row["run_id"], tenant_id)
        if not run or not run.get("audit_path"):
            raise SettlementServiceError("run evidence missing; cannot issue receipt")
        import json
        with open(run["audit_path"], encoding="utf-8") as handle:
            cert_data = json.load(handle)
        from datashell.settlement.models import SettlementStatement
        statement = SettlementStatement(**{**row, "calculation": row["calculation"]})
        return build_settlement_receipt(statement, cert_data)

    def verify_settlement(self, settlement_id: str, tenant_id: str) -> dict[str, Any]:
        """Verify a stored settlement: re-check receipt signature + delivery certificate."""
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        run = self.boundary.get_run(row["run_id"], tenant_id)
        if not run or not run.get("audit_path"):
            raise SettlementServiceError("run evidence missing; cannot verify")
        import json
        with open(run["audit_path"], encoding="utf-8") as handle:
            cert_data = json.load(handle)
        from datashell.settlement.models import SettlementStatement
        statement = SettlementStatement(**{**row, "calculation": row["calculation"]})
        receipt = build_settlement_receipt(statement, cert_data)
        return verify_settlement_receipt(receipt, delivery_certificate=cert_data,
                                         expected_statement=statement)

    # ---------------------------------------------------------------- disputes

    def open_dispute(self, *, settlement_id: str, tenant_id: str, disputing_party: str,
                     reason: str, evidence_references: list[str] | None = None,
                     supporting_explanation: str = "", actor: str = "") -> Dispute:
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        if row["settlement_status"] in (SettlementStatus.SUPERSEDED.value, SettlementStatus.CANCELLED.value):
            raise SettlementServiceError(
                f"settlement is {row['settlement_status']}; disputes cannot be opened on it")
        dispute = Dispute(
            dispute_id=f"dsp_{uuid.uuid4().hex[:16]}",
            settlement_id=settlement_id,
            tenant_id=tenant_id,
            disputing_party=disputing_party,
            reason=reason,
            evidence_references=evidence_references or [],
            supporting_explanation=supporting_explanation,
        )
        self.ledger.save_dispute(dispute)
        self.ledger.transition_status(
            settlement_id, tenant_id, SettlementStatus.DISPUTED, actor or disputing_party,
            f"dispute opened: {reason}",
            event_type="DISPUTE_OPENED",
            payload={"dispute_id": dispute.dispute_id, "reason": reason},
        )
        return dispute

    def resolve_dispute(self, *, dispute_id: str, tenant_id: str, resolution: DisputeResolution,
                        resolution_actor: str, resolution_reason: str,
                        adjustment_settlement_id: str | None = None) -> dict[str, Any]:
        dispute = self.ledger.get_dispute(dispute_id, tenant_id)
        if dispute is None:
            raise SettlementServiceError(f"dispute '{dispute_id}' not found for tenant '{tenant_id}'")
        if dispute["status"] in (DisputeStatus.RESOLVED.value, DisputeStatus.WITHDRAWN.value):
            raise SettlementServiceError(f"dispute is already {dispute['status']}")
        self.ledger.update_dispute_resolution(
            dispute_id, tenant_id, status=DisputeStatus.RESOLVED, resolution=resolution,
            resolution_actor=resolution_actor, resolution_reason=resolution_reason,
            adjustment_settlement_id=adjustment_settlement_id,
        )
        settlement_id = dispute["settlement_id"]
        if resolution == DisputeResolution.REJECT_SETTLEMENT:
            self.ledger.transition_status(
                settlement_id, tenant_id, SettlementStatus.CANCELLED, resolution_actor,
                f"dispute resolved: {resolution.value}",
                event_type="DISPUTE_RESOLVED",
                payload={"dispute_id": dispute_id, "resolution": resolution.value},
            )
        elif resolution in (DisputeResolution.ACCEPT_AS_IS, DisputeResolution.CORRECT_CALCULATION,
                            DisputeResolution.REPLACE_WITH_NEW_SETTLEMENT):
            self.ledger.transition_status(
                settlement_id, tenant_id, SettlementStatus.READY_FOR_BILLING, resolution_actor,
                f"dispute resolved: {resolution.value}",
                event_type="DISPUTE_RESOLVED",
                payload={"dispute_id": dispute_id, "resolution": resolution.value},
            )
        return {"dispute_id": dispute_id, "status": DisputeStatus.RESOLVED.value,
                "resolution": resolution.value,
                "settlement_id": settlement_id}

    def supersede_settlement(self, *, settlement_id: str, tenant_id: str,
                             replacement: dict[str, Any], actor: str) -> dict[str, Any]:
        """Mark a statement superseded by a correction; history is preserved."""
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        self.ledger.transition_status(
            settlement_id, tenant_id, SettlementStatus.SUPERSEDED, actor,
            f"superseded by {replacement.get('settlement_id')}",
            event_type="SUPERSEDED",
            payload={"replacement_settlement_id": replacement.get("settlement_id")},
        )
        final_state = self.ledger.get_settlement(settlement_id, tenant_id)
        if final_state is None:
            raise SettlementServiceError(
                f"settlement '{settlement_id}' vanished during transition")
        return final_state

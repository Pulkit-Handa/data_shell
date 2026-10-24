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
    verify_settlement_receipt_chain,
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
    SettlementStatement,
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

        # 7. Persist the statement and its signed receipt atomically
        #    (idempotent on statement ID and key). The receipt captures the
        #    evidence at issuance; verification later compares against this
        #    stored signature instead of re-signing the current database row.
        receipt = build_settlement_receipt(statement, cert_data, receipt_kind="ISSUANCE")
        try:
            created = self.ledger.save_settlement(statement, receipt=receipt)
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

    @staticmethod
    def _statement_from_row(row: dict[str, Any]) -> SettlementStatement:
        return SettlementStatement(**{**row, "calculation": row["calculation"]})

    def issue_receipt(self, settlement_id: str, tenant_id: str) -> dict[str, Any]:
        """Return the stored signed receipt for a settlement.

        Receipts are issued and persisted atomically with the statement (and
        with every lifecycle transition). They are never regenerated on demand:
        re-signing whatever currently sits in the database would authenticate
        post-hoc tampering.
        """
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        receipt = self.ledger.get_latest_settlement_receipt(settlement_id, tenant_id)
        if receipt is None:
            raise SettlementServiceError(
                f"settlement '{settlement_id}' has no stored receipt; refusing to re-sign the current row")
        return receipt

    def verify_settlement(self, settlement_id: str, tenant_id: str) -> dict[str, Any]:
        """Verify the stored receipt chain against the row and delivery certificate.

        The signed evidence comes from the receipt persisted at issuance (and
        each later transition) — never from a fresh signature over the current
        database row. A DB edit therefore produces
        ``SETTLEMENT_EVIDENCE_MISMATCH`` rather than ``AUTHENTIC_AND_UNMODIFIED``.
        """
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        run = self.boundary.get_run(row["run_id"], tenant_id)
        if not run or not run.get("audit_path"):
            raise SettlementServiceError("run evidence missing; cannot verify")
        import json
        with open(run["audit_path"], encoding="utf-8") as handle:
            cert_data = json.load(handle)
        statement = self._statement_from_row(row)
        receipts = self.ledger.get_settlement_receipts(settlement_id, tenant_id)
        return verify_settlement_receipt_chain(
            receipts,
            delivery_certificate=cert_data,
            expected_statement=statement,
            expected_row=row,
        )

    # ------------------------------------------------------- signed transitions

    def _build_transition_receipt(self, row: dict[str, Any], *,
                                  to_status: SettlementStatus, event_type: str,
                                  actor: str, reason: str,
                                  payload: dict[str, Any] | None) -> dict[str, Any]:
        """Extend the signed evidence to a new lifecycle state.

        The new receipt is built from the *previously signed evidence*, not
        from the current DB row. First the stored receipt is verified against
        the row; then only the status field changes. This prevents a tampered
        row from being laundered through a legitimate transition.
        """
        settlement_id = row["settlement_id"]
        tenant_id = row["tenant_id"]
        latest = self.ledger.get_latest_settlement_receipt(settlement_id, tenant_id)
        if latest is None:
            raise SettlementServiceError(
                f"settlement '{settlement_id}' has no stored receipt; "
                "refusing to sign an unsigned state transition")
        current = self._statement_from_row(row)
        report = verify_settlement_receipt(
            latest, expected_statement=current, expected_row=row)
        if report.get("signature_valid") is not True or \
                report.get("status") != "AUTHENTIC_AND_UNMODIFIED":
            raise SettlementServiceError(
                f"stored settlement receipt does not match the current row "
                f"({report.get('status')}); refusing to sign a state transition")
        evidence = latest.get("settlement_evidence")
        if not isinstance(evidence, dict):
            raise SettlementServiceError(
                "stored settlement receipt has no settlement evidence; refusing to transition")
        signed_statement = SettlementStatement(**evidence)
        if signed_statement.settlement_status != SettlementStatus(row["settlement_status"]):
            raise SettlementServiceError(
                "signed settlement status does not match the current row; "
                "refusing to sign a state transition")
        updated = signed_statement.model_copy(update={"settlement_status": to_status})
        return build_settlement_receipt(
            updated,
            delivery_certificate_digest=latest["delivery_certificate_digest"],
            previous_receipt=latest,
            receipt_kind="TRANSITION",
            transition={
                "event_type": event_type,
                "from_status": row["settlement_status"],
                "to_status": to_status.value,
                "actor": actor,
                "reason": reason,
                "payload": payload or {},
            },
        )

    def transition_status(self, *, settlement_id: str, tenant_id: str,
                          to_status: SettlementStatus, actor: str, reason: str,
                          event_type: str = "STATUS_TRANSITION",
                          payload: dict[str, Any] | None = None) -> bool:
        """Apply a lifecycle transition and persist its signed receipt."""
        row = self.ledger.get_settlement(settlement_id, tenant_id)
        if row is None:
            raise SettlementServiceError(
                f"settlement '{settlement_id}' not found for tenant '{tenant_id}'")
        target = to_status if isinstance(to_status, SettlementStatus) else SettlementStatus(to_status)
        receipt = self._build_transition_receipt(
            row, to_status=target, event_type=event_type, actor=actor,
            reason=reason, payload=payload)
        return self.ledger.transition_status(
            settlement_id, tenant_id, target, actor, reason,
            event_type=event_type, payload=payload, receipt=receipt)

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
        self.transition_status(
            settlement_id=settlement_id, tenant_id=tenant_id,
            to_status=SettlementStatus.DISPUTED, actor=actor or disputing_party,
            reason=f"dispute opened: {reason}",
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
            self.transition_status(
                settlement_id=settlement_id, tenant_id=tenant_id,
                to_status=SettlementStatus.CANCELLED, actor=resolution_actor,
                reason=f"dispute resolved: {resolution.value}",
                event_type="DISPUTE_RESOLVED",
                payload={"dispute_id": dispute_id, "resolution": resolution.value},
            )
        elif resolution in (DisputeResolution.ACCEPT_AS_IS, DisputeResolution.CORRECT_CALCULATION,
                            DisputeResolution.REPLACE_WITH_NEW_SETTLEMENT):
            self.transition_status(
                settlement_id=settlement_id, tenant_id=tenant_id,
                to_status=SettlementStatus.READY_FOR_BILLING, actor=resolution_actor,
                reason=f"dispute resolved: {resolution.value}",
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
        self.transition_status(
            settlement_id=settlement_id, tenant_id=tenant_id,
            to_status=SettlementStatus.SUPERSEDED, actor=actor,
            reason=f"superseded by {replacement.get('settlement_id')}",
            event_type="SUPERSEDED",
            payload={"replacement_settlement_id": replacement.get("settlement_id")},
        )
        final_state = self.ledger.get_settlement(settlement_id, tenant_id)
        if final_state is None:
            raise SettlementServiceError(
                f"settlement '{settlement_id}' vanished during transition")
        return final_state

"""Settlement engine: pure, deterministic commercial calculation.

The engine consumes (a) verified run evidence — the boundary ledger record plus
an independently verified certificate — and (b) a validated immutable
commercial contract version, and produces a settlement statement.

Determinism guarantees:

* No network calls, no clock reads (timestamps are recorded at issuance, never
  used in calculation), no mutable global state.
* The same accepted inputs and calculation version always produce the same
  output, so a settlement can be reproduced from its stored inputs.
* An invalid certificate or failed reconciliation can never authorize a
  payable result: the engine fails closed with ``REJECTED`` + reason codes.

Qualified units are derived from run evidence through the contract's
qualification policy. ``delivered_rows`` is the default billable unit, but a
contract may bill completed batches or unique entities instead; held, refused,
duplicated, or unpublished rows are never billable unless an explicit
contractual rule says otherwise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from datashell.core.crypto import CryptoEngine
from datashell.settlement.acceptance import (
    AcceptanceVerdict,
    RunEvidence,
    evaluate_acceptance,
)
from datashell.settlement.models import (
    AcceptanceStatus,
    CommercialContract,
    PenaltyModel,
    PricingModel,
    QualificationSpec,
    QualificationUnit,
    ReasonCode,
    SettlementCalculation,
    SettlementStatement,
    SettlementStatus,
    settlement_id_for,
)
from datashell.settlement.pricing import calculate as calculate_price
from datashell.settlement.pricing import PricingResult


CALCULATION_VERSION = "1"


class SettlementEngineError(RuntimeError):
    """Raised when a settlement cannot be calculated safely (fail closed)."""


def derive_qualified_units(spec: QualificationSpec, evidence: RunEvidence) -> int:
    """Derive the billable quantity from run evidence per the qualification policy."""
    unit = spec.billable_unit
    if unit == QualificationUnit.DELIVERED_ROWS:
        return evidence.rows_delivered
    if unit == QualificationUnit.COMPLETED_BATCH:
        # One billable unit per accepted (delivered) batch.
        return 1 if evidence.rows_delivered > 0 else 0
    if unit == QualificationUnit.SOURCE_ROWS_READ:
        return evidence.rows_read if spec.include_refused_in_count else evidence.rows_delivered
    if unit == QualificationUnit.UNIQUE_ENTITIES:
        # Distinct-entity counting requires the delivered dataset; the run
        # evidence carries the precomputed distinct count in
        # ``unique_entities_delivered`` when the contract declares it.
        return int(getattr(evidence, "unique_entities_delivered", 0))
    raise SettlementEngineError(f"unsupported qualification unit: {unit}")


def build_run_evidence(ledger_run: Dict[str, Any], certificate_valid: bool,
                       certificate_status: str = "AUTHENTIC_AND_UNMODIFIED") -> RunEvidence:
    """Assemble compact run evidence from the durable ledger record.

    Only compact summaries and digests are consumed — never the source
    dataset — so settlement memory use is independent of feed size.
    """
    batch_failures: List[str] = []
    for batch in (ledger_run.get("batch_evaluations") or []):
        if isinstance(batch, dict) and not batch.get("passed", True):
            batch_failures.append(str(batch.get("rule_id", "batch_rule")))
    drift_failures = [
        str(d.get("rule_id", "drift"))
        for d in (ledger_run.get("drift_evaluations") or [])
        if isinstance(d, dict) and not d.get("passed", True)
    ]
    batch_failures.extend(drift_failures)
    return RunEvidence(
        run_id=ledger_run["run_id"],
        status=ledger_run.get("status") or "LOADED",
        rows_read=int(ledger_run.get("rows_read") or 0),
        rows_delivered=int(ledger_run.get("rows_delivered") or 0),
        rows_refused=int(ledger_run.get("rows_refused") or 0),
        rows_held=int(ledger_run.get("rows_held") or 0),
        unaccounted_for=int(ledger_run.get("unaccounted_for") or 0),
        footing_matched=bool(ledger_run.get("footing_matched", True)),
        is_run_rejected=bool(ledger_run.get("is_run_rejected", False)),
        batch_rules_passed=len(batch_failures) == 0,
        batch_failures=batch_failures,
        required_artifacts_present=bool(ledger_run.get("audit_path")),
        certificate_valid=certificate_valid,
        certificate_status=certificate_status,
        drift_passed=len(drift_failures) == 0,
    )


def _apply_penalty(contract: CommercialContract, verdict: AcceptanceVerdict,
                   gross_minor: int) -> Tuple[int, Optional[str]]:
    """Apply an explicitly contracted penalty; never invent one."""
    spec = contract.penalty
    if spec.model == PenaltyModel.NONE:
        return 0, None
    applies = False
    if verdict.status == AcceptanceStatus.REJECTED and "REJECTED" in spec.applies_to:
        applies = True
    if verdict.status in (AcceptanceStatus.PARTIALLY_ACCEPTED, AcceptanceStatus.PENDING_REVIEW) \
            and "SHORT_DELIVERY" in spec.applies_to:
        applies = True
    if not applies:
        return 0, None
    if spec.model == PenaltyModel.FLAT_FEE:
        return spec.flat_penalty_minor, f"contracted flat penalty ({spec.flat_penalty_minor} minor)"
    from fractions import Fraction
    from datashell.settlement.money import round_half_up
    amount = round_half_up(Fraction(gross_minor) * Fraction(spec.penalty_percent_bps, 10000))
    return amount, f"contracted penalty {spec.penalty_percent_bps} bps of gross"


def _settlement_status(verdict: AcceptanceVerdict, contract: CommercialContract) -> SettlementStatus:
    # A payable, cleanly accepted delivery is ready for billing. Dispute
    # blocking is enforced at billing time (the billing service re-checks for
    # open disputes), not by withholding the READY_FOR_BILLING state.
    if verdict.status == AcceptanceStatus.REJECTED:
        return SettlementStatus.PENDING_REVIEW
    if verdict.status == AcceptanceStatus.DISPUTED:
        return SettlementStatus.DISPUTED
    if verdict.status == AcceptanceStatus.PENDING_REVIEW:
        return SettlementStatus.PENDING_REVIEW
    return SettlementStatus.READY_FOR_BILLING


def calculate_settlement(
    *,
    tenant_id: str,
    contract: CommercialContract,
    ledger_run: Dict[str, Any],
    certificate_digest: str,
    certificate_valid: bool,
    certificate_status: str = "AUTHENTIC_AND_UNMODIFIED",
    actor: str,
    idempotency_key: Optional[str] = None,
    dispute_open: bool = False,
    unique_entities_delivered: Optional[int] = None,
) -> SettlementStatement:
    """Calculate the settlement for one run against one immutable contract version.

    Pure function of its inputs: no I/O, no clock, no randomness. Callers
    persist the result via :class:`datashell.settlement.ledger.SettlementLedger`.
    """
    if not certificate_valid:
        # Fail closed: no settlement is issued against an invalid certificate.
        raise SettlementEngineError(
            f"cannot calculate settlement: certificate verification failed ({certificate_status})"
        )

    evidence = build_run_evidence(ledger_run, certificate_valid, certificate_status)
    if unique_entities_delivered is not None:
        evidence.unique_entities_delivered = int(unique_entities_delivered)

    verdict = evaluate_acceptance(contract, evidence, dispute_open=dispute_open)
    qualified_units = derive_qualified_units(contract.qualification, evidence)
    expected = contract.acceptance.expected_units
    short_delivery = expected is not None and qualified_units < expected

    # Pricing. A rejected/held batch is never payable: the calculator returns
    # zero and the statement carries the non-payable status + reason codes.
    hold = not verdict.billing_eligible
    pricing: PricingResult = calculate_price(
        contract.pricing, qualified_units, expected_units=expected,
        short_delivery=short_delivery, hold=hold,
    )
    penalty_minor, penalty_reason = _apply_penalty(contract, verdict, pricing.gross_amount_minor)
    gross = pricing.gross_amount_minor + penalty_minor

    status = _settlement_status(verdict, contract)
    reason_codes = [r for r in verdict.reason_codes if r != ReasonCode.NONE]
    if contract.pricing.model == PricingModel.HOLD_OR_DISPUTE:
        # The contract defers the payable amount to manual handling: an
        # automatically calculated statement is never billing-eligible.
        status = SettlementStatus.PENDING_REVIEW
        if ReasonCode.MANUAL_REVIEW_REQUIRED not in reason_codes:
            reason_codes.append(ReasonCode.MANUAL_REVIEW_REQUIRED)
    if penalty_minor:
        reason_codes.append(ReasonCode.PENALTY_APPLIED)

    calculation = SettlementCalculation(
        calculation_version=CALCULATION_VERSION,
        pricing_model=contract.pricing.model,
        qualified_units=qualified_units,
        unit_price_minor=contract.pricing.unit_price_minor,
        tiers_applied=pricing.tiers_applied,
        proration_ratio=pricing.proration_ratio,
        gross_amount_minor=gross,
        penalty_minor=penalty_minor,
        penalty_reason=penalty_reason,
        rounding_mode="ROUND_HALF_UP",
        currency_precision=2,
    )

    explanation_parts = [verdict.explanation]
    explanation_parts.extend(pricing.notes)
    if penalty_reason:
        explanation_parts.append(penalty_reason)

    version = contract.contract_version
    return SettlementStatement(
        settlement_id=settlement_id_for(ledger_run["run_id"], contract.contract_id, version),
        tenant_id=tenant_id,
        contract_id=contract.contract_id,
        contract_version=version,
        contract_digest=contract.contract_digest,
        run_id=ledger_run["run_id"],
        certificate_digest=certificate_digest,
        calculation=calculation,
        currency=contract.currency,
        acceptance_status=verdict.status,
        settlement_status=status,
        reason_codes=reason_codes,
        explanation="; ".join(p for p in explanation_parts if p),
        actor=actor,
        idempotency_key=idempotency_key,
    )


def new_settlement_id() -> str:
    """Random ID for caller-created settlements (corrections/adjustments)."""
    return f"stl_{uuid.uuid4().hex[:16]}"


def canonical_settlement_payload(statement: SettlementStatement) -> Dict[str, Any]:
    """Canonical signed payload for the settlement receipt certificate."""
    return statement.model_dump(mode="json")


def settlement_receipt_digest(statement: SettlementStatement) -> str:
    """Digest binding the statement into a settlement receipt certificate."""
    return CryptoEngine.canonical_json_hash(canonical_settlement_payload(statement))

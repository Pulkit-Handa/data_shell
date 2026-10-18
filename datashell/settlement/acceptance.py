"""Deterministic contractual acceptance evaluation.

A technically successful pipeline run does **not** imply commercial
acceptance, and a batch can be commercially disputed after processing. This
module turns run evidence (pipeline status, reconciliation footing, batch-rule
outcomes, certificate verification) plus the contract's acceptance spec into a
verdict:

* ``ACCEPTED`` — every contractual condition is met.
* ``PARTIALLY_ACCEPTED`` — delivered, footed, certified, but quality or
  quantity thresholds degrade the outcome (payable, with reason codes).
* ``REJECTED`` — the run itself was rejected or failed; never payable.
* ``DISPUTED`` — an open dispute marks the settlement disputed.
* ``PENDING_REVIEW`` — human review required before any payable state.

The evaluator never converts an integrity failure into a payable result: a
failed reconciliation or invalid certificate yields ``REJECTED`` with a
machine-readable reason code and no billing eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from datashell.settlement.models import (
    AcceptancePolicy,
    AcceptanceSpec,
    AcceptanceStatus,
    CommercialContract,
    ReasonCode,
)


@dataclass
class RunEvidence:
    """Compact evidence about a completed delivery run.

    Derived from the ledger record and the verified certificate — never by
    loading the source dataset.
    """

    run_id: str
    status: str                      # TerminalStatus value: LOADED / REJECTED / FAILED
    rows_read: int = 0
    rows_delivered: int = 0
    rows_refused: int = 0
    rows_held: int = 0
    unaccounted_for: int = 0
    footing_matched: bool = True
    is_run_rejected: bool = False
    batch_rules_passed: bool = True
    batch_failures: List[str] = field(default_factory=list)
    required_artifacts_present: bool = True
    certificate_valid: bool = True
    certificate_status: str = "AUTHENTIC_AND_UNMODIFIED"
    drift_passed: bool = True
    # Precomputed distinct-entity count over delivered rows (when the
    # contract bills UNIQUE_ENTITIES). Derived during the run, stored as a
    # compact summary — the settlement never re-scans the dataset.
    unique_entities_delivered: int = 0


@dataclass
class AcceptanceVerdict:
    status: AcceptanceStatus
    reason_codes: List[ReasonCode] = field(default_factory=list)
    explanation: str = ""
    billing_eligible: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "acceptance_status": self.status.value,
            "reason_codes": [r.value for r in self.reason_codes],
            "explanation": self.explanation,
            "billing_eligible": self.billing_eligible,
        }


def _tolerance_exceeded(evidence: RunEvidence, acceptance: AcceptanceSpec) -> bool:
    """Whether refused+held rows exceed the contracted failure tolerance."""
    if evidence.rows_read <= 0:
        return False
    tolerance = acceptance.allowed_failure_tolerance_bps
    failure_ratio_num = (evidence.rows_refused + evidence.rows_held) * 10000
    return failure_ratio_num > tolerance * evidence.rows_read


def evaluate_acceptance(
    contract: CommercialContract,
    evidence: RunEvidence,
    *,
    dispute_open: bool = False,
) -> AcceptanceVerdict:
    """Evaluate whether the delivery satisfied the agreed acceptance criteria."""
    spec = contract.acceptance
    codes: List[ReasonCode] = []

    # --- Integrity gates: any failure here is never payable ---------------
    if not evidence.footing_matched or evidence.unaccounted_for != 0:
        codes.append(ReasonCode.RECONCILIATION_MISMATCH)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            "reconciliation did not foot; delivery arithmetic is inconsistent",
            billing_eligible=False,
        )
    if not evidence.certificate_valid:
        codes.append(ReasonCode.CERTIFICATE_VERIFICATION_FAILED)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            f"certificate verification failed ({evidence.certificate_status})",
            billing_eligible=False,
        )
    if evidence.is_run_rejected or evidence.status == "REJECTED":
        codes.append(ReasonCode.BATCH_REJECTED)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            "run was rejected by the contract policy gate",
            billing_eligible=False,
        )
    if evidence.status == "FAILED":
        codes.append(ReasonCode.CONTRACT_ACCEPTANCE_FAILED)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            "engine failure; no verifiable delivery exists",
            billing_eligible=False,
        )
    if not evidence.required_artifacts_present:
        codes.append(ReasonCode.REQUIRED_ARTIFACT_MISSING)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            "required delivery artifacts are missing",
            billing_eligible=False,
        )
    if not evidence.batch_rules_passed:
        codes.append(ReasonCode.CONTRACT_ACCEPTANCE_FAILED)
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            f"required validation rules failed: {', '.join(evidence.batch_failures) or 'batch rule failure'}",
            billing_eligible=False,
        )
    if not evidence.drift_passed:
        # Drift failure with DROP/WARN behavior does not reject the run itself,
        # but the delivery is not contractually clean.
        codes.append(ReasonCode.MANUAL_REVIEW_REQUIRED)

    # --- Quantity gates ----------------------------------------------------
    qualified = evidence.rows_delivered
    shortfall = spec.expected_units is not None and qualified < spec.expected_units
    minimum_breach = spec.minimum_accepted_units > 0 and qualified < spec.minimum_accepted_units
    maximum_breach = spec.maximum_units is not None and qualified > spec.maximum_units

    if minimum_breach:
        codes.append(ReasonCode.MINIMUM_QUANTITY_NOT_MET)
    if maximum_breach:
        codes.append(ReasonCode.MAXIMUM_QUANTITY_EXCEEDED)

    if dispute_open:
        codes.append(ReasonCode.DISPUTE_OPEN)
        return AcceptanceVerdict(
            AcceptanceStatus.DISPUTED, codes,
            "an open dispute marks this delivery disputed",
            billing_eligible=False,
        )

    # --- Policy resolution -------------------------------------------------
    if minimum_breach or maximum_breach:
        if spec.policy in (AcceptancePolicy.MINIMUM_REQUIRED, AcceptancePolicy.STRICT_QUANTITY):
            return AcceptanceVerdict(
                AcceptanceStatus.REJECTED, codes,
                f"quantity {qualified} outside contracted bounds "
                f"[min={spec.minimum_accepted_units}, max={spec.maximum_units}] under "
                f"{spec.policy.value} policy",
                billing_eligible=False,
            )
        if spec.policy == AcceptancePolicy.REVIEW_ON_SHORTFALL:
            return AcceptanceVerdict(
                AcceptanceStatus.PENDING_REVIEW, codes,
                "quantity breach requires manual review before any payable state",
                billing_eligible=False,
            )
        # PARTIAL_WITH_MINIMUM: a hard minimum breach still rejects.
        return AcceptanceVerdict(
            AcceptanceStatus.REJECTED, codes,
            f"qualified quantity {qualified} below contracted minimum "
            f"{spec.minimum_accepted_units}",
            billing_eligible=False,
        )

    if _tolerance_exceeded(evidence, spec):
        codes.append(ReasonCode.MANUAL_REVIEW_REQUIRED)

    if shortfall:
        codes.append(ReasonCode.MANUAL_REVIEW_REQUIRED)
        if spec.policy == AcceptancePolicy.STRICT_QUANTITY:
            return AcceptanceVerdict(
                AcceptanceStatus.PENDING_REVIEW, codes,
                f"delivered {qualified} != expected {spec.expected_units} under STRICT_QUANTITY",
                billing_eligible=False,
            )
        if spec.policy == AcceptancePolicy.REVIEW_ON_SHORTFALL:
            return AcceptanceVerdict(
                AcceptanceStatus.PENDING_REVIEW, codes,
                "shortfall requires manual review",
                billing_eligible=False,
            )
        # PARTIAL_WITH_MINIMUM / FULL_ACCEPTANCE: payable, flagged.
        return AcceptanceVerdict(
            AcceptanceStatus.PARTIALLY_ACCEPTED, codes,
            f"partial delivery {qualified}/{spec.expected_units} accepted under "
            f"{spec.policy.value}",
            billing_eligible=True,
        )

    if codes:
        # Payable but degraded (e.g. drift or tolerance warning only).
        return AcceptanceVerdict(
            AcceptanceStatus.PARTIALLY_ACCEPTED, codes,
            "delivery accepted with warnings",
            billing_eligible=True,
        )

    return AcceptanceVerdict(
        AcceptanceStatus.ACCEPTED, [ReasonCode.NONE],
        "delivery satisfied all contracted acceptance criteria",
        billing_eligible=True,
    )

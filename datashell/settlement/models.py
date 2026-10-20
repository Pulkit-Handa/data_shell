"""Settlement domain model: commercial contracts, statements, disputes, reason codes.

Layering:

* **Data contract** (``datashell.contract.models.DataContract``) — what a valid
  batch must contain and how the boundary evaluates it.
* **Commercial delivery contract** (``CommercialContract`` below) — the
  commercial obligations attached to a feed: pricing, qualification rules,
  acceptance policy, penalties. Versioned and immutable once referenced by a
  settlement.
* **Delivery run** — one execution of the existing pipeline (run_id,
  TerminalStatus, reconciliation ledger).
* **Settlement statement** — the deterministic commercial calculation for one
  run against one immutable contract version. Immutable after issuance;
  corrections create new linked records.
* **Billing instruction** — a proposed external commercial action. Distinct
  from a calculated liability, an invoice, and a confirmed payment.

A contract version is identified by ``contract_id`` + ``contract_version`` and
bound by ``contract_digest`` (SHA-256 over the canonical contract payload).
Settlements reference exactly one immutable contract version and exactly one
run; they are never silently rewritten.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from datashell.core.crypto import CryptoEngine
from datashell.settlement.money import (
    MAX_GROSS_AMOUNT_MINOR,
    MoneyValidationError,
    parse_unit_price_minor,
)

# --------------------------------------------------------------------------- enums


class PricingModel(StrEnum):
    PER_ACCEPTED_UNIT = "PER_ACCEPTED_UNIT"
    FIXED_BATCH_FEE = "FIXED_BATCH_FEE"
    TIERED_PRICING = "TIERED_PRICING"
    PRORATED_SHORT_DELIVERY = "PRORATED_SHORT_DELIVERY"
    HOLD_OR_DISPUTE = "HOLD_OR_DISPUTE"


class AcceptanceStatus(StrEnum):
    """Commercial acceptance of a delivery run (distinct from pipeline status)."""

    ACCEPTED = "ACCEPTED"
    PARTIALLY_ACCEPTED = "PARTIALLY_ACCEPTED"
    REJECTED = "REJECTED"
    DISPUTED = "DISPUTED"
    PENDING_REVIEW = "PENDING_REVIEW"


class SettlementStatus(StrEnum):
    """Settlement lifecycle. Deliberately separate from payment-provider states."""

    CALCULATED = "CALCULATED"
    PENDING_REVIEW = "PENDING_REVIEW"
    READY_FOR_BILLING = "READY_FOR_BILLING"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class ReasonCode(StrEnum):
    """Machine-readable explanations attached to settlement outcomes."""

    NONE = "NONE"
    CONTRACT_ACCEPTANCE_FAILED = "CONTRACT_ACCEPTANCE_FAILED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    CERTIFICATE_VERIFICATION_FAILED = "CERTIFICATE_VERIFICATION_FAILED"
    MINIMUM_QUANTITY_NOT_MET = "MINIMUM_QUANTITY_NOT_MET"
    REQUIRED_ARTIFACT_MISSING = "REQUIRED_ARTIFACT_MISSING"
    CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    BATCH_REJECTED = "BATCH_REJECTED"
    DISPUTE_OPEN = "DISPUTE_OPEN"
    PENALTY_APPLIED = "PENALTY_APPLIED"
    MAXIMUM_QUANTITY_EXCEEDED = "MAXIMUM_QUANTITY_EXCEEDED"


class DisputeStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    WITHDRAWN = "WITHDRAWN"


class DisputeResolution(StrEnum):
    """Outcome a reviewer may record when resolving a dispute."""

    ACCEPT_AS_IS = "ACCEPT_AS_IS"
    REJECT_SETTLEMENT = "REJECT_SETTLEMENT"
    CORRECT_CALCULATION = "CORRECT_CALCULATION"
    REPLACE_WITH_NEW_SETTLEMENT = "REPLACE_WITH_NEW_SETTLEMENT"


class QualificationUnit(StrEnum):
    """What the contract bills against, derived from run evidence."""

    DELIVERED_ROWS = "DELIVERED_ROWS"          # rows published to the target
    COMPLETED_BATCH = "COMPLETED_BATCH"        # 1 per accepted batch
    UNIQUE_ENTITIES = "UNIQUE_ENTITIES"        # distinct business keys delivered
    SOURCE_ROWS_READ = "SOURCE_ROWS_READ"      # every row read (use with care)


class AcceptancePolicy(StrEnum):
    """How the acceptance evaluator treats quantity shortfalls."""

    FULL_ACCEPTANCE = "FULL_ACCEPTANCE"                  # any successful delivery is payable
    MINIMUM_REQUIRED = "MINIMUM_REQUIRED"                # below minimum -> REJECTED, never payable
    PARTIAL_WITH_MINIMUM = "PARTIAL_WITH_MINIMUM"        # payable at minimum, review below it
    REVIEW_ON_SHORTFALL = "REVIEW_ON_SHORTFALL"          # shortfall -> PENDING_REVIEW, not payable
    STRICT_QUANTITY = "STRICT_QUANTITY"                  # delivered != expected -> review


class PenaltyModel(StrEnum):
    """Explicitly contracted penalty shapes. No implicit SLA penalties exist."""

    NONE = "NONE"
    FLAT_FEE = "FLAT_FEE"                    # fixed charge, defined in contract
    PERCENT_OF_GROSS = "PERCENT_OF_GROSS"    # explicit percentage of the gross amount


class BillingProviderStatus(StrEnum):
    """Internal lifecycle of an external billing instruction.

    These states are independent of any provider's own invoice states; a
    mapping from provider state to this enum is recorded per transition.
    """

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    PROVIDER_INVOICE_CREATED = "PROVIDER_INVOICE_CREATED"
    UNRESOLVED = "UNRESOLVED"          # ambiguous external outcome; reconcile before retry
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# --------------------------------------------------------------------------- contract


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class QualificationSpec(BaseModel):
    """Versioned policy turning run evidence into billable units."""

    billable_unit: QualificationUnit = QualificationUnit.DELIVERED_ROWS
    # For UNIQUE_ENTITIES: column(s) whose distinct delivered count is billed.
    unique_key_columns: list[str] = Field(default_factory=list)
    # Never count held/refused/duplicated/unpublished rows as billable unless
    # an explicit rule permits it; only COMPLETED_BATCH/SOURCE_ROWS_READ opt
    # into counting those categories, and even then never held rows.
    include_refused_in_count: bool = False

    @model_validator(mode="after")
    def validate_unique_key(self) -> QualificationSpec:
        if self.billable_unit == QualificationUnit.UNIQUE_ENTITIES and not self.unique_key_columns:
            raise ValueError("UNIQUE_ENTITIES qualification requires unique_key_columns")
        if self.unique_key_columns and len(set(self.unique_key_columns)) != len(self.unique_key_columns):
            raise ValueError("unique_key_columns must be distinct")
        return self


class TierBand(BaseModel):
    """One tiered-pricing band. Boundaries are [min_units, max_units) minor-free.

    The first band starts at 0; ``max_units`` is exclusive and None means
    unbounded. Bands must be contiguous and non-overlapping, validated at the
    contract level.
    """

    min_units: int = Field(ge=0)
    max_units: int | None = Field(default=None, ge=1)
    unit_price_minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> TierBand:
        if self.max_units is not None and self.max_units <= self.min_units:
            raise ValueError("tier max_units must be greater than min_units")
        return self


class PricingSpec(BaseModel):
    model: PricingModel = PricingModel.PER_ACCEPTED_UNIT
    currency: str = "USD"
    unit_price_minor: int | None = Field(default=None, ge=0)
    fixed_fee_minor: int | None = Field(default=None, ge=0)
    tiers: list[TierBand] = Field(default_factory=list)
    # PRORATED_SHORT_DELIVERY: agreed rational factor = numerator/denominator
    # applied to the full-delivery amount when the batch is short.
    proration_numerator: int = Field(default=1, ge=0)
    proration_denominator: int = Field(default=1, ge=1)
    # Optional cap on the payable gross amount for this contract version.
    max_payable_minor: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pricing(self) -> PricingSpec:
        if self.model in (PricingModel.PER_ACCEPTED_UNIT, PricingModel.PRORATED_SHORT_DELIVERY) and (
            self.unit_price_minor is None
        ):
            raise ValueError(f"pricing model {self.model.value} requires unit_price_minor")
        if self.model == PricingModel.FIXED_BATCH_FEE and self.fixed_fee_minor is None:
            raise ValueError("pricing model FIXED_BATCH_FEE requires fixed_fee_minor")
        if self.model == PricingModel.TIERED_PRICING:
            if not self.tiers:
                raise ValueError("pricing model TIERED_PRICING requires at least one tier band")
            ordered = sorted(self.tiers, key=lambda t: t.min_units)
            if ordered[0].min_units != 0:
                raise ValueError("tiered pricing must start at min_units=0")
            for prev, nxt in zip(ordered, ordered[1:], strict=False):
                if prev.max_units != nxt.min_units:
                    raise ValueError("tier bands must be contiguous (each max_units must equal the next min_units)")
            self.tiers = ordered
        if self.model == PricingModel.PRORATED_SHORT_DELIVERY and self.proration_denominator == 0:
            raise ValueError("proration_denominator must be positive")
        return self


class AcceptanceSpec(BaseModel):
    """Contractual acceptance criteria evaluated against run evidence."""

    policy: AcceptancePolicy = AcceptancePolicy.PARTIAL_WITH_MINIMUM
    minimum_accepted_units: int = Field(default=0, ge=0)
    expected_units: int | None = Field(default=None, ge=0)
    maximum_units: int | None = Field(default=None, ge=0)
    # Fraction of delivered rows that may be refused/held before the quality
    # threshold forces review, expressed as basis points (10000 = 100%).
    allowed_failure_tolerance_bps: int = Field(default=10000, ge=0, le=10000)
    required_validation_rules: list[str] = Field(default_factory=list)
    # Whether an open dispute makes the settlement ineligible for billing.
    block_billing_during_dispute: bool = True


class PenaltySpec(BaseModel):
    model: PenaltyModel = PenaltyModel.NONE
    flat_penalty_minor: int = Field(default=0, ge=0)
    penalty_percent_bps: int = Field(default=0, ge=0, le=10000)
    # When the penalty applies: on rejection, on short delivery, or both.
    applies_to: list[str] = Field(default_factory=lambda: ["REJECTED", "SHORT_DELIVERY"])

    @model_validator(mode="after")
    def validate_penalty(self) -> PenaltySpec:
        if self.model == PenaltyModel.FLAT_FEE and self.flat_penalty_minor <= 0:
            raise ValueError("FLAT_FEE penalty requires flat_penalty_minor > 0")
        if self.model == PenaltyModel.PERCENT_OF_GROSS and not (0 < self.penalty_percent_bps <= 10000):
            raise ValueError("PERCENT_OF_GROSS penalty requires penalty_percent_bps in (0, 10000]")
        return self


class CommercialContract(BaseModel):
    """Versioned commercial delivery contract (immutable once settled against).

    The schema is explicitly versioned (``schema_version``). ``contract_digest``
    is the SHA-256 over the canonical payload and is computed at creation;
    settlements bind to it exactly.
    """

    schema_version: int = Field(default=1, ge=1)
    contract_id: str
    contract_version: int = Field(default=1, ge=1)
    buyer_id: str
    seller_id: str
    feed_id: str
    currency: str = "USD"
    pricing: PricingSpec = Field(default_factory=PricingSpec)
    qualification: QualificationSpec = Field(default_factory=QualificationSpec)
    acceptance: AcceptanceSpec = Field(default_factory=AcceptanceSpec)
    penalty: PenaltySpec = Field(default_factory=PenaltySpec)
    effective_at: str
    expires_at: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    approved_by: list[str] = Field(default_factory=list)
    description: str | None = None

    @field_validator("contract_id", "buyer_id", "seller_id", "feed_id")
    @classmethod
    def validate_safe_id(cls, v: str) -> str:
        if not _SAFE_ID.match(v):
            raise ValueError(f"identifier '{v}' must match {_SAFE_ID.pattern}")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        if not _CURRENCY.match(v):
            raise ValueError(f"currency '{v}' must be a 3-letter ISO code")
        return v

    @model_validator(mode="after")
    def validate_cross_field(self) -> CommercialContract:
        self.pricing.currency = self.currency
        maximum = self.acceptance.maximum_units
        expected = self.acceptance.expected_units
        if maximum is not None and expected is not None and maximum < expected:
            raise ValueError("maximum_units cannot be less than expected_units")
        # ISO-8601 bounds
        try:
            datetime.fromisoformat(self.effective_at.replace("Z", "+00:00"))
            if self.expires_at:
                datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"effective_at/expires_at must be ISO-8601 timestamps: {exc}") from exc
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Canonical dict used for the digest.

        ``created_at`` is registration metadata, not a commercial term: two
        registrations of the same agreement must produce the same digest, so
        the timestamp is excluded from the canonical form. The full payload
        (including ``created_at``) is still stored for audit.
        """
        payload = self.model_dump(mode="json")
        payload.pop("created_at", None)
        return payload

    def compute_digest(self) -> str:
        """SHA-256 over the canonical JSON serialization of the contract."""
        return CryptoEngine.canonical_json_hash(self.canonical_payload())

    @property
    def contract_digest(self) -> str:
        return self.compute_digest()


# --------------------------------------------------------------------------- statements


class SettlementCalculation(BaseModel):
    """The reproducible arithmetic behind one settlement statement."""

    calculation_version: str = "1"
    pricing_model: PricingModel
    qualified_units: int = Field(ge=0)
    unit_price_minor: int | None = None
    tiers_applied: list[dict[str, Any]] = Field(default_factory=list)
    proration_ratio: str | None = None   # "num/den" when short-delivery proration used
    gross_amount_minor: int = Field(ge=0)
    penalty_minor: int = Field(default=0, ge=0)
    penalty_reason: str | None = None
    rounding_mode: str = "ROUND_HALF_UP"
    currency_precision: int = 2


class SettlementStatement(BaseModel):
    """Immutable commercial calculation result for one run + one contract version."""

    settlement_id: str
    tenant_id: str
    contract_id: str
    contract_version: int
    contract_digest: str
    run_id: str
    certificate_digest: str
    calculation: SettlementCalculation
    currency: str
    acceptance_status: AcceptanceStatus
    settlement_status: SettlementStatus
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    explanation: str = ""
    actor: str
    idempotency_key: str | None = None
    superseding_settlement_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    # External billing reference: a *proposal*, never money moved.
    billing_instruction_id: str | None = None

    def explain_lines(self) -> list[str]:
        """Human-readable, deterministic explanation of the calculation."""
        c = self.calculation
        lines = [
            f"Settlement {self.settlement_id}",
            f"  Run:                {self.run_id}",
            f"  Contract:           {self.contract_id} v{self.contract_version}",
            f"  Contract digest:    {self.contract_digest}",
            f"  Certificate digest: {self.certificate_digest}",
            f"  Pricing model:      {c.pricing_model.value}",
            f"  Qualified units:      {c.qualified_units:,}",
        ]
        if c.unit_price_minor is not None:
            from datashell.settlement.money import format_minor_units
            unit = format_minor_units(c.unit_price_minor, self.currency)
            lines.append(f"  Unit price:         {unit} {self.currency}")
        for tier in c.tiers_applied:
            lines.append(
                f"  Tier: {tier['units']:,} units x {tier['unit_price_minor']} minor "
                f"= {tier['amount_minor']:,} minor"
            )
        if c.proration_ratio:
            lines.append(f"  Proration ratio:    {c.proration_ratio}")
        lines.append(f"  Gross amount:       {c.gross_amount_minor:,} minor {self.currency}")
        if c.penalty_minor:
            lines.append(f"  Penalty:            {c.penalty_minor:,} minor ({c.penalty_reason})")
        lines.append(f"  Rounding:           {c.rounding_mode} at final stage")
        lines.append(f"  Acceptance:         {self.acceptance_status.value}")
        lines.append(f"  Settlement status:  {self.settlement_status.value}")
        if self.reason_codes:
            lines.append(f"  Reason codes:       {', '.join(r.value for r in self.reason_codes)}")
        return lines


class Dispute(BaseModel):
    """A formal commercial dispute against a settlement statement."""

    dispute_id: str
    settlement_id: str
    tenant_id: str
    disputing_party: str
    reason: str
    evidence_references: list[str] = Field(default_factory=list)
    supporting_explanation: str = ""
    status: DisputeStatus = DisputeStatus.OPEN
    resolution: DisputeResolution | None = None
    resolution_actor: str | None = None
    resolution_reason: str | None = None
    resolution_timestamp: str | None = None
    adjustment_settlement_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# --------------------------------------------------------------------------- helpers


def settlement_id_for(run_id: str, contract_id: str, contract_version: int) -> str:
    """Deterministic settlement ID derivation (ids embed no random component)."""
    return f"stl_{run_id}_{contract_id}_v{contract_version}"


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable canonical JSON used for digests and evidence binding."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "AcceptancePolicy",
    "AcceptanceSpec",
    "AcceptanceStatus",
    "BillingProviderStatus",
    "CommercialContract",
    "Dispute",
    "DisputeResolution",
    "DisputeStatus",
    "MAX_GROSS_AMOUNT_MINOR",
    "MoneyValidationError",
    "PenaltyModel",
    "PenaltySpec",
    "PricingModel",
    "PricingSpec",
    "QualificationSpec",
    "QualificationUnit",
    "ReasonCode",
    "SettlementCalculation",
    "SettlementStatement",
    "SettlementStatus",
    "TierBand",
    "parse_unit_price_minor",
    "settlement_id_for",
]

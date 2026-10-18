"""Billing provider abstraction.

The settlement engine never talks to a payment provider directly. All external
commercial actions go through this interface so that:

* a settlement calculation stays a calculation (no side effects on billing),
* providers are swappable and testable with fakes,
* ambiguous external outcomes are recorded as ``UNRESOLVED`` and reconciled
  before any retry — a timeout is never treated as "not created", and a
  retry never blindly creates a second invoice,
* live money movement stays impossible through this layer: the interface
  creates *draft invoices* only. Funds flow is a separate, explicitly
  authorized integration that does not exist here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Protocol


class BillingProviderName(str, Enum):
    DRY_RUN = "dry-run"
    STRIPE = "stripe"


@dataclass(frozen=True)
class InvoicePayload:
    """A versioned, deterministic billing instruction payload."""

    payload_version: str = "1"
    instruction_kind: str = "DRAFT_INVOICE"
    tenant_id: str = ""
    settlement_id: str = ""
    contract_id: str = ""
    contract_version: int = 0
    run_id: str = ""
    certificate_digest: str = ""
    currency: str = "USD"
    amount_minor: int = 0
    description: str = ""
    memo: str = ""
    buyer_id: str = ""
    seller_id: str = ""
    idempotency_key: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BillingResult:
    """Outcome of a provider operation.

    ``status`` is an internal lifecycle state (see
    ``settlement.models.BillingProviderStatus``), not a provider invoice
    state. ``provider_invoice_id`` records the external reference exactly
    once; ``ambiguous`` marks operations whose external outcome is unknown
    (timeout, dropped response) so callers reconcile instead of retrying
    blindly.
    """

    ok: bool
    status: str
    provider: str
    provider_invoice_id: Optional[str] = None
    provider_state: Optional[str] = None
    ambiguous: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    request_meta: Dict[str, Any] = field(default_factory=dict)
    billing_instruction_id: str = field(default_factory=lambda: f"bil_{uuid.uuid4().hex[:16]}")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "provider": self.provider,
            "provider_invoice_id": self.provider_invoice_id,
            "provider_state": self.provider_state,
            "ambiguous": self.ambiguous,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "request_meta": self.request_meta,
            "billing_instruction_id": self.billing_instruction_id,
        }


class BillingProvider(Protocol):
    """Interface every billing adapter implements."""

    name: BillingProviderName

    def create_draft_invoice(self, payload: InvoicePayload,
                             *, idempotency_key: str) -> BillingResult:
        """Create a *draft* invoice. Never finalizes or collects payment."""
        ...

    def get_invoice_status(self, provider_invoice_id: str) -> BillingResult:
        """Retrieve provider-side invoice state for reconciliation."""
        ...

    def reconcile(self, provider_invoice_id: str) -> BillingResult:
        """Reconcile an ambiguous external operation before any retry."""
        ...

    def verify_webhook(self, *, body: bytes, signature_header: str,
                       secret: str, tolerance_seconds: int = 300) -> Dict[str, Any]:
        """Verify an incoming provider webhook signature.

        Returns the parsed event on success; raises on any verification
        failure (fail closed). Implementations must deduplicate on the
        event ID so replayed webhooks never double-apply.
        """
        ...

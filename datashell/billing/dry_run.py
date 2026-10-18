"""Dry-run billing provider: deterministic, offline, side-effect free.

Suitable for demonstrations, tests, and development. Generates the exact
invoice payload a real provider would receive, records the instruction in the
settlement ledger, and simulates a successful draft-invoice response with a
deterministic synthetic provider reference — without any network request and
without moving money.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

from datashell.billing.base import (
    BillingProviderName,
    BillingResult,
    InvoicePayload,
)
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import BillingProviderStatus


class DryRunBillingProvider:
    """Simulates a billing provider deterministically.

    The synthetic provider invoice ID is derived from the idempotency key, so
    replays of the same instruction produce the same reference — making
    duplicate-prevention testable without a network.
    """

    name = BillingProviderName.DRY_RUN

    def __init__(self, ledger: SettlementLedger):
        self.ledger = ledger

    def create_draft_invoice(self, payload: InvoicePayload, *,
                             idempotency_key: str) -> BillingResult:
        # Dedup: an instruction for this provider reference already exists.
        existing = self.ledger.find_billing_instruction(
            payload.tenant_id, self.name.value, self._synthetic_ref(idempotency_key))
        if existing is not None:
            return BillingResult(
                ok=True,
                status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
                provider=self.name.value,
                provider_invoice_id=existing["provider_invoice_id"],
                provider_state="draft",
                request_meta={"deduplicated": True,
                              "billing_instruction_id": existing["billing_instruction_id"]},
                billing_instruction_id=existing["billing_instruction_id"],
            )

        # Fail closed: only a READY_FOR_BILLING settlement may be billed, and
        # an open dispute always blocks automatic billing actions.
        settlement = self.ledger.get_settlement(payload.settlement_id, payload.tenant_id)
        if settlement is None:
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code="SETTLEMENT_NOT_FOUND", error_message="settlement does not exist")
        if settlement["settlement_status"] != "READY_FOR_BILLING":
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code="SETTLEMENT_NOT_BILLABLE",
                error_message=f"settlement status '{settlement['settlement_status']}' is not billable")
        if self.ledger.has_open_dispute(payload.settlement_id, payload.tenant_id):
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code="DISPUTE_OPEN",
                error_message="an open dispute blocks automatic billing actions")

        instruction_id = f"bil_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        provider_ref = self._synthetic_ref(idempotency_key)
        self.ledger.save_billing_instruction(
            billing_instruction_id=instruction_id,
            settlement_id=payload.settlement_id,
            tenant_id=payload.tenant_id,
            provider=self.name.value,
            provider_invoice_id=provider_ref,
            currency=payload.currency,
            amount_minor=payload.amount_minor,
            idempotency_key=idempotency_key,
            status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
            request_meta={"payload": payload.as_dict()},
        )
        return BillingResult(
            ok=True,
            status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
            provider=self.name.value,
            provider_invoice_id=provider_ref,
            provider_state="draft",
            request_meta={"payload": payload.as_dict(), "offline_simulation": True},
            billing_instruction_id=instruction_id,
        )

    def get_invoice_status(self, provider_invoice_id: str) -> BillingResult:
        return BillingResult(
            ok=True, status=BillingProviderStatus.RECONCILED.value, provider=self.name.value,
            provider_invoice_id=provider_invoice_id, provider_state="draft",
            request_meta={"offline_simulation": True},
        )

    def reconcile(self, provider_invoice_id: str) -> BillingResult:
        return BillingResult(
            ok=True, status=BillingProviderStatus.RECONCILED.value, provider=self.name.value,
            provider_invoice_id=provider_invoice_id, provider_state="draft",
            request_meta={"offline_simulation": True, "reconciled": True},
        )

    def verify_webhook(self, *, body: bytes, signature_header: str, secret: str,
                       tolerance_seconds: int = 300) -> Dict[str, Any]:
        # The dry-run provider never receives webhooks; fail closed.
        raise NotImplementedError("dry-run provider accepts no webhooks")

    @staticmethod
    def _synthetic_ref(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        return f"dryrun_inv_{digest}"

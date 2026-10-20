"""Optional Stripe draft-invoice adapter.

Disabled by default and opt-in only: it requires explicitly configured
credentials (``DATASHELL_BILLING_STRIPE_SECRET_KEY``), an authorized action,
and runs against Stripe test mode unless a separate production authorization
is granted. It uses the Stripe REST API directly over HTTPS (no new runtime
dependency — httpx is already a locked dependency of the gateway).

Scope limits, stated plainly:

* Creates **draft invoices** only. Finalization and collection remain under
  explicit user/administrator control.
* A Stripe invoice is **not** an escrow account. Nothing here holds, moves, or
  releases funds, and no escrow behaviour is implied or invented.
* Amounts are converted to the provider's required currency units (integer
  minor units — Stripe expects the smallest currency unit, matching our
  internal representation exactly).
* Every external operation carries an idempotency key. An ambiguous failure
  (timeout, dropped response) is recorded as ``UNRESOLVED`` — never as "not
  created" — and must be reconciled before any retry. A retry never blindly
  creates a second invoice.
* Webhook signatures are verified with :func:`verify_stripe_webhook`, and
  events are deduplicated by event ID.
* Provider request/response metadata stored internally never includes the
  secret key or full card data (Stripe never sends card data here anyway).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from datashell.billing.base import (
    BillingProviderName,
    BillingResult,
    InvoicePayload,
)
from datashell.billing.webhooks import (
    WebhookDeduplicator,
    WebhookVerificationError,
    verify_stripe_webhook,
)
from datashell.core.config import settings
from datashell.settlement.ledger import SettlementLedger
from datashell.settlement.models import BillingProviderStatus

STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeBillingProvider:
    """Stripe draft-invoice adapter. Disabled unless explicitly configured."""

    name = BillingProviderName.STRIPE

    def __init__(self, ledger: SettlementLedger, *, secret_key: str | None = None,
                 webhook_secret: str | None = None,
                 test_mode: bool = True,
                 customer_id: str | None = None,
                 timeout_seconds: float = 10.0,
                 transport: httpx.Client | None = None):
        self.ledger = ledger
        self.secret_key = secret_key or (getattr(settings, "billing_stripe_secret_key", "") or "")
        self.webhook_secret = webhook_secret or (getattr(settings, "billing_stripe_webhook_secret", "") or "")
        self.test_mode = test_mode
        self.customer_id = customer_id or (getattr(settings, "billing_stripe_customer_id", "") or "") or None
        self.timeout_seconds = timeout_seconds
        self._client = transport
        self._dedup = WebhookDeduplicator()

    # ------------------------------------------------------------- lifecycle

    def is_configured(self) -> bool:
        return bool(self.secret_key)

    def _require_configured(self) -> None:
        if not self.secret_key:
            raise RuntimeError(
                "Stripe billing is not configured: set DATASHELL_BILLING_STRIPE_SECRET_KEY "
                "and complete the explicit provider authorization"
            )

    def _client_or_create(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        self._client = httpx.Client(
            base_url=STRIPE_API_BASE,
            auth=(self.secret_key, ""),
            headers={"Stripe-Version": "2024-06-20"},
            timeout=self.timeout_seconds,
        )
        return self._client

    # ------------------------------------------------------------ operations

    def create_draft_invoice(self, payload: InvoicePayload, *,
                             idempotency_key: str) -> BillingResult:
        self._require_configured()
        if self.test_mode and not self.secret_key.startswith("sk_test_"):
            raise RuntimeError(
                "DATASHELL_BILLING_STRIPE_SECRET_KEY is not a test-mode key but the "
                "adapter is configured for test mode; refusing to run"
            )
        if not self.customer_id:
            raise RuntimeError("DATASHELL_BILLING_STRIPE_CUSTOMER_ID is required")

        # Idempotency: a retry with the same key never blindly creates a second
        # invoice item. The previously recorded instruction (if any) is the
        # source of truth: a completed creation replays, and every ambiguous or
        # failed prior attempt must be reconciled by an operator before any
        # retry — a retry is never treated as "the first attempt failed".
        existing = self.ledger.find_billing_by_idempotency(payload.tenant_id, idempotency_key)
        if existing is not None:
            prior_status = existing["status"]
            if prior_status == BillingProviderStatus.PROVIDER_INVOICE_CREATED.value:
                return BillingResult(
                    ok=True, status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
                    provider=self.name.value, provider_invoice_id=existing["provider_invoice_id"],
                    provider_state=existing.get("last_provider_state") or "draft",
                    request_meta={"deduplicated": True},
                    billing_instruction_id=existing["billing_instruction_id"],
                )
            lease_pending = prior_status == BillingProviderStatus.SUBMITTED.value
            return BillingResult(
                ok=False, status=BillingProviderStatus.UNRESOLVED.value,
                provider=self.name.value,
                provider_invoice_id=existing["provider_invoice_id"],
                ambiguous=True,
                error_code="RECONCILIATION_REQUIRED",
                error_message=(
                    "a prior attempt with this idempotency key is "
                    f"'{prior_status}'; the external outcome is not safely known — "
                    "reconcile with the provider before any retry"
                ),
                request_meta={"prior_status": prior_status, "in_flight": lease_pending},
                billing_instruction_id=existing["billing_instruction_id"],
            )

        # Fail closed on a non-billable or disputed settlement.
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

        instruction_id = f"bil_{uuid.uuid4().hex[:16]}"
        meta: dict[str, Any] = {
            "instruction_id": instruction_id,
            "payload": payload.as_dict(),
            "test_mode": self.test_mode,
        }
        self.ledger.save_billing_instruction(
            billing_instruction_id=instruction_id,
            settlement_id=payload.settlement_id,
            tenant_id=payload.tenant_id,
            provider=self.name.value,
            currency=payload.currency,
            amount_minor=payload.amount_minor,
            idempotency_key=idempotency_key,
            status=BillingProviderStatus.SUBMITTED.value,
            request_meta=meta,
        )

        client = self._client_or_create()
        try:
            response = client.post(
                "/invoiceitems",
                data={
                    "customer": self.customer_id,
                    "amount": str(payload.amount_minor),
                    "currency": payload.currency.lower(),
                    "description": payload.description or f"Data delivery {payload.run_id}",
                    "metadata[settlement_id]": payload.settlement_id,
                    "metadata[contract_id]": payload.contract_id,
                    "metadata[run_id]": payload.run_id,
                },
                headers={"Idempotency-Key": f"{idempotency_key}-item"},
            )
        except httpx.TimeoutException:
            # Ambiguous: the item may or may not exist. Record UNRESOLVED and
            # reconcile before any retry. Never blindly create a second one.
            self.ledger.update_billing_instruction(
                instruction_id, payload.tenant_id,
                status=BillingProviderStatus.UNRESOLVED.value,
                last_error="TIMEOUT: provider response ambiguous; reconcile before retry",
            )
            return BillingResult(
                ok=False, status=BillingProviderStatus.UNRESOLVED.value,
                provider=self.name.value, ambiguous=True,
                error_code="PROVIDER_TIMEOUT",
                error_message="provider request timed out; external state unknown — reconcile before retry",
                request_meta=meta, billing_instruction_id=instruction_id,
            )
        except httpx.HTTPError as exc:
            self.ledger.update_billing_instruction(
                instruction_id, payload.tenant_id,
                status=BillingProviderStatus.FAILED.value,
                last_error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code="PROVIDER_NETWORK_ERROR", error_message="provider request failed",
                request_meta=meta, billing_instruction_id=instruction_id,
            )

        if response.status_code >= 400:
            body = response.text[:500]
            self.ledger.update_billing_instruction(
                instruction_id, payload.tenant_id,
                status=BillingProviderStatus.FAILED.value,
                last_error=f"HTTP {response.status_code}: {body}",
                last_provider_state=str(response.status_code),
            )
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code=f"PROVIDER_HTTP_{response.status_code}",
                error_message="provider rejected the invoice item",
                request_meta={**meta, "http_status": response.status_code},
                billing_instruction_id=instruction_id,
            )

        try:
            item = response.json()
            item_id = item.get("id")
        except ValueError:
            item_id = None
        meta["invoice_item_id"] = item_id

        try:
            response = client.post(
                "/invoices",
                data={
                    "customer": self.customer_id,
                    "auto_advance": "false",           # draft only; never auto-finalize
                    "collection_method": "send_invoice",
                    "days_until_due": "30",
                    "metadata[settlement_id]": payload.settlement_id,
                    "metadata[contract_id]": payload.contract_id,
                    "metadata[run_id]": payload.run_id,
                },
                headers={"Idempotency-Key": f"{idempotency_key}-invoice"},
            )
        except httpx.TimeoutException:
            self.ledger.update_billing_instruction(
                instruction_id, payload.tenant_id,
                status=BillingProviderStatus.UNRESOLVED.value,
                last_error="TIMEOUT: invoice creation response ambiguous; reconcile before retry",
            )
            return BillingResult(
                ok=False, status=BillingProviderStatus.UNRESOLVED.value,
                provider=self.name.value, ambiguous=True,
                error_code="PROVIDER_TIMEOUT",
                error_message="invoice creation timed out; external state unknown — reconcile before retry",
                request_meta=meta, billing_instruction_id=instruction_id,
            )

        if response.status_code >= 400:
            self.ledger.update_billing_instruction(
                instruction_id, payload.tenant_id,
                status=BillingProviderStatus.FAILED.value,
                last_error=f"HTTP {response.status_code}: {response.text[:500]}",
                last_provider_state=str(response.status_code),
            )
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value, provider=self.name.value,
                error_code=f"PROVIDER_HTTP_{response.status_code}",
                error_message="provider rejected the invoice",
                request_meta={**meta, "http_status": response.status_code},
                billing_instruction_id=instruction_id,
            )

        invoice = response.json()
        provider_invoice_id = invoice.get("id")
        self.ledger.update_billing_instruction(
            instruction_id, payload.tenant_id,
            status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
            provider_invoice_id=provider_invoice_id,
            last_provider_state=invoice.get("status", "draft"),
        )
        meta["invoice_id"] = provider_invoice_id
        meta["provider_status"] = invoice.get("status")
        return BillingResult(
            ok=True, status=BillingProviderStatus.PROVIDER_INVOICE_CREATED.value,
            provider=self.name.value, provider_invoice_id=provider_invoice_id,
            provider_state=invoice.get("status", "draft"),
            request_meta=meta, billing_instruction_id=instruction_id,
        )

    def get_invoice_status(self, provider_invoice_id: str) -> BillingResult:
        self._require_configured()
        client = self._client_or_create()
        try:
            response = client.get(f"/invoices/{provider_invoice_id}")
        except httpx.TimeoutException:
            return BillingResult(
                ok=False, status=BillingProviderStatus.UNRESOLVED.value,
                provider=self.name.value, provider_invoice_id=provider_invoice_id,
                ambiguous=True, error_code="PROVIDER_TIMEOUT",
                error_message="status lookup timed out; state unknown")
        if response.status_code >= 400:
            return BillingResult(
                ok=False, status=BillingProviderStatus.FAILED.value,
                provider=self.name.value, provider_invoice_id=provider_invoice_id,
                error_code=f"PROVIDER_HTTP_{response.status_code}",
                error_message="status lookup failed")
        invoice = response.json()
        return BillingResult(
            ok=True, status=BillingProviderStatus.RECONCILED.value,
            provider=self.name.value, provider_invoice_id=provider_invoice_id,
            provider_state=invoice.get("status"),
            request_meta={"amount_paid_minor": invoice.get("amount_paid"),
                          "currency": invoice.get("currency")},
        )

    def reconcile(self, provider_invoice_id: str) -> BillingResult:
        """Reconcile an ambiguous operation by asking the provider.

        A database rollback can never reverse an external API call, so the
        only safe recovery from an ambiguous failure is to look the object up
        by its external reference and record the truth.
        """
        result = self.get_invoice_status(provider_invoice_id)
        if result.ok and result.provider_state:
            result.status = BillingProviderStatus.RECONCILED.value
        return result

    # -------------------------------------------------------------- webhooks

    def verify_webhook(self, *, body: bytes, signature_header: str, secret: str = "",
                       tolerance_seconds: int = 300) -> dict[str, Any]:
        if not self.webhook_secret and not secret:
            raise WebhookVerificationError("stripe webhook signing secret is not configured")
        event = verify_stripe_webhook(
            body=body, signature_header=signature_header,
            secret=secret or self.webhook_secret, tolerance_seconds=tolerance_seconds)
        event_id = event.get("id", "")
        if self._dedup.is_duplicate(event_id):
            # Replayed event: return it so the caller can treat it as a no-op.
            return {**event, "_deduplicated": True}
        self._dedup.mark_seen(event_id)
        return event

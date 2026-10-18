"""Billing provider abstraction and adapters.

A billing instruction is a *proposal* for an external commercial action. It is
never money moved: the provider abstraction creates draft invoices, records
provider references, verifies webhook signatures, and reconciles external
state — it never represents a calculated amount as a confirmed payment.
"""

from __future__ import annotations

__all__ = [
    "BillingProvider",
    "BillingResult",
    "DryRunBillingProvider",
    "InvoicePayload",
    "WebhookVerificationError",
    "verify_stripe_webhook",
]

from datashell.billing.base import BillingProvider, BillingResult, InvoicePayload  # noqa: E402
from datashell.billing.dry_run import DryRunBillingProvider  # noqa: E402
from datashell.billing.webhooks import WebhookVerificationError, verify_stripe_webhook  # noqa: E402

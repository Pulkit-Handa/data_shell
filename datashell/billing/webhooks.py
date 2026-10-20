"""Provider webhook verification and replay protection.

Implements the Stripe webhook signature scheme (HMAC-SHA256 over
``timestamp.body`` with a versioned ``v1`` secret prefix, constant-time
comparison, and a replay tolerance window). Verification fails closed on any
malformed input, and event IDs are deduplicated so a replayed webhook never
double-applies a state transition.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


class WebhookVerificationError(Exception):
    """Raised when a webhook signature, timestamp, or payload is invalid."""


class WebhookDeduplicator:
    """In-process replay protection keyed by provider event ID.

    Stateful by design: a webhook endpoint that has already applied an event
    must not apply it again. The deduplicator records seen event IDs; for
    durable deployments this state should be persisted (for example in the
    settlement ledger) — the class intentionally keeps the check atomic and
    side-effect-free so callers can persist the decision transactionally.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_duplicate(self, event_id: str) -> bool:
        return event_id in self._seen

    def mark_seen(self, event_id: str) -> None:
        self._seen.add(event_id)


def verify_stripe_webhook(*, body: bytes, signature_header: str, secret: str,
                           tolerance_seconds: int = 300,
                           now: float | None = None) -> dict[str, Any]:
    """Verify a Stripe-style webhook signature and return the parsed event.

    The header has the form ``t=<timestamp>,v1=<signature>`` (older payloads
    may carry ``v0``). Only ``v1`` HMAC-SHA256 signatures are accepted;
    anything else fails closed. The signed content is ``<timestamp>.<body>``
    with the secret as the HMAC key, compared in constant time.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise WebhookVerificationError("webhook body must be bytes")
    if not signature_header or not isinstance(signature_header, str):
        raise WebhookVerificationError("missing signature header")
    if not secret or not isinstance(secret, str):
        raise WebhookVerificationError("webhook signing secret is not configured")

    parts = signature_header.split(",")
    timestamp: str | None = None
    signatures: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            raise WebhookVerificationError("malformed signature header")
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            timestamp = value
        elif key.startswith("v"):
            signatures[key] = value
    if timestamp is None:
        raise WebhookVerificationError("signature header is missing the timestamp component")
    if "v1" not in signatures:
        raise WebhookVerificationError("signature header carries no v1 signature")

    # Replay protection: refuse events outside the tolerance window.
    event_time = float(timestamp) if timestamp.replace(".", "", 1).isdigit() else 0.0
    current = time.time() if now is None else now
    if abs(current - event_time) > tolerance_seconds:
        raise WebhookVerificationError("webhook timestamp outside the replay tolerance window")

    signed_content = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()
    provided = signatures["v1"]
    if not hmac.compare_digest(expected, provided):
        raise WebhookVerificationError("webhook signature mismatch")

    try:
        event = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise WebhookVerificationError("webhook body is not valid JSON") from exc
    if not isinstance(event, dict) or not event.get("id"):
        raise WebhookVerificationError("webhook event is missing an id")
    return event

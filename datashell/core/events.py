"""Best-effort run-event emission for runbook and SLA integrations.

The boundary ledger makes history *queryable*; this module makes it *push*. When
``DATASHELL_RUN_WEBHOOK_URL`` is configured, every terminal run is delivered to an
operator-controlled endpoint with an HMAC-SHA256 signature, so incident tooling
(PagerDuty, Jira, Slack, a SIEM) can react to rejections and footing deficits
without polling. Emission is fire-and-forget and can never fail a run.

The signature covers ``<timestamp>.<body>`` and the timestamp travels in
``X-DataShell-Timestamp``, so a receiver can reject replayed or stale deliveries.
Delivery retries a small, bounded number of times with backoff and then gives up,
because a slow sink must never delay enforcement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from datashell.core.config import settings
from datashell.core.telemetry import telemetry_logger

EVENT_SCHEMA_VERSION = "1.0"
_MAX_ATTEMPTS = 3


def _sign(body: bytes, timestamp: str) -> str:
    signed = timestamp.encode("utf-8") + b"." + body
    return hmac.new(settings.hmac_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def _post(url: str, payload: Dict[str, Any], timeout: float) -> bool:
    # Defense in depth: only ever open http(s) endpoints. Configuration already
    # validates the scheme, but enforcing it here means a bad value can never
    # cause urlopen to touch a file:/ or other local scheme.
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        telemetry_logger.warning(
            "Run event webhook URL rejected", scheme=parsed.scheme
        )
        return False
    body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = _sign(body, timestamp)
    for attempt in range(1, 4):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-DataShell-Event": "run.completed",
                "X-DataShell-Delivery": str(payload.get("event_id", "")),
                "X-DataShell-Timestamp": timestamp,
                "X-DataShell-Signature": f"sha256={_sign(body, timestamp)}",
            },
        )
        try:
            # Scheme is validated to http(s) at the top of _post.
            with urllib.request.urlopen(request, timeout=timeout):  # nosec B310
                return True
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            if attempt >= _post.attempts:
                telemetry_logger.warning(
                    "Run event webhook delivery failed",
                    error_type=type(exc).__name__,
                    attempts=attempt,
                )
                return False
            time.sleep(min(2 ** (attempt - 1), 5))
    return False


_post.attempts = 3  # type: ignore[attr-defined]


def emit_run_event(payload: dict) -> None:
    """Emit a ``run.completed`` event if a webhook URL is configured.

    Runs on a daemon thread so a slow or unavailable endpoint never blocks (or
    delays) an enforcement run. The payload is enriched with a unique event ID,
    schema version, and emission timestamp before signing.
    """
    url = settings.run_webhook_url
    if not url:
        return
    enriched = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event": "run.completed",
        "schema_version": 1,
        "emitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **payload,
    }
    try:
        thread = threading.Thread(
            target=_post,
            args=(url, enriched, settings.webhook_timeout_seconds),
            name="datashell-run-event",
            daemon=True,
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001 - thread creation must not fail a run
        telemetry_logger.warning("Run event webhook dispatch failed", error_type=type(exc).__name__)

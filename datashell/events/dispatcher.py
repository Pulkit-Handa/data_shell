"""Enterprise Webhook & Event Dispatcher with HMAC Signature Non-Repudiation."""

from __future__ import annotations
import json
import time
from typing import Dict, Any, Optional
import httpx
from urllib.parse import urlsplit

from datashell.core.crypto import CryptoEngine
from datashell.core.telemetry import telemetry_logger


class WebhookDispatcher:
    """Dispatches cryptographically signed event notifications to external webhooks."""

    @staticmethod
    def dispatch(
        url: str,
        event_type: str,
        payload: Dict[str, Any],
        secret_key: Optional[str] = None,
        timeout_seconds: float = 5.0
    ) -> bool:
        """
        Sends an HTTP POST webhook with X-DataShell-Signature and X-DataShell-Event headers.
        """
        event_payload = {
            "event": event_type,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": payload
        }
        
        # Cryptographically sign payload with HMAC-SHA256
        signature = CryptoEngine.sign_manifest(event_payload, secret_key)
        
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "DataShell-Gateway/1.0.1",
            "X-DataShell-Event": event_type,
            "X-DataShell-Signature": signature
        }

        parsed_url = urlsplit(url)
        safe_url = f"{parsed_url.scheme}://{parsed_url.hostname or 'unknown'}"
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.post(url, json=event_payload, headers=headers)
                success = resp.status_code in (200, 201, 202, 204)
                if not success:
                    telemetry_logger.warning(
                        "Webhook delivery failed",
                        url=safe_url,
                        status_code=resp.status_code,
                    )
                return success
        except Exception as e:
            telemetry_logger.error("Webhook dispatch network exception", url=safe_url, error_type=type(e).__name__)
            return False

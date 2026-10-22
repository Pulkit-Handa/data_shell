"""Durable, signed alert delivery to a human-operated endpoint.

Data Shell already exposes the conditions that matter as Prometheus metrics and
ships the rule definitions in ``deploy/prometheus-alerts.yaml``. That is only a
*signal*: without a transport, a human is never told that a publication failed
or that a delivery is waiting for ledger reconciliation.

This module owns the missing last mile. The normal application path (a terminal
run, or startup recovery) builds an :class:`Alert` from the same state that
produces the metric, writes a durable pending record, and dispatches a signed
JSON payload to the operator-configured webhook. Delivery is:

* **HMAC-authenticated** over ``<timestamp>.<body>`` so a receiver can reject
  forged or replayed requests;
* **bounded** - a per-attempt timeout, a small retry budget, exponential
  backoff, and ``Retry-After`` support;
* **classified** - only network errors, timeouts, 408, 425, 429 and 5xx are
  retried; other 4xx responses are permanent failures;
* **durable** - a pending record is written before the first attempt and is
  removed only after a successful delivery, so a crash or an outage leaves a
  recoverable alert rather than silently discarding it;
* **idempotent** - the alert ID is deterministic for a rule and subject, and a
  persisted delivery log prevents duplicate notifications when the same run or
  recovery outcome is processed again;
* **observable** - every attempt is appended to
  ``<output_dir>/alerts/alert-deliveries.jsonl`` (no secrets, no full URL), and
  Prometheus counters/gauges expose delivery outcomes.

Delivery is best-effort from the caller's perspective: an alert failure must
never fail an enforcement run. It is *not* best-effort from an evidence
perspective: a failed alert stays visible in the pending directory and can be
retried with :func:`retry_pending_alerts` or at the next startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from datashell import __version__
from datashell.core.config import settings
from datashell.core.telemetry import (
    ALERT_DELIVERIES_TOTAL,
    ALERT_DELIVERY_DURATION_SECONDS,
    ALERT_DELIVERY_PENDING,
    telemetry_logger,
)

ALERT_SCHEMA_VERSION = "1.0"
ALERT_SERVICE_NAME = "data-shell"
ALERT_EVENT_TYPE = "alert.firing"

_DISPATCH_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_RETRY_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_RETRY_THREAD: threading.Thread | None = None

# Successful HTTP statuses for a webhook sink. A 2xx means the receiver
# accepted the request; it does not prove a human read it.
_SUCCESS_STATUSES = frozenset(range(200, 300))
_TRANSIENT_STATUSES = frozenset({408, 425, 429})
_MAX_RETRY_AFTER_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 30.0


class AlertConfigurationError(RuntimeError):
    """Raised when alert delivery is requested without a usable configuration."""


@dataclass(frozen=True)
class Alert:
    """One actionable notification, correlated with the state that caused it."""

    alert_id: str
    rule: str
    severity: str
    timestamp: str
    correlation_id: str
    source: str
    summary: str
    description: str
    evidence: dict[str, Any]
    recommended_action: str
    runbook_url: str | None = None
    environment: str = "unknown"
    synthetic: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Render the transport payload. Contains no secrets and no raw data."""
        human = (
            f"[{self.severity.upper()}] {self.rule}: {self.summary} "
            f"(alert_id={self.alert_id}, correlation_id={self.correlation_id})"
        )
        return {
            "schema_version": ALERT_SCHEMA_VERSION,
            "event": ALERT_EVENT_TYPE,
            "status": "firing",
            "alert_id": self.alert_id,
            "rule": self.rule,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "source": self.source,
            "service": ALERT_SERVICE_NAME,
            "environment": self.environment,
            "summary": self.summary,
            "description": self.description,
            "evidence": self.evidence,
            "recommended_action": self.recommended_action,
            "runbook_url": self.runbook_url,
            "synthetic": self.synthetic,
            # Slack/Teams-compatible text field (a generic receiver can ignore
            # it); the structured fields above are the authoritative payload.
            "text": human,
            "content": human,
        }


@dataclass(frozen=True)
class DeliveryResult:
    """Terminal result of one alert dispatch, suitable for release evidence."""

    alert_id: str
    outcome: str
    attempts: int
    http_status: int | None
    error: str | None
    destination: str | None
    duration_seconds: float


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _alert_id(rule: str, correlation_id: str) -> str:
    """Deterministic identity for a rule/subject pair (dedup across retries)."""
    digest = hashlib.sha256(f"{rule}|{correlation_id}".encode()).hexdigest()[:24]
    return f"alert_{digest}"


def _alerts_dir() -> Path:
    return settings.output_dir / "alerts"


def _pending_dir() -> Path:
    return _alerts_dir() / "pending"


def _delivery_log_path() -> Path:
    return _alerts_dir() / "alert-deliveries.jsonl"


def _safe_destination(url: str | None) -> str | None:
    """Scheme and host only: never persist a webhook path (it may be a secret)."""
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port_number = parsed.port
    except ValueError:
        port_number = None
    port = f":{port_number}" if port_number else ""
    return f"{parsed.scheme}://{host}{port}"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.replace(temp_path, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _pending_path(alert_id: str) -> Path:
    # Alert IDs are generated by this module, but validate before using one in a
    # path so a corrupted pending file can never escape the alert directory.
    if not alert_id.startswith("alert_") or not all(c.isalnum() or c in "_-" for c in alert_id):
        raise ValueError("invalid alert ID")
    return _pending_dir() / f"{alert_id}.json"


def _write_pending(alert: Alert) -> None:
    path = _pending_path(alert.alert_id)
    payload = {
        "alert": alert.to_payload(),
        "pending_since": _utc_now(),
    }
    _write_json_atomic(path, payload)


def _clear_pending(alert_id: str) -> None:
    try:
        _pending_path(alert_id).unlink(missing_ok=True)
    except (OSError, ValueError):
        return


def _pending_alerts() -> list[Alert]:
    alerts: list[Alert] = []
    try:
        paths = sorted(_pending_dir().glob("alert_*.json"))
    except OSError:
        return alerts
    for path in paths:
        record = _read_json(path)
        if not record:
            continue
        raw = record.get("alert")
        if not isinstance(raw, dict):
            continue
        try:
            alerts.append(_alert_from_payload(raw))
        except (KeyError, TypeError, ValueError):
            telemetry_logger.warning(
                "Discarding malformed pending alert record",
                file=path.name,
            )
    return alerts


def _alert_from_payload(raw: dict[str, Any]) -> Alert:
    return Alert(
        alert_id=str(raw["alert_id"]),
        rule=str(raw["rule"]),
        severity=str(raw["severity"]),
        timestamp=str(raw["timestamp"]),
        correlation_id=str(raw["correlation_id"]),
        source=str(raw["source"]),
        summary=str(raw["summary"]),
        description=str(raw["description"]),
        evidence=dict(raw.get("evidence") or {}),
        recommended_action=str(raw["recommended_action"]),
        runbook_url=str(raw["runbook_url"]) if raw.get("runbook_url") else None,
        environment=str(raw.get("environment") or "unknown"),
        synthetic=bool(raw.get("synthetic", False)),
    )


def _append_delivery_record(record: dict[str, Any]) -> None:
    path = _delivery_log_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
        with _LOG_LOCK:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except OSError as exc:
        # Delivery evidence must never crash the run. The alert still remains
        # pending and the structured log line records the failure.
        telemetry_logger.error(
            "Could not persist alert delivery record",
            alert_id=record.get("alert_id"),
            error_type=type(exc).__name__,
        )


def _delivered_ids() -> set[str]:
    delivered: set[str] = set()
    try:
        with open(_delivery_log_path(), encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("outcome") == "delivered":
                    alert_id = record.get("alert_id")
                    if isinstance(alert_id, str):
                        delivered.add(alert_id)
    except OSError:
        pass
    return delivered


def _pending_count() -> int:
    try:
        return sum(1 for _ in _pending_dir().glob("alert_*.json"))
    except OSError:
        return 0


def _update_pending_gauge() -> None:
    ALERT_DELIVERY_PENDING.set(float(_pending_count()))


def _signature(body: bytes, timestamp: str) -> str:
    secret = (settings.alert_webhook_secret or settings.hmac_secret).encode("utf-8")
    signed = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def _retry_after_seconds(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _post_once(
    url: str,
    body: bytes,
    *,
    alert_id: str,
    correlation_id: str,
    timeout: float,
) -> tuple[int | None, str | None, float | None]:
    """POST one signed attempt. Returns (status, error, retry_after)."""
    timestamp = str(int(time.time()))
    delivery_id = f"dlv_{uuid.uuid4().hex}"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"DataShell-Gateway/{__version__}",
            "X-DataShell-Event": ALERT_EVENT_TYPE,
            "X-DataShell-Alert-ID": alert_id,
            "X-DataShell-Correlation-ID": correlation_id,
            "X-DataShell-Delivery": delivery_id,
            "X-DataShell-Timestamp": timestamp,
            "X-DataShell-Signature": f"sha256={_signature(body, timestamp)}",
        },
    )
    try:
        # Scheme is validated by EnterpriseSettings and again before dispatch.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return int(response.status), None, None
    except urllib.error.HTTPError as exc:
        return int(exc.code), f"HTTP {exc.code}", _retry_after_seconds(exc.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, type(exc).__name__, None


def _classify(status: int | None) -> str:
    if status is None:
        return "transient"
    if status in _SUCCESS_STATUSES:
        return "delivered"
    if status in _TRANSIENT_STATUSES or 500 <= status <= 599:
        return "transient"
    return "permanent"


def _dispatch(alert: Alert, *, url: str) -> DeliveryResult:
    started = time.monotonic()
    payload = alert.to_payload()
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    destination = _safe_destination(url)
    max_attempts = max(1, min(int(settings.alert_max_attempts), 10))
    timeout = max(0.1, float(settings.alert_timeout_seconds))
    backoff = max(0.0, min(float(settings.alert_backoff_seconds), _MAX_BACKOFF_SECONDS))

    last_status: int | None = None
    last_error: str | None = None
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        attempt_started = time.monotonic()
        status, error, retry_after = _post_once(
            url,
            body,
            alert_id=alert.alert_id,
            correlation_id=alert.correlation_id,
            timeout=timeout,
        )
        last_status, last_error = status, error
        elapsed = round(time.monotonic() - attempt_started, 6)
        verdict = _classify(status)
        if verdict == "delivered":
            outcome = "delivered"
        elif verdict == "transient" and attempt < max_attempts:
            outcome = "retry"
        else:
            outcome = "failed"
        _append_delivery_record(
            {
                "timestamp": _utc_now(),
                "alert_id": alert.alert_id,
                "correlation_id": alert.correlation_id,
                "rule": alert.rule,
                "severity": alert.severity,
                "synthetic": alert.synthetic,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "outcome": outcome,
                "http_status": status,
                "error": error,
                "destination": destination,
                "attempt_duration_seconds": elapsed,
            }
        )
        if verdict == "delivered":
            _clear_pending(alert.alert_id)
            ALERT_DELIVERIES_TOTAL.labels(outcome="delivered").inc()
            ALERT_DELIVERY_DURATION_SECONDS.observe(time.monotonic() - started)
            telemetry_logger.info(
                "Alert delivered",
                alert_id=alert.alert_id,
                correlation_id=alert.correlation_id,
                rule=alert.rule,
                severity=alert.severity,
                attempts=attempt,
                destination=destination,
            )
            return DeliveryResult(
                alert_id=alert.alert_id,
                outcome="delivered",
                attempts=attempt,
                http_status=status,
                error=None,
                destination=destination,
                duration_seconds=round(time.monotonic() - started, 6),
            )
        if verdict == "permanent":
            break
        if attempt < max_attempts:
            delay = backoff * (2 ** (attempt - 1))
            if retry_after is not None:
                delay = max(delay, retry_after)
            delay = min(delay, _MAX_BACKOFF_SECONDS)
            if delay > 0:
                time.sleep(delay)

    ALERT_DELIVERIES_TOTAL.labels(outcome="failed").inc()
    ALERT_DELIVERY_DURATION_SECONDS.observe(time.monotonic() - started)
    telemetry_logger.warning(
        "Alert delivery failed",
        alert_id=alert.alert_id,
        correlation_id=alert.correlation_id,
        rule=alert.rule,
        severity=alert.severity,
        attempts=attempts,
        http_status=last_status,
        error_type=last_error,
        destination=destination,
    )
    return DeliveryResult(
        alert_id=alert.alert_id,
        outcome="failed",
        attempts=attempts,
        http_status=last_status,
        error=last_error,
        destination=destination,
        duration_seconds=round(time.monotonic() - started, 6),
    )


def dispatch_alert(alert: Alert) -> DeliveryResult:
    """Deliver one alert synchronously, with dedup, retries and evidence.

    Returns a :class:`DeliveryResult` for every outcome. A failed delivery is
    not raised: it is persisted and observable. Callers that need a hard
    failure (the verification command) inspect ``result.outcome``.
    """
    url = settings.alert_webhook_url
    started = time.monotonic()
    if not url:
        # Keep the alert durable even when the transport is not configured yet.
        # An operator can fix the webhook and let startup/manual retry deliver
        # it instead of losing the qualifying event.
        try:
            _write_pending(alert)
        except (OSError, ValueError) as exc:
            telemetry_logger.error(
                "Could not persist pending alert while delivery is unconfigured",
                alert_id=alert.alert_id,
                error_type=type(exc).__name__,
            )
        _update_pending_gauge()
        _append_delivery_record(
            {
                "timestamp": _utc_now(),
                "alert_id": alert.alert_id,
                "correlation_id": alert.correlation_id,
                "rule": alert.rule,
                "severity": alert.severity,
                "synthetic": alert.synthetic,
                "attempt": 0,
                "max_attempts": 0,
                "outcome": "not_configured",
                "http_status": None,
                "error": "DATASHELL_ALERT_WEBHOOK_URL is not configured",
                "destination": None,
                "attempt_duration_seconds": 0.0,
            }
        )
        ALERT_DELIVERIES_TOTAL.labels(outcome="not_configured").inc()
        ALERT_DELIVERY_DURATION_SECONDS.observe(time.monotonic() - started)
        telemetry_logger.error(
            "Alert delivery is not configured; alert remains pending",
            alert_id=alert.alert_id,
            correlation_id=alert.correlation_id,
            rule=alert.rule,
        )
        return DeliveryResult(
            alert_id=alert.alert_id,
            outcome="not_configured",
            attempts=0,
            http_status=None,
            error="DATASHELL_ALERT_WEBHOOK_URL is not configured",
            destination=None,
            duration_seconds=round(time.monotonic() - started, 6),
        )

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        try:
            _write_pending(alert)
        except (OSError, ValueError) as exc:
            telemetry_logger.error(
                "Could not persist pending alert with an invalid webhook URL",
                alert_id=alert.alert_id,
                error_type=type(exc).__name__,
            )
        _update_pending_gauge()
        _append_delivery_record(
            {
                "timestamp": _utc_now(),
                "alert_id": alert.alert_id,
                "correlation_id": alert.correlation_id,
                "rule": alert.rule,
                "severity": alert.severity,
                "synthetic": alert.synthetic,
                "attempt": 0,
                "max_attempts": 0,
                "outcome": "failed",
                "http_status": None,
                "error": "invalid alert webhook URL",
                "destination": None,
                "attempt_duration_seconds": 0.0,
            }
        )
        ALERT_DELIVERIES_TOTAL.labels(outcome="failed").inc()
        ALERT_DELIVERY_DURATION_SECONDS.observe(time.monotonic() - started)
        telemetry_logger.error(
            "Alert webhook URL is invalid; alert remains pending",
            alert_id=alert.alert_id,
            correlation_id=alert.correlation_id,
            rule=alert.rule,
        )
        return DeliveryResult(
            alert_id=alert.alert_id,
            outcome="failed",
            attempts=0,
            http_status=None,
            error="invalid alert webhook URL",
            destination=None,
            duration_seconds=round(time.monotonic() - started, 6),
        )

    with _DISPATCH_LOCK:
        if alert.alert_id in _INFLIGHT:
            return DeliveryResult(
                alert_id=alert.alert_id,
                outcome="in_flight",
                attempts=0,
                http_status=None,
                error=None,
                destination=_safe_destination(url),
                duration_seconds=round(time.monotonic() - started, 6),
            )
        if alert.alert_id in _delivered_ids():
            _clear_pending(alert.alert_id)
            _update_pending_gauge()
            ALERT_DELIVERIES_TOTAL.labels(outcome="duplicate").inc()
            telemetry_logger.info(
                "Alert delivery suppressed as duplicate",
                alert_id=alert.alert_id,
                correlation_id=alert.correlation_id,
                rule=alert.rule,
            )
            return DeliveryResult(
                alert_id=alert.alert_id,
                outcome="duplicate",
                attempts=0,
                http_status=None,
                error=None,
                destination=_safe_destination(url),
                duration_seconds=round(time.monotonic() - started, 6),
            )
        _INFLIGHT.add(alert.alert_id)

    try:
        try:
            _write_pending(alert)
        except OSError as exc:
            telemetry_logger.error(
                "Could not persist pending alert; delivery will still be attempted",
                alert_id=alert.alert_id,
                error_type=type(exc).__name__,
            )
        result = _dispatch(alert, url=url)
        return result
    finally:
        with _DISPATCH_LOCK:
            _INFLIGHT.discard(alert.alert_id)
        _update_pending_gauge()


def _dispatch_safely(alert: Alert) -> None:
    try:
        dispatch_alert(alert)
    except Exception as exc:  # noqa: BLE001 - delivery must never crash the caller
        telemetry_logger.error(
            "Unexpected alert dispatch failure",
            alert_id=alert.alert_id,
            error_type=type(exc).__name__,
        )


def emit_alert(alert: Alert) -> str:
    """Persist and dispatch an alert without delaying the enforcement path."""
    try:
        _write_pending(alert)
        _update_pending_gauge()
    except (OSError, ValueError) as exc:
        telemetry_logger.error(
            "Could not persist pending alert",
            alert_id=alert.alert_id,
            error_type=type(exc).__name__,
        )
    try:
        thread = threading.Thread(
            target=_dispatch_safely,
            args=(alert,),
            name="datashell-alert-delivery",
            daemon=True,
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001 - thread creation must not fail a run
        telemetry_logger.error(
            "Could not start alert delivery thread",
            alert_id=alert.alert_id,
            error_type=type(exc).__name__,
        )
    return alert.alert_id


def emit_alerts(alerts: list[Alert]) -> list[str]:
    return [emit_alert(alert) for alert in alerts]


def _run_evidence(event: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "run_id",
        "tenant_id",
        "contract_name",
        "contract_version",
        "status",
        "ledger_status",
        "execution_mode",
        "duration_seconds",
        "rows_read",
        "rows_delivered",
        "rows_refused",
        "rows_held",
        "unaccounted_for",
        "footing_matched",
        "rejection_reason",
        "audit_path",
        "target_path",
    )
    return {key: event[key] for key in keys if event.get(key) is not None}


def build_run_alerts(event: dict[str, Any]) -> list[Alert]:
    """Map a terminal run event to the documented alert conditions.

    Only conditions that already exist in ``deploy/prometheus-alerts.yaml`` are
    emitted here. A policy rejection is deliberately *not* an alert: it is a
    normal business outcome, and alerting on it would page a human for a feed
    doing exactly what its contract says.
    """
    run_id = str(event.get("run_id") or "").strip()
    if not run_id:
        return []
    status = str(event.get("status") or "").upper()
    ledger_status = str(event.get("ledger_status") or "").upper()
    evidence = _run_evidence(event)
    alerts: list[Alert] = []
    if status in {"FAILED", "ERROR"}:
        alerts.append(
            Alert(
                alert_id=_alert_id("DataShellPipelineFailures", run_id),
                rule="DataShellPipelineFailures",
                severity="warning",
                timestamp=_utc_now(),
                correlation_id=run_id,
                source="datashell.pipeline",
                environment=str(settings.environment),
                summary="Data Shell pipeline run failed",
                description=(
                    f"Run {run_id} for contract {event.get('contract_name', 'unknown')} "
                    f"terminated with status {status}."
                ),
                evidence=evidence,
                recommended_action=(
                    "Inspect the run log and quarantine/audit evidence, then follow "
                    "docs/production/INCIDENT_RESPONSE.md section 1 before re-submitting the feed."
                ),
                runbook_url="docs/production/INCIDENT_RESPONSE.md#1-publication-failure-a-run-did-not-publish-or-published-partially",
            )
        )
    if ledger_status == "PENDING":
        alerts.append(
            Alert(
                alert_id=_alert_id("DataShellPendingLedgerEntries", run_id),
                rule="DataShellPendingLedgerEntries",
                severity="page",
                timestamp=_utc_now(),
                correlation_id=run_id,
                source="datashell.pipeline",
                environment=str(settings.environment),
                summary="Published run is awaiting ledger reconciliation",
                description=(
                    f"Run {run_id} published artifacts but its ledger write did not complete. "
                    "The durable pending outbox record is still on disk."
                ),
                evidence=evidence,
                recommended_action=(
                    "Do not delete the *.ledger-pending.json record. Follow the reconciliation "
                    "repair procedure in docs/production/INCIDENT_RESPONSE.md section 2."
                ),
                runbook_url="docs/production/INCIDENT_RESPONSE.md#2-unreconciled-ledger-entries-published-but-not-recorded",
            )
        )
    return alerts


def build_failed_run_alert(
    *,
    run_id: str,
    tenant_id: str,
    contract_name: str,
    contract_version: str,
    error: BaseException,
) -> Alert:
    """Alert for a run that failed before ``_record_run_outcome`` was reached."""
    error_detail = f"{type(error).__name__}: {error}"[:1000]
    return Alert(
        alert_id=_alert_id("DataShellPipelineFailures", run_id),
        rule="DataShellPipelineFailures",
        severity="warning",
        timestamp=_utc_now(),
        correlation_id=run_id,
        source="datashell.pipeline",
        environment=str(settings.environment),
        summary="Data Shell pipeline run failed",
        description=f"Run {run_id} for contract {contract_name} failed before delivery: {error_detail}",
        evidence={
            "run_id": run_id,
            "tenant_id": tenant_id,
            "contract_name": contract_name,
            "contract_version": contract_version,
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error_message": error_detail,
        },
        recommended_action=(
            "Inspect the run log and follow docs/production/INCIDENT_RESPONSE.md section 1 "
            "before re-submitting the feed."
        ),
        runbook_url="docs/production/INCIDENT_RESPONSE.md#1-publication-failure-a-run-did-not-publish-or-published-partially",
    )


def build_startup_alerts(recovery: dict[str, Any]) -> list[Alert]:
    """Alert on the two startup-recovery conditions that page an operator."""
    startup_id = f"startup_{uuid.uuid4().hex[:12]}"
    evidence: dict[str, Any] = {
        key: recovery[key]
        for key in ("journals", "journal_errors", "replayed", "skipped")
        if key in recovery
    }
    alerts: list[Alert] = []
    if int(recovery.get("journal_errors", 0) or 0) > 0:
        alerts.append(
            Alert(
                alert_id=_alert_id("DataShellPublicationRecoveryErrors", startup_id),
                rule="DataShellPublicationRecoveryErrors",
                severity="page",
                timestamp=_utc_now(),
                correlation_id=startup_id,
                source="datashell.startup",
                environment=str(settings.environment),
                summary="Publication journal recovery reported errors",
                description=(
                    "Startup recovery could not fully recover a publication journal, "
                    "so the committed generation is unknown."
                ),
                evidence=evidence,
                recommended_action=(
                    "Preserve the journal and follow docs/production/INCIDENT_RESPONSE.md section 8."
                ),
                runbook_url="docs/production/INCIDENT_RESPONSE.md#8-interrupted-startup-recovery",
            )
        )
    if int(recovery.get("skipped", 0) or 0) > 0:
        alerts.append(
            Alert(
                alert_id=_alert_id("DataShellPendingLedgerEntries", startup_id),
                rule="DataShellPendingLedgerEntries",
                severity="page",
                timestamp=_utc_now(),
                correlation_id=startup_id,
                source="datashell.startup",
                environment=str(settings.environment),
                summary="Published runs are awaiting ledger reconciliation",
                description=(
                    f"{int(recovery.get('skipped', 0) or 0)} published run(s) could not be "
                    "reconciled into the ledger at startup."
                ),
                evidence=evidence,
                recommended_action=(
                    "Do not delete the pending outbox records. Follow "
                    "docs/production/INCIDENT_RESPONSE.md section 2."
                ),
                runbook_url="docs/production/INCIDENT_RESPONSE.md#2-unreconciled-ledger-entries-published-but-not-recorded",
            )
        )
    return alerts


def emit_run_alerts(event: dict[str, Any]) -> list[str]:
    return emit_alerts(build_run_alerts(event))


def emit_startup_alerts(recovery: dict[str, Any]) -> list[str]:
    return emit_alerts(build_startup_alerts(recovery))


def build_verification_alert(correlation_id: str) -> Alert:
    """A synthetic alert for exercising the same transport a real alert uses.

    This is explicitly marked ``synthetic`` so a receiver (and a human) can tell
    it apart from a production incident. It is not a bypass of the lifecycle
    check: the verification command also drives a real failing run.
    """
    return Alert(
        alert_id=_alert_id("DataShellDeliveryVerification", correlation_id),
        rule="DataShellDeliveryVerification",
        severity="warning",
        timestamp=_utc_now(),
        correlation_id=correlation_id,
        source="datashell.verification",
        environment=str(settings.environment),
        summary="Authorized Data Shell alert-delivery verification",
        description=(
            "This is a synthetic test alert generated by an authorized operator. "
            "No production condition has occurred."
        ),
        evidence={"correlation_id": correlation_id, "synthetic": True},
        recommended_action=(
            "Confirm receipt in the on-call channel. No remediation is required."
        ),
        runbook_url="docs/production/DEPLOYMENT_VERIFICATION.md#11-monitoring-and-alert-delivery",
        synthetic=True,
    )


def check_alert_configuration() -> dict[str, Any]:
    """Validate delivery settings without revealing the webhook or its secret."""
    issues: list[str] = []
    url = settings.alert_webhook_url
    if not url:
        issues.append(
            "DATASHELL_ALERT_WEBHOOK_URL is not set; alerts can be generated but cannot reach a human."
        )
    else:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            issues.append("DATASHELL_ALERT_WEBHOOK_URL must be an absolute http(s) URL.")
        if parsed.username or parsed.password:
            issues.append("DATASHELL_ALERT_WEBHOOK_URL must not embed credentials.")
        if settings.is_production_mode and parsed.scheme != "https":
            issues.append("DATASHELL_ALERT_WEBHOOK_URL must use https in production.")
    if settings.alert_timeout_seconds <= 0:
        issues.append("DATASHELL_ALERT_TIMEOUT_SECONDS must be positive.")
    if not (1 <= settings.alert_max_attempts <= 10):
        issues.append("DATASHELL_ALERT_MAX_ATTEMPTS must be between 1 and 10.")
    if not (0 <= settings.alert_backoff_seconds <= 30):
        issues.append("DATASHELL_ALERT_BACKOFF_SECONDS must be between 0 and 30.")
    return {
        "configured": not issues,
        "destination": _safe_destination(url),
        "signing_secret": (
            "DATASHELL_ALERT_WEBHOOK_SECRET" if settings.alert_webhook_secret else "DATASHELL_HMAC_SECRET"
        ),
        "timeout_seconds": settings.alert_timeout_seconds,
        "max_attempts": settings.alert_max_attempts,
        "backoff_seconds": settings.alert_backoff_seconds,
        "pending_alerts": _pending_count(),
        "issues": issues,
    }


def retry_pending_alerts() -> dict[str, int]:
    """Retry every durable pending alert once. Safe to call at startup.

    Returns counts for the operator. A still-failing alert remains pending; it
    is never deleted merely because a retry failed.
    """
    summary = {"pending": 0, "delivered": 0, "failed": 0, "not_configured": 0, "duplicate": 0}
    if not settings.alert_webhook_url:
        summary["not_configured"] = _pending_count()
        if summary["not_configured"]:
            telemetry_logger.warning(
                "Pending alerts remain because alert delivery is not configured",
                pending=summary["not_configured"],
            )
        _update_pending_gauge()
        return summary
    with _RETRY_LOCK:
        alerts = _pending_alerts()
        summary["pending"] = len(alerts)
        for alert in alerts:
            result = dispatch_alert(alert)
            if result.outcome in summary:
                summary[result.outcome] += 1
    _update_pending_gauge()
    return summary


def retry_pending_alerts_async() -> None:
    """Retry pending alerts on startup without delaying readiness."""
    global _RETRY_THREAD
    if not settings.alert_webhook_url or _pending_count() == 0:
        _update_pending_gauge()
        return
    with _RETRY_LOCK:
        if _RETRY_THREAD is not None and _RETRY_THREAD.is_alive():
            return
        _RETRY_THREAD = threading.Thread(
            target=retry_pending_alerts,
            name="datashell-alert-retry",
            daemon=True,
        )
        _RETRY_THREAD.start()


def wait_for_delivery(
    alert_id: str,
    *,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.1,
) -> DeliveryResult | None:
    """Wait until a delivery attempt for ``alert_id`` reaches a terminal state.

    Used by the release-verification command so it does not mistake successful
    enqueueing for successful delivery.
    """
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        result = _delivery_result_from_log(alert_id)
        if result is not None and result.outcome in {"delivered", "failed", "duplicate", "not_configured"}:
            return result
        time.sleep(max(0.01, poll_seconds))
    return _delivery_result_from_log(alert_id)


def _delivery_result_from_log(alert_id: str) -> DeliveryResult | None:
    records: list[dict[str, Any]] = []
    try:
        with open(_delivery_log_path(), encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("alert_id") == alert_id:
                    records.append(record)
    except OSError:
        return None
    if not records:
        return None
    terminal_outcomes = {"delivered", "failed", "duplicate", "not_configured"}
    terminal = [record for record in records if record.get("outcome") in terminal_outcomes]
    if not terminal:
        return None
    last = terminal[-1]
    attempts = max(int(record.get("attempt", 0) or 0) for record in records)
    return DeliveryResult(
        alert_id=alert_id,
        outcome=str(last.get("outcome")),
        attempts=attempts,
        http_status=int(last["http_status"]) if last.get("http_status") is not None else None,
        error=str(last["error"]) if last.get("error") else None,
        destination=str(last["destination"]) if last.get("destination") else None,
        duration_seconds=round(
            sum(float(record.get("attempt_duration_seconds", 0.0) or 0.0) for record in records),
            6,
        ),
    )

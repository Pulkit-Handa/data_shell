"""Regression and integration tests for human alert delivery.

The release gate this covers is not "an alert rule exists": it is "a normal
application event produced a useful payload and a real transport attempt to a
configured human endpoint was made, recorded and retried when necessary".

The tests use an ephemeral loopback HTTP receiver. That exercises the actual
``urllib`` transport, signatures, HTTP status classification, retries, durable
pending records and the delivery log - without inventing a target webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")

from datashell.contract.models import (
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityBehavior,
    QualitySpec,
    SchemaColumn,
    SourceDefinition,
)
from datashell.core import alerts
from datashell.core.config import EnterpriseSettings, settings
from datashell.pipeline.runner import DataShellPipeline

TERMINAL_OUTCOMES = {"delivered", "failed", "duplicate", "not_configured"}


class _Sink:
    """Loopback HTTP receiver with programmable responses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.responses: list[tuple[int, dict[str, str]]] = []
        self.delay = 0.0
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length)
                with sink._lock:
                    sink.requests.append(
                        {
                            "path": self.path,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                            "body": body,
                        }
                    )
                    status, headers = sink.responses.pop(0) if sink.responses else (200, {})
                if sink.delay:
                    time.sleep(sink.delay)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/alerts"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sink() -> Any:
    receiver = _Sink()
    receiver.start()
    try:
        yield receiver
    finally:
        receiver.stop()


@pytest.fixture
def alert_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Isolate alert state and point storage at a temporary directory."""
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "alert_webhook_url", None)
    monkeypatch.setattr(settings, "alert_webhook_secret", "test-alert-secret")
    monkeypatch.setattr(settings, "alert_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "alert_max_attempts", 2)
    monkeypatch.setattr(settings, "alert_backoff_seconds", 0.01)
    monkeypatch.setattr(alerts, "_RETRY_THREAD", None)
    with alerts._DISPATCH_LOCK:
        alerts._INFLIGHT.clear()
    return settings


def _verify_signature(request: dict[str, Any], secret: str) -> bool:
    headers = request["headers"]
    signature = headers.get("x-datashell-signature", "")
    timestamp = headers.get("x-datashell-timestamp", "")
    if not signature.startswith("sha256=") or not timestamp:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + request["body"],
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, f"sha256={expected}")


def _delivery_records() -> list[dict[str, Any]]:
    path = alerts._delivery_log_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _wait_for_rule(rule: str, timeout: float = 10.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in reversed(_delivery_records()):
            if record.get("rule") == rule and record.get("outcome") in TERMINAL_OUTCOMES:
                return record
        time.sleep(0.05)
    return None


def _contract(tmp_path: Path) -> DataContract:
    return DataContract(
        metadata={"name": "alert-delivery-tests", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(tmp_path / "input.jsonl"),
                    format="jsonl",
                    schema=[SchemaColumn(name="id", polars_type="Utf8", nullable=False)],
                )
            ],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW, rules=[]),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(tmp_path / "output.parquet"))]),
        },
    )


# ----------------- Payload and alert conditions -----------------


def test_payload_carries_actionable_alert_fields(alert_env: Any) -> None:
    alert = alerts.build_verification_alert("verify-payload")
    payload = alert.to_payload()

    assert payload["alert_id"].startswith("alert_")
    assert payload["alert_id"] == alert.alert_id
    assert payload["rule"] == "DataShellDeliveryVerification"
    assert payload["severity"] == "warning"
    assert payload["timestamp"]
    assert payload["correlation_id"] == "verify-payload"
    assert payload["source"] == "datashell.verification"
    assert payload["evidence"]["correlation_id"] == "verify-payload"
    assert payload["recommended_action"]
    assert payload["synthetic"] is True
    # Slack/Teams-compatible text is present, but structured fields are authoritative.
    assert alert.alert_id in payload["text"]


def test_failed_run_event_maps_to_pipeline_failure_alert(alert_env: Any) -> None:
    built = alerts.build_run_alerts(
        {
            "run_id": "dm_run_failed",
            "tenant_id": "tenant-a",
            "contract_name": "payments",
            "status": "FAILED",
            "ledger_status": "COMMITTED",
            "rows_read": 10,
            "rows_delivered": 0,
            "audit_path": "/tmp/audit.json",
        }
    )
    assert [alert.rule for alert in built] == ["DataShellPipelineFailures"]
    alert = built[0]
    assert alert.correlation_id == "dm_run_failed"
    assert alert.severity == "warning"
    assert alert.evidence["run_id"] == "dm_run_failed"
    assert alert.evidence["contract_name"] == "payments"
    assert alert.evidence["audit_path"] == "/tmp/audit.json"
    assert alert.runbook_url


def test_pending_ledger_event_adds_reconciliation_alert(alert_env: Any) -> None:
    built = alerts.build_run_alerts({"run_id": "dm_run_pending", "status": "LOADED", "ledger_status": "PENDING"})
    assert [alert.rule for alert in built] == ["DataShellPendingLedgerEntries"]
    assert built[0].severity == "page"


def test_successful_and_policy_rejected_runs_do_not_page(alert_env: Any) -> None:
    assert alerts.build_run_alerts({"run_id": "dm_run_ok", "status": "LOADED", "ledger_status": "COMMITTED"}) == []
    assert alerts.build_run_alerts(
        {"run_id": "dm_run_rejected", "status": "REJECTED", "ledger_status": "COMMITTED"}
    ) == []


def test_startup_recovery_conditions_build_two_alerts(alert_env: Any) -> None:
    built = alerts.build_startup_alerts(
        {"journals": 2, "journal_errors": 1, "replayed": 0, "skipped": 3, "errors": 1}
    )
    assert {alert.rule for alert in built} == {
        "DataShellPublicationRecoveryErrors",
        "DataShellPendingLedgerEntries",
    }
    assert all(alert.severity == "page" for alert in built)


# ----------------- Transport, evidence and safety -----------------


def test_delivers_signed_payload_and_writes_safe_evidence(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = f"{sink.url}/services/very-secret-token"
    alert = alerts.build_verification_alert("deliver-ok")

    result = alerts.dispatch_alert(alert)

    assert result.outcome == "delivered"
    assert result.http_status == 200
    assert result.attempts == 1
    assert result.destination == sink.base_url
    assert len(sink.requests) == 1
    request = sink.requests[0]
    assert request["path"] == "/alerts/services/very-secret-token"
    assert request["headers"]["x-datashell-event"] == "alert.firing"
    assert request["headers"]["x-datashell-alert-id"] == alert.alert_id
    assert request["headers"]["x-datashell-correlation-id"] == "deliver-ok"
    assert _verify_signature(request, "test-alert-secret")

    payload = json.loads(request["body"])
    assert payload["alert_id"] == alert.alert_id
    assert payload["rule"] == "DataShellDeliveryVerification"
    assert payload["evidence"]["synthetic"] is True

    assert not (alerts._pending_dir() / f"{alert.alert_id}.json").exists()
    log_text = alerts._delivery_log_path().read_text(encoding="utf-8")
    assert "very-secret-token" not in log_text
    assert "test-alert-secret" not in log_text
    record = _delivery_records()[0]
    assert record["outcome"] == "delivered"
    assert record["destination"] == sink.base_url


def test_duplicate_alert_id_is_suppressed(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = sink.url
    alert = alerts.build_verification_alert("dedup")
    assert alerts.dispatch_alert(alert).outcome == "delivered"
    assert alerts.dispatch_alert(alert).outcome == "duplicate"
    assert len(sink.requests) == 1
    assert not (alerts._pending_dir() / f"{alert.alert_id}.json").exists()


def test_transient_failure_is_retried_then_delivered(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = sink.url
    alert_env.alert_max_attempts = 3
    sink.responses = [(503, {}), (500, {})]

    result = alerts.dispatch_alert(alerts.build_verification_alert("retry-success"))

    assert result.outcome == "delivered"
    assert result.attempts == 3
    assert len(sink.requests) == 3
    assert [record["outcome"] for record in _delivery_records()] == ["retry", "retry", "delivered"]


def test_rate_limit_is_retried_and_retry_after_is_honoured(
    alert_env: Any, sink: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_env.alert_webhook_url = sink.url
    alert_env.alert_max_attempts = 2
    alert_env.alert_backoff_seconds = 0.0
    sink.responses = [(429, {"Retry-After": "7"})]
    delays: list[float] = []
    monkeypatch.setattr(alerts.time, "sleep", lambda seconds: delays.append(seconds))

    result = alerts.dispatch_alert(alerts.build_verification_alert("rate-limited"))

    assert result.outcome == "delivered"
    assert result.attempts == 2
    assert delays == [7.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_client_errors_are_not_retried(alert_env: Any, sink: Any, status: int) -> None:
    alert_env.alert_webhook_url = sink.url
    alert_env.alert_max_attempts = 5
    sink.responses = [(status, {})]

    result = alerts.dispatch_alert(alerts.build_verification_alert(f"permanent-{status}"))

    assert result.outcome == "failed"
    assert result.attempts == 1
    assert result.http_status == status
    assert len(sink.requests) == 1


def test_timeout_fails_bounded_and_leaves_pending_record(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = sink.url
    alert_env.alert_timeout_seconds = 0.05
    alert_env.alert_max_attempts = 1
    sink.delay = 0.3

    alert = alerts.build_verification_alert("timeout")
    result = alerts.dispatch_alert(alert)

    assert result.outcome == "failed"
    assert result.attempts == 1
    assert result.error == "TimeoutError"
    assert (alerts._pending_dir() / f"{alert.alert_id}.json").exists()


def test_connection_failure_is_bounded_and_kept_for_recovery(
    alert_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reserve and release a port so connection attempts are refused immediately.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    alert_env.alert_webhook_url = f"http://127.0.0.1:{port}/alerts"
    alert_env.alert_max_attempts = 2
    monkeypatch.setattr(alerts.time, "sleep", lambda _seconds: None)

    alert = alerts.build_verification_alert("connection-refused")
    result = alerts.dispatch_alert(alert)

    assert result.outcome == "failed"
    assert result.attempts == 2
    assert result.http_status is None
    assert (alerts._pending_dir() / f"{alert.alert_id}.json").exists()
    assert alerts.check_alert_configuration()["pending_alerts"] == 1


def test_failed_delivery_is_retried_from_pending_on_recovery(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = sink.url
    alert_env.alert_max_attempts = 2
    sink.responses = [(503, {}), (503, {})]
    alert = alerts.build_verification_alert("recover-after-outage")

    assert alerts.dispatch_alert(alert).outcome == "failed"
    assert (alerts._pending_dir() / f"{alert.alert_id}.json").exists()

    summary = alerts.retry_pending_alerts()

    assert summary == {"pending": 1, "delivered": 1, "failed": 0, "not_configured": 0, "duplicate": 0}
    assert not (alerts._pending_dir() / f"{alert.alert_id}.json").exists()
    assert len(sink.requests) == 3


def test_missing_configuration_is_observable_and_recoverable(alert_env: Any, sink: Any) -> None:
    alert = alerts.build_verification_alert("not-configured")

    result = alerts.dispatch_alert(alert)

    assert result.outcome == "not_configured"
    assert (alerts._pending_dir() / f"{alert.alert_id}.json").exists()
    report = alerts.check_alert_configuration()
    assert report["configured"] is False
    assert any("DATASHELL_ALERT_WEBHOOK_URL" in issue for issue in report["issues"])
    assert _delivery_records()[-1]["outcome"] == "not_configured"

    alert_env.alert_webhook_url = sink.url
    summary = alerts.retry_pending_alerts()
    assert summary["delivered"] == 1
    assert not (alerts._pending_dir() / f"{alert.alert_id}.json").exists()


def test_invalid_webhook_url_is_recorded_and_kept_pending(alert_env: Any) -> None:
    alert_env.alert_webhook_url = "ftp://alerts.example.com/hook"
    alert = alerts.build_verification_alert("invalid-url")

    result = alerts.dispatch_alert(alert)

    assert result.outcome == "failed"
    assert result.attempts == 0
    assert (alerts._pending_dir() / f"{alert.alert_id}.json").exists()
    assert _delivery_records()[-1]["outcome"] == "failed"
    assert _delivery_records()[-1]["error"] == "invalid alert webhook URL"


def test_configuration_report_redacts_webhook_path_and_secret(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = f"{sink.url}/services/secret-token"
    report = alerts.check_alert_configuration()

    assert report["configured"] is True
    assert report["destination"] == sink.base_url
    serialized = json.dumps(report)
    assert "secret-token" not in serialized
    assert "test-alert-secret" not in serialized


# ----------------- Normal lifecycle integration -----------------


def test_failing_pipeline_run_emits_and_delivers_alert(alert_env: Any, sink: Any, tmp_path: Path) -> None:
    alert_env.alert_webhook_url = sink.url
    pipeline = DataShellPipeline(_contract(tmp_path), tenant_id="alert-delivery-tests")

    with pytest.raises(FileNotFoundError):
        pipeline.run_file(
            source_path=tmp_path / "missing-input.jsonl",
            target_path_override=str(tmp_path / "output.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )

    record = _wait_for_rule("DataShellPipelineFailures")
    assert record is not None, "normal failure path did not create a pipeline-failure alert"
    assert record["outcome"] == "delivered"
    assert str(record["correlation_id"]).startswith("dm_run_")
    assert len(sink.requests) == 1
    payload = json.loads(sink.requests[0]["body"])
    assert payload["rule"] == "DataShellPipelineFailures"
    assert payload["severity"] == "warning"
    assert payload["correlation_id"] == record["correlation_id"]
    assert payload["evidence"]["run_id"] == record["correlation_id"]
    assert payload["evidence"]["tenant_id"] == "alert-delivery-tests"
    assert payload["evidence"]["status"] == "FAILED"
    assert payload["recommended_action"]
    assert payload["timestamp"]


def test_startup_recovery_alerts_are_delivered(alert_env: Any, sink: Any) -> None:
    alert_env.alert_webhook_url = sink.url

    alert_ids = alerts.emit_startup_alerts({"journals": 2, "journal_errors": 1, "replayed": 0, "skipped": 1})

    assert len(alert_ids) == 2
    results = [alerts.wait_for_delivery(alert_id, timeout_seconds=10) for alert_id in alert_ids]
    assert [result.outcome for result in results if result is not None] == ["delivered", "delivered"]
    assert len(sink.requests) == 2


# ----------------- Configuration validation -----------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"alert_webhook_url": "ftp://alerts.example.com/hook"}, "absolute http"),
        ({"alert_webhook_url": "alerts.example.com/hook"}, "absolute http"),
        ({"alert_webhook_url": "https://user:pass@alerts.example.com/hook"}, "must not embed credentials"),
        ({"alert_timeout_seconds": 0}, "DATASHELL_ALERT_TIMEOUT_SECONDS"),
        ({"alert_max_attempts": 0}, "DATASHELL_ALERT_MAX_ATTEMPTS"),
        ({"alert_max_attempts": 11}, "DATASHELL_ALERT_MAX_ATTEMPTS"),
        ({"alert_backoff_seconds": -1}, "DATASHELL_ALERT_BACKOFF_SECONDS"),
        ({"alert_backoff_seconds": 31}, "DATASHELL_ALERT_BACKOFF_SECONDS"),
    ],
)
def test_configuration_rejects_unsafe_alert_settings(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        EnterpriseSettings(**kwargs)


def test_production_requires_https_alert_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets

    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    with pytest.raises(RuntimeError, match="must use https in production"):
        EnterpriseSettings(
            environment="production",
            allowed_origins=["https://console.example.com"],
            alert_webhook_url="http://alerts.example.com/hook",
        )

"""Verify that a qualifying Data Shell event reaches a human notification sink.

This is the executable release check for release gate **O7** (alert delivery).
It is deliberately not a rule-file inspection and not a direct transport smoke
test dressed up as proof:

1. it validates the alert-delivery configuration without printing the webhook
   path or signing secret;
2. it drives a **real failing run through the normal pipeline path**, waits for
   the alert the pipeline generates, and records the transport outcome;
3. it independently sends a synthetic ``DataShellDeliveryVerification`` alert
   through the same dispatcher (unless ``--lifecycle-only``);
4. it exits nonzero when configuration, alert creation, or delivery fails.

Modes
-----
``--local-sink``
    Starts an ephemeral HTTP receiver on loopback and points
    ``DATASHELL_ALERT_WEBHOOK_URL`` at it. This proves the complete lifecycle
    and the transport adapter against a real HTTP receiver, but it is **local
    integration evidence**, not evidence that a deployed target or a person
    received the alert.

configured endpoint (default)
    Uses ``DATASHELL_ALERT_WEBHOOK_URL`` from the environment. Run this on the
    target host (or inside the deployed container) with the same secrets and
    destination the service uses. A 2xx response proves the receiving endpoint
    accepted the request; recipient-side confirmation is still required before
    O7 can be called resolved.

The check uses an isolated storage root by default so a synthetic failed run
does not pollute the deployment's ledger. Pass ``--use-configured-storage`` if
you explicitly want the run written to the configured storage root.

Usage::

    # Local integration evidence (safe, self-contained)
    python scripts/verify_alert_delivery.py --local-sink --json

    # Target/authorized environment: same env as the service
    DATASHELL_ALERT_WEBHOOK_URL=https://hooks.example.com/services/... \
    DATASHELL_ALERT_WEBHOOK_SECRET=... \
        python scripts/verify_alert_delivery.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Sink:
    """Minimal HTTP receiver that records what the dispatcher actually sent."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
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
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"received":true}')

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/alert-delivery-verification"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_contract(workspace: Path) -> Any:
    from datashell.contract.models import (
        DataContract,
        DeliverySpec,
        DeliveryTarget,
        QualityBehavior,
        QualitySpec,
        SchemaColumn,
        SourceDefinition,
    )

    return DataContract(
        metadata={"name": "alert-delivery-verification", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(workspace / "verification-input.jsonl"),
                    format="jsonl",
                    schema=[SchemaColumn(name="id", polars_type="Utf8", nullable=False)],
                )
            ],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW, rules=[]),
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path=str(workspace / "verification-output.parquet"))]
            ),
        },
    )


def _trigger_failing_run(workspace: Path) -> None:
    """Drive the real pipeline into its failure path; it must raise."""
    from datashell.pipeline.runner import DataShellPipeline

    pipeline = DataShellPipeline(
        _build_contract(workspace),
        tenant_id="alert-delivery-verification",
    )
    try:
        pipeline.run_file(
            source_path=workspace / "does-not-exist.jsonl",
            target_path_override=str(workspace / "verification-output.parquet"),
            audit_output_path=str(workspace / "verification-audit.json"),
        )
    except Exception:
        # The failure is the qualifying event. The check below proves that the
        # normal failure path emitted an alert; re-raising would hide it.
        return
    raise RuntimeError("synthetic failing run unexpectedly completed")


def _read_delivery_records(alert_log: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with open(alert_log, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return records
    return records


def _wait_for_lifecycle_alert(alert_log: Path, since: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.1, timeout)
    terminal = {"delivered", "failed", "not_configured"}
    while time.monotonic() < deadline:
        for record in reversed(_read_delivery_records(alert_log)):
            if (
                record.get("rule") == "DataShellPipelineFailures"
                and record.get("outcome") in terminal
                and str(record.get("timestamp", "")) >= since
            ):
                return record
        time.sleep(0.1)
    return None


def _verify_signature(request: dict[str, Any], secret: str) -> bool:
    import hashlib
    import hmac

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


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    workspace = Path(tempfile.mkdtemp(prefix="datashell-alert-verify-"))
    sink: _Sink | None = None
    if args.local_sink:
        sink = _Sink()
        # A loopback sink is plain HTTP by definition; keep the local check in
        # test mode so production HTTPS validation does not reject it.
        os.environ["ENVIRONMENT"] = "test"
        os.environ["DATASHELL_ALERT_WEBHOOK_URL"] = sink.start()
        os.environ.setdefault("DATASHELL_ALERT_WEBHOOK_SECRET", "local-verification-secret-" + uuid.uuid4().hex)
    if not args.use_configured_storage:
        os.environ["DATASHELL_STORAGE_ROOT"] = str(workspace / "storage")
        os.environ["DATASHELL_DATA_DIR"] = str(workspace / "storage" / "data")
        os.environ["DATASHELL_OUTPUT_DIR"] = str(workspace / "storage" / "output")
        os.environ["DATASHELL_AUDIT_DIR"] = str(workspace / "storage" / "output" / "audits")
        os.environ["DATASHELL_QUARANTINE_DIR"] = str(workspace / "storage" / "output" / "quarantine")
        os.environ["DATASHELL_CONTRACTS_DIR"] = str(workspace / "storage" / "contracts_registry")
        os.environ.setdefault("ENVIRONMENT", "test")

    # Import only after the isolated storage / sink environment is in place:
    # EnterpriseSettings reads the process environment exactly once.
    from datashell.core import alerts as alert_module
    from datashell.core.config import settings

    checks: list[dict[str, Any]] = []
    correlation_ids: list[str] = []
    alerts_seen: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, detail: str, **extra: Any) -> None:
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail, **extra})

    config = alert_module.check_alert_configuration()
    add_check(
        "configuration",
        config["configured"],
        "alert webhook configuration is valid" if config["configured"] else "; ".join(config["issues"]),
        destination=config["destination"],
        signing_secret=config["signing_secret"],
    )
    if not config["configured"]:
        if sink:
            sink.stop()
        return 1, {
            "status": "FAIL",
            "mode": "local-sink" if sink else "configured-endpoint",
            "config": config,
            "checks": checks,
            "limitations": ["No alert could be sent because the configuration is incomplete."],
            "timestamp": _iso_now(),
        }

    if not args.transport_only:
        lifecycle_started = _iso_now()
        started_monotonic = time.monotonic()
        failure: str | None = None
        try:
            _trigger_failing_run(workspace)
        except Exception as exc:  # noqa: BLE001 - record the actual check failure
            failure = f"{type(exc).__name__}: {exc}"
        alert_log = settings.output_dir / "alerts" / "alert-deliveries.jsonl"
        record = _wait_for_lifecycle_alert(alert_log, lifecycle_started, args.timeout)
        if failure:
            add_check("lifecycle-alert-created", False, f"failing run path raised unexpectedly: {failure}")
        elif record is None:
            add_check(
                "lifecycle-alert-created",
                False,
                "no DataShellPipelineFailures delivery record appeared within the timeout",
            )
        else:
            alerts_seen.append(record)
            correlation_ids.append(str(record.get("correlation_id")))
            latency_ms = round((time.monotonic() - started_monotonic) * 1000, 1)
            add_check(
                "lifecycle-alert-created",
                True,
                "normal pipeline failure path created and dispatched a DataShellPipelineFailures alert",
                alert_id=record.get("alert_id"),
                correlation_id=record.get("correlation_id"),
            )
            add_check(
                "lifecycle-transport",
                record.get("outcome") == "delivered",
                f"terminal transport outcome: {record.get('outcome')}",
                alert_id=record.get("alert_id"),
                attempts=record.get("attempt"),
                http_status=record.get("http_status"),
                destination=record.get("destination"),
                latency_ms=latency_ms,
            )

    if not args.lifecycle_only:
        correlation = args.correlation_id or f"verify_{uuid.uuid4().hex}"
        correlation_ids.append(correlation)
        alert = alert_module.build_verification_alert(correlation)
        alert_module.emit_alert(alert)
        result = alert_module.wait_for_delivery(alert.alert_id, timeout_seconds=args.timeout)
        if result is None:
            add_check("transport-alert-delivered", False, "synthetic verification alert was not delivered in time")
        else:
            add_check(
                "transport-alert-delivered",
                result.outcome == "delivered",
                f"terminal transport outcome: {result.outcome}",
                alert_id=result.alert_id,
                correlation_id=correlation,
                attempts=result.attempts,
                http_status=result.http_status,
                destination=result.destination,
                latency_ms=round(result.duration_seconds * 1000, 1),
            )
            alerts_seen.append(
                {
                    "alert_id": result.alert_id,
                    "correlation_id": correlation,
                    "rule": "DataShellDeliveryVerification",
                    "outcome": result.outcome,
                    "attempts": result.attempts,
                    "http_status": result.http_status,
                }
            )

    if sink is not None:
        requests = sink.snapshot()
        add_check(
            "receiver-received-requests",
            len(requests) >= 1,
            f"loopback sink received {len(requests)} signed request(s)",
            received=len(requests),
        )
        secret = settings.alert_webhook_secret or settings.hmac_secret
        add_check(
            "receiver-signature-valid",
            bool(requests) and all(_verify_signature(request, secret) for request in requests),
            "every received request carried a valid X-DataShell-Signature",
        )
        payloads = []
        for request in requests:
            try:
                payloads.append(json.loads(request["body"]))
            except ValueError:
                continue
        required = {"alert_id", "rule", "severity", "timestamp", "source", "evidence", "recommended_action"}
        add_check(
            "receiver-payload-complete",
            bool(payloads) and all(required <= set(payload) for payload in payloads),
            "every received payload carried alert_id, rule, severity, timestamp, "
            "source, evidence, recommended_action",
        )
        # Prove the lifecycle record and the synthetic record are not merely
        # enqueued: the receiver saw their exact alert IDs.
        received_ids = {
            request["headers"].get("x-datashell-alert-id") for request in requests
        }
        expected_ids = {str(alert["alert_id"]) for alert in alerts_seen if alert.get("alert_id")}
        add_check(
            "receiver-received-alert-ids",
            bool(expected_ids) and expected_ids <= received_ids,
            f"receiver saw all {len(expected_ids)} expected alert ID(s)",
            expected_alert_ids=sorted(expected_ids),
            received_alert_ids=sorted(str(value) for value in received_ids if value),
        )
        sink.stop()

    report = {
        "status": "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL",
        "mode": "local-sink" if args.local_sink else "configured-endpoint",
        "environment": settings.environment,
        "timestamp": _iso_now(),
        "config": config,
        "checks": checks,
        "correlation_ids": correlation_ids,
        "alerts": alerts_seen,
        "limitations": [
            "A successful HTTP response proves the configured receiving endpoint accepted the signed alert; "
            "it does not prove that a human read or acknowledged it.",
            "Local-sink mode is local integration evidence, not target-deployment or recipient-side evidence.",
            "This check exercises the installed Data Shell code and the normal pipeline failure/recovery paths; "
            "it does not replace the target-host procedure in docs/production/DEPLOYMENT_VERIFICATION.md section 11.",
        ],
    }
    return (0 if report["status"] == "PASS" else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Data Shell alert creation and delivery to a human-operated endpoint."
    )
    parser.add_argument("--local-sink", action="store_true", help="Run against an ephemeral loopback receiver")
    parser.add_argument("--transport-only", action="store_true", help="Skip the real failing-run lifecycle check")
    parser.add_argument("--lifecycle-only", action="store_true", help="Skip the synthetic transport check")
    parser.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait for each delivery (default: 20)")
    parser.add_argument("--correlation-id", default=None, help="Explicit correlation ID for the synthetic alert")
    parser.add_argument(
        "--use-configured-storage",
        action="store_true",
        help="Write the synthetic failing run to the configured storage root instead of an isolated temp root",
    )
    parser.add_argument("--json", action="store_true", help="Emit the sanitized result as JSON")
    args = parser.parse_args(argv)
    if args.transport_only and args.lifecycle_only:
        parser.error("--transport-only and --lifecycle-only are mutually exclusive")
    exit_code, report = run(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print("Data Shell — alert delivery verification\n")
        for check in report["checks"]:
            marker = "PASS" if check["status"] == "PASS" else "FAIL"
            print(f"  [{marker}] {check['name']}: {check['detail']}")
            for key in ("alert_id", "correlation_id", "attempts", "http_status", "destination", "latency_ms"):
                if check.get(key) is not None:
                    print(f"         {key}: {check[key]}")
        print("\n" + "=" * 72)
        print(f"Result: {report['status']} ({report['mode']})")
        for limitation in report["limitations"]:
            print(f"NOTE: {limitation}")
        print("=" * 72)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Enterprise Telemetry: Prometheus Metrics & Structured Logging."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

# ----------------- Prometheus Metrics -----------------

# Avoid tenant, contract, and rule identifiers as metric labels: these are
# unbounded and may contain sensitive business metadata. Use structured audit
# logs for per-tenant investigations and aggregate metrics for alerting.
RUNS_TOTAL = Counter(
    "datashell_runs_total",
    "Total boundary enforcement runs executed",
    ["status"],
)

ROWS_READ_TOTAL = Counter("datashell_rows_read_total", "Total rows read at the boundary")
ROWS_DELIVERED_TOTAL = Counter("datashell_rows_delivered_total", "Total rows delivered to target storage")
ROWS_HELD_TOTAL = Counter("datashell_rows_held_total", "Total rows held by semantic quality rules")
ROWS_REFUSED_TOTAL = Counter(
    "datashell_rows_refused_total", "Total rows refused due to structural schema mismatch"
)
FOOTING_ERRORS_TOTAL = Counter(
    "datashell_footing_errors_total", "Total footing reconciliation balance failures"
)
RUN_DURATION_SECONDS = Histogram(
    "datashell_run_duration_seconds",
    "Latency distribution of boundary runs in seconds",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

ACTIVE_RUNS = Gauge(
    "datashell_active_runs",
    "Currently running pipeline boundary executions"
)


HTTP_REQUESTS_TOTAL = Counter(
    "datashell_http_requests_total",
    "HTTP requests handled by the Data Shell API",
    ["method", "route", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "datashell_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)
HTTP_IN_FLIGHT_REQUESTS = Gauge(
    "datashell_http_in_flight_requests",
    "HTTP requests currently being handled",
)
STORAGE_FREE_BYTES = Gauge(
    "datashell_storage_free_bytes",
    "Free bytes on the persistent storage filesystem",
)
STORAGE_TOTAL_BYTES = Gauge(
    "datashell_storage_total_bytes",
    "Total bytes on the persistent storage filesystem",
)

# Settlement lifecycle metrics. Labels are deliberately low-cardinality:
# no tenant, contract, settlement, or invoice identifiers ever appear here.
SETTLEMENTS_CALCULATED_TOTAL = Counter(
    "datashell_settlements_calculated_total",
    "Total settlement statements calculated",
    ["acceptance_status", "pricing_model"],
)
SETTLEMENTS_REJECTED_TOTAL = Counter(
    "datashell_settlements_rejected_total",
    "Total settlement calculations refused (integrity gate failure)",
    ["reason_code"],
)
SETTLEMENTS_DISPUTED_TOTAL = Counter(
    "datashell_settlements_disputed_total",
    "Total settlements marked disputed",
)
CERTIFICATE_VERIFICATION_FAILURES_TOTAL = Counter(
    "datashell_certificate_verification_failures_total",
    "Certificate verification failures observed during settlement",
    ["failure_kind"],
)
BILLING_INSTRUCTIONS_CREATED_TOTAL = Counter(
    "datashell_billing_instructions_created_total",
    "Billing instructions created",
    ["provider", "status"],
)
BILLING_EXTERNAL_RETRIES_TOTAL = Counter(
    "datashell_billing_external_retries_total",
    "External billing operations retried",
    ["provider"],
)
BILLING_UNRESOLVED_TOTAL = Counter(
    "datashell_billing_unresolved_total",
    "Billing operations with ambiguous external outcome awaiting reconciliation",
    ["provider"],
)
SETTLEMENT_CALCULATION_DURATION_SECONDS = Histogram(
    "datashell_settlement_calculation_duration_seconds",
    "Latency distribution of settlement calculations in seconds",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)
LEDGER_PERSISTENCE_FAILURES_TOTAL = Counter(
    "datashell_ledger_persistence_failures_total",
    "Settlement ledger persistence failures",
    ["entity"],
)

# Operational state of the durable pending-ledger recovery path. These are
# gauges (point-in-time state, not a rate) so an alert can fire on "there are
# unreconciled entries right now" rather than on a rate that would need a
# nonzero window to evaluate.
PENDING_LEDGER_ENTRIES = Gauge(
    "datashell_pending_ledger_entries",
    "Published runs whose ledger record is still pending reconciliation",
)
PUBLICATION_RECOVERY_ERRORS = Gauge(
    "datashell_publication_recovery_errors",
    "Publication journal recovery errors observed at the last startup recovery",
)
PUBLICATION_RECOVERY_JOURNALS = Gauge(
    "datashell_publication_recovery_journals",
    "Publication journals recovered at the last startup recovery",
)

# Alert delivery is a separate lifecycle from run accounting: an alert can be
# created correctly and still fail to reach a human. These metrics make that
# distinction observable without putting alert IDs (unbounded cardinality) in
# labels.
ALERT_DELIVERIES_TOTAL = Counter(
    "datashell_alert_deliveries_total",
    "Alert delivery attempts by terminal outcome",
    ["outcome"],
)
ALERT_DELIVERY_DURATION_SECONDS = Histogram(
    "datashell_alert_delivery_duration_seconds",
    "End-to-end alert delivery duration in seconds (including bounded retries)",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)
ALERT_DELIVERY_PENDING = Gauge(
    "datashell_alert_delivery_pending",
    "Alerts persisted locally whose delivery has not succeeded",
)


# ----------------- Structured Logging -----------------

class StructuredLogger:
    """Emits production JSON logs (default) or operator-friendly lines.

    ``LOG_FORMAT=structured`` (the default) emits one JSON object per line so
    log shippers get stable fields. ``LOG_FORMAT=pretty`` renders a
    human-readable line for interactive sessions (``docker logs -f``,
    local shells): the message first, optional ``key=value`` context after.
    """

    def __init__(self, name: str = "datashell", log_format: str | None = None) -> None:
        self.format = (
            log_format if log_format is not None else os.getenv("LOG_FORMAT", "structured")
        ).strip().lower()
        if self.format not in ("structured", "pretty"):
            raise RuntimeError("LOG_FORMAT must be 'structured' or 'pretty'")
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.propagate = False

    def _render(self, level: str, message: str, **kwargs: Any) -> str:
        if self.format == "pretty":
            context = " ".join(f"{key}={value}" for key, value in kwargs.items())
            rendered = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{level.upper():<7}] {message}"
            return rendered if not context else rendered + " — " + context
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level.upper(),
            "message": message,
            **kwargs,
        }
        return json.dumps(payload, default=str)

    def log(self, level: str, message: str, **kwargs: Any) -> None:
        line = self._render(level, message, **kwargs)
        if level.upper() == "ERROR":
            self.logger.error(line)
        elif level.upper() == "WARNING":
            self.logger.warning(line)
        else:
            self.logger.info(line)

    def info(self, message: str, **kwargs: Any) -> None:
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self.log("ERROR", message, **kwargs)


telemetry_logger = StructuredLogger("datashell.telemetry")

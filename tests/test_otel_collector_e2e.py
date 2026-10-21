"""End-to-end OpenTelemetry export against a real collector.

``tests/test_observability.py`` pins the *contract* of the tracing bridge with
a fake tracer: the activation gate, the span name, the attributes. This module
pins the *wire*: the real OpenTelemetry SDK, the real OTLP/gRPC exporter, and a
real collector process that has to accept the gateway's spans.

The check is opt-in because it needs two things a plain ``pytest`` run does not
have:

* the ``datashell[otel]`` extra installed (SDK + gRPC exporter), and
* a running collector whose captured spans are readable on the host.

``scripts/run_otel_collector_e2e.sh`` provides both and is what CI runs (job
``otel-collector-e2e`` in ``.github/workflows/ci.yml``). Environment contract:

* ``DATASHELL_OTEL_E2E=1`` enables the check; unset skips it.
* ``OTEL_EXPORTER_OTLP_ENDPOINT`` is the collector's OTLP/gRPC endpoint
  (``http://host:port``).
* ``DATASHELL_OTEL_SPANS_FILE`` is the path of the collector's captured spans
  (one JSON object per line).
* ``DATASHELL_OTEL_SERVICE_NAME`` is the ``service.name`` the collector is
  expected to see.

The gateway is driven through its own ASGI stack, so the exported span is the
one a deployed gateway emits. What is *not* covered here is the transport to a
production tracing backend (Tempo, Jaeger, Elastic): the collector is the last
hop this repository owns.

FastAPI ships its own OpenTelemetry instrumentation (scope ``fastapi``) and
names its server span after the route too, so a collector receives both the
framework's span and the gateway's. The check therefore selects the gateway's
span by instrumentation scope (``datashell``) — the one this repository owns.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

import datashell.core.observability as observability
from datashell.core.observability import configure_otel, tracing_enabled
from datashell.server.app import app

E2E_ENV = "DATASHELL_OTEL_E2E"
SPANS_FILE_ENV = "DATASHELL_OTEL_SPANS_FILE"
SERVICE_NAME_ENV = "DATASHELL_OTEL_SERVICE_NAME"
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

EXPECTED_SERVICE_NAME = "datashell-e2e"
EXPECTED_SPAN_NAME = "GET /health"
EXPECTED_ROUTE = "/health"
# The gateway's own instrumentation scope (``trace.get_tracer("datashell")`` in
# ``configure_otel``). FastAPI ships its own OpenTelemetry instrumentation
# (scope ``fastapi``) and names its server span after the route as well, so
# the scope is what distinguishes the gateway's span from the framework's.
GATEWAY_SCOPE = "datashell"
COLLECTOR_READY_TIMEOUT_SECONDS = 15.0
SPAN_WAIT_TIMEOUT_SECONDS = 30.0

pytestmark = [
    pytest.mark.otel_e2e,
    pytest.mark.skipif(
        os.getenv(E2E_ENV, "") != "1",
        reason=f"set {E2E_ENV}=1 (and run a collector) to exercise the real export path",
    ),
]


# ----------------- collector capture parsing -----------------

def _attribute_value(value: dict[str, Any]) -> Any:
    """Unwrap one OTLP AnyValue into a plain Python value."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    return value


def _attributes(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {entry.get("key", ""): _attribute_value(entry.get("value", {})) for entry in entries}


def _iter_spans(path: Path) -> Iterator[dict[str, Any]]:
    """Yield every span the collector captured, flattened for assertions.

    The collector's ``file`` exporter writes one JSON object per export
    request (newline-delimited). Each object holds ``resourceSpans`` with the
    resource attributes, the instrumentation scope, and the spans themselves.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        for resource_spans in payload.get("resourceSpans", []):
            resource = _attributes(resource_spans.get("resource", {}).get("attributes", []))
            for scope_spans in resource_spans.get("scopeSpans", []):
                scope = (scope_spans.get("scope") or {}).get("name", "")
                for span in scope_spans.get("spans", []):
                    yield {
                        "name": span.get("name", ""),
                        "scope": scope,
                        "resource": resource,
                        "attributes": _attributes(span.get("attributes", [])),
                        "status": span.get("status", {}),
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                    }


def _wait_for_collector(endpoint: str, timeout: float = COLLECTOR_READY_TIMEOUT_SECONDS) -> None:
    """Block until the collector's OTLP/gRPC port accepts a connection."""
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4317
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:  # pragma: no cover - timing dependent
            last_error = exc
            time.sleep(0.5)
    pytest.fail(f"collector at {host}:{port} never accepted a connection: {last_error}")


def _wait_for_span(
    path: Path,
    span_name: str,
    scope: str,
    seen_trace_ids: set[str],
    timeout: float = SPAN_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Poll the collector's capture file until a *new* expected span arrives.

    ``seen_trace_ids`` holds the spans the collector had already captured
    before this exchange. Only a span that was not there before counts, so a
    stale capture file can never make the check pass.

    The scope matters: FastAPI's built-in instrumentation also emits a server
    span named after the route, under the scope ``fastapi``. Only the span
    the gateway itself produced (scope ``datashell``) is the one under test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for span in _iter_spans(path):
            if (
                span["name"] == span_name
                and span["scope"] == scope
                and span["trace_id"] not in seen_trace_ids
            ):
                return span
        time.sleep(0.25)
    pytest.fail(
        f"collector never received a new span {scope!r}/{span_name!r}; "
        f"captured spans: {[(s['scope'], s['name']) for s in _iter_spans(path)]}"
    )


def _force_flush() -> None:
    """Push the batched spans to the collector without waiting for the timer."""
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    assert callable(flush), "the global tracer provider cannot be flushed"
    assert flush(timeout_millis=10_000), "the tracer provider failed to flush its spans"


# ----------------- the end-to-end check -----------------

def test_gateway_spans_reach_a_real_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway exchange must arrive at a real collector as a real span.

    This is the whole point of the check: the activation path (env var +
    installed extra) must produce spans that leave the process over OTLP/gRPC
    and are accepted by a collector — not merely spans recorded in memory.
    """
    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    assert endpoint, f"{ENDPOINT_ENV} must point at the collector"
    spans_file_env = os.environ.get(SPANS_FILE_ENV, "").strip()
    assert spans_file_env, f"{SPANS_FILE_ENV} must point at the collector's capture file"
    spans_file = Path(spans_file_env).expanduser()

    try:
        import opentelemetry.sdk  # noqa: F401
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: F401
            OTLPSpanExporter,
        )
    except ImportError as exc:  # pragma: no cover - environment guard
        pytest.fail(f"the datashell[otel] extra is required for the end-to-end check: {exc}")

    _wait_for_collector(endpoint)

    # The service name is operator configuration; the collector must see the
    # configured value, not a hard-coded one.
    service_name = os.getenv(SERVICE_NAME_ENV, "").strip() or EXPECTED_SERVICE_NAME
    monkeypatch.setenv(SERVICE_NAME_ENV, service_name)
    # Export promptly: the batch processor would otherwise wait out its
    # schedule delay before the first export.
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "200")
    monkeypatch.setenv(ENDPOINT_ENV, endpoint)

    # Start from a clean activation state so configure_otel() really runs.
    observability._STATE.clear()
    observability._STATE.update({"configured": False, "tracer": None, "_reason": "not requested"})

    assert configure_otel() is True, "the tracing bridge refused to activate with the extra installed"
    assert tracing_enabled() is True

    # Whatever the collector captured before this exchange is not evidence for
    # this exchange: only a span that arrives from now on counts.
    seen_trace_ids = {span["trace_id"] for span in _iter_spans(spans_file)}

    with TestClient(app) as client:
        response = client.get(EXPECTED_ROUTE)
    assert response.status_code == 200

    _force_flush()

    span = _wait_for_span(spans_file, EXPECTED_SPAN_NAME, GATEWAY_SCOPE, seen_trace_ids)

    # The span the collector received must be the one the gateway produced.
    assert span["scope"] == GATEWAY_SCOPE
    assert span["resource"].get("service.name") == service_name
    assert span["attributes"].get("http.route") == EXPECTED_ROUTE
    assert span["attributes"].get("http.request.method") == "GET"
    assert span["attributes"].get("http.response.status_code") == 200
    assert span["trace_id"], "the exported span has no trace id"
    assert span["span_id"], "the exported span has no span id"
    # A successful exchange must not be recorded as an error span.
    assert span["status"].get("code", 0) in (0, "STATUS_CODE_UNSET"), span["status"]

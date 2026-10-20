"""Section-6 observability tests: OTel bridge and the benchmark CLI.

The tracing bridge must be a pure configuration gate: no endpoint env and no
SDK, zero behavioral difference. The enabled path is exercised through a fake
tracer (the opentelemetry packages are not installed in dev environments), so
the tests pin the *contract*: spans get named with the templated route and
carry status attributes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from fastapi.testclient import TestClient

import datashell.core.observability as observability
from datashell.core.observability import (
    configure_otel,
    http_server_span,
    tracing_enabled,
)
from datashell.server.app import app

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _reset_otel_state(monkeypatch: pytest.MonkeyPatch):
    """Every test starts and ends with tracing disabled, env untouched."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("DATASHELL_OTEL_SERVICE_NAME", raising=False)
    observability._STATE.clear()
    observability._STATE.update(
        {"configured": False, "tracer": None, "_reason": "not requested"})
    yield
    observability._STATE.clear()
    observability._STATE.update(
        {"configured": False, "tracer": None, "_reason": "not requested"})


class FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def update_name(self, name: str) -> None:
        self.name = name

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def __enter__(self) -> FakeSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.ended = True
        return False


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []
        self.start_attributes: dict[str, Any] | None = None

    def start_as_current_span(
        self, name: str, attributes: dict[str, Any] | None = None, **_kw: Any,
    ) -> FakeSpan:
        span = FakeSpan(name)
        self.start_attributes = attributes
        self.spans.append(span)
        return span


# ----------------- Configuration gate -----------------

def test_no_request_no_sdk_means_disabled() -> None:
    """Without OTEL_EXPORTER_OTLP_ENDPOINT the bridge stays inert."""
    assert configure_otel() is False
    assert tracing_enabled() is False


def test_configure_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second configure call cannot flip state unexpectedly."""
    assert configure_otel() is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    assert configure_otel() is False  # still disabled: already consulted once
    assert tracing_enabled() is False


def test_requested_without_sdk_warns_and_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint set but the extra is missing: warn, do not crash, stay off."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    # A None entry in sys.modules makes the import itself raise ImportError,
    # which is exactly the "extra not installed" branch configure_otel guards.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    assert configure_otel() is False
    assert tracing_enabled() is False


# ----------------- Span surface -----------------

def test_http_span_is_a_noop_when_disabled() -> None:
    with http_server_span("GET") as span:
        assert span is None


def test_middleware_names_spans_with_the_templated_route() -> None:
    """Enabled path: the exchange span is renamed to 'METHOD /health' with attrs."""
    fake = FakeTracer()
    observability._STATE["tracer"] = fake
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert fake.spans, "expected a server span for the exchange"
    span = fake.spans[0]
    assert span.ended
    assert span.name == "GET /health"
    assert span.attributes.get("http.route") == "/health"
    assert span.attributes.get("http.response.status_code") == 200
    assert fake.start_attributes == {"http.request.method": "GET"}


def test_disabled_span_leaves_response_untouched() -> None:
    """Tracing off == byte-identical behavior for callers."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("trace_")
    assert tracing_enabled() is False


# ----------------- Benchmark CLI -----------------

def test_benchmark_contract_matches_the_reviewed_example() -> None:
    """The CLI's embedded contract cannot drift from the reviewed JSON file."""
    from datashell.cli.main import _BENCHMARK_CONTRACT

    example_path = REPO_ROOT / "examples" / "vendor_settlements_contract.json"
    if not example_path.is_file():
        pytest.skip("repository example not present (wheel-only checkout)")
    assert json.dumps(_BENCHMARK_CONTRACT, sort_keys=True) == json.dumps(
        json.loads(example_path.read_text(encoding="utf-8")), sort_keys=True)


def test_benchmark_cli_reports_reconciled_throughput(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The smallest honest measurement: a real run, real certificate, real math."""
    from datashell.cli.main import main

    out_dir = tmp_path / "bench"
    with pytest.raises(SystemExit) as excinfo:
        main(["benchmark", "--rows", "2000", "--out", str(out_dir), "--json"])
    assert excinfo.value.code == 0

    stdout = capsys.readouterr().out.strip()
    assert stdout.startswith("{"), f"expected pure JSON on stdout, got: {stdout[:120]!r}"
    payload = json.loads(stdout)
    assert payload["status"] == "LOADED"
    assert payload["rows"] == 2000
    assert payload["rows_read"] == 2000
    assert payload["rows_delivered"] == 2000
    assert payload["unaccounted_for"] == 0
    assert payload["footing_matched"] is True
    assert payload["rows_per_second"] > 0
    assert (out_dir / "curated.parquet").is_file()

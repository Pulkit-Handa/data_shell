"""Section-5 API-surface hardening tests.

Pins the guarantees every consumer depends on: every API endpoint declares an
authorization scope (verified at startup, not per-request), the OpenAPI
document stays human-readable and in sync, requests carry correlation IDs,
and operators can pick a human log format without losing structure.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from datashell.core.security import Capability, require_scope
from datashell.core.telemetry import StructuredLogger
from datashell.server.app import app, assert_routes_require_scope

REPO_ROOT = Path(__file__).parent.parent


# ----------------- Scope introspection -----------------

def test_startup_introspection_passes_for_the_shipped_gateway() -> None:
    """Every shipped /api route already carries a capability guard."""
    assert_routes_require_scope(app)


def test_startup_introspection_fails_for_an_unguarded_route() -> None:
    """A router endpoint without require_scope fails the process at boot."""
    unguarded = FastAPI()

    @unguarded.get("/api/v1/open-hole")
    def open_hole() -> dict[str, str]:
        return {"leak": "everything"}

    with pytest.raises(RuntimeError, match="/api/v1/open-hole"):
        assert_routes_require_scope(unguarded)


def test_require_scope_marks_the_guard_for_introspection() -> None:
    """The guard carries its capability so assert_routes_require_scope sees it."""
    guard = require_scope(Capability.AUDIT_READ)
    assert getattr(guard, "required_capability", None) == Capability.AUDIT_READ


def test_every_openapi_operation_has_a_summary() -> None:
    """The contract document reads like documentation, not a symbol dump."""
    schema = app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, methods in schema["paths"].items()
        for method in methods
        if not methods[method].get("summary")
    ]
    assert missing == []


# ----------------- Committed OpenAPI document -----------------

def test_committed_openapi_document_is_in_sync() -> None:
    """docs/openapi.yaml must be the exact export of the live schema (CI enforces)."""
    committed = yaml.safe_load(
        (REPO_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    assert committed == app.openapi()


def test_openapi_document_declares_api_key_security() -> None:
    """Consumers learn the X-API-Key auth from the document's scheme."""
    schema = app.openapi()
    assert "APIKeyHeader" in (schema.get("components", {}).get("securitySchemes", {}) or {})


# ----------------- Correlation IDs -----------------

def test_valid_request_id_is_echoed() -> None:
    """A well-formed X-Request-ID rides the response for end-to-end tracing."""
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "svc-a-run-42"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "svc-a-run-42"


def test_invalid_request_id_is_regenerated_not_echoed() -> None:
    """Control characters or oversized IDs are replaced by a generated one."""
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "bad id\nwith-newline"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("trace_")


# ----------------- Operator log format -----------------

class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _capturing_logger(name: str, log_format: str) -> tuple[StructuredLogger, _Capture]:
    logger = StructuredLogger(name, log_format=log_format)
    capture = _Capture()
    logger.logger.handlers = [capture]
    return logger, capture


def test_pretty_log_format_renders_human_lines() -> None:
    logger, capture = _capturing_logger("pretty-format-check", "pretty")
    logger.info("Boundary enforcement completed", run_id="dm_run_1", rows=6)
    line = capture.lines[-1]
    assert "Boundary enforcement completed" in line
    assert "run_id=dm_run_1" in line and "rows=6" in line
    assert not line.lstrip().startswith("{")


def test_structured_log_format_removes_json_lines_by_default() -> None:
    logger, capture = _capturing_logger("structured-format-check", "structured")
    logger.info("Boundary enforcement completed", run_id="dm_run_1")
    payload = json.loads(capture.lines[-1])
    assert payload["message"] == "Boundary enforcement completed"
    assert payload["run_id"] == "dm_run_1"
    assert payload["level"] == "INFO"


def test_unknown_log_format_fails_construction() -> None:
    with pytest.raises(RuntimeError, match="LOG_FORMAT"):
        StructuredLogger("bad-format-check", log_format="xml")

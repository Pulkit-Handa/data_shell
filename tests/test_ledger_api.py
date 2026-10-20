"""HTTP tests for the boundary-ledger query API.

Covers tenant isolation, capability enforcement, filtering, and readiness.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.core.security import APIKeyMetadata, Role, register_api_key
from datashell.server import app as app_module
from datashell.server.app import app

client = TestClient(app)

ALPHA_KEY = "ledger_test_alpha_key"
BETA_KEY = "ledger_test_beta_key"
PRODUCER_KEY = "ledger_test_producer_key"


def _register_keys():
    register_api_key(APIKeyMetadata(key=ALPHA_KEY, tenant_id="alpha", role=Role.AUDITOR, description="alpha auditor"))
    register_api_key(APIKeyMetadata(key=BETA_KEY, tenant_id="beta", role=Role.AUDITOR, description="beta auditor"))
    register_api_key(APIKeyMetadata(key=PRODUCER_KEY, tenant_id="alpha", role=Role.PRODUCER, description="producer"))


@pytest.fixture
def api_ledger(tmp_path, monkeypatch):
    _register_keys()
    db = tmp_path / "history.db"
    ledger = BoundaryLedger(db)
    ledger.record_run("run-alpha", "contract-a", 100, 90, {}, tenant_id="alpha", status="LOADED", rows_held=10)
    ledger.record_run("run-beta", "contract-b", 50, 50, {}, tenant_id="beta", status="LOADED")
    ledger.record_quarantine(
        "run-alpha", "alpha", "contract-a", "held_quality",
        str(tmp_path / "q.parquet"), 10, {"amount-positive": 10})
    ledger.record_usage("run-alpha", "alpha", "contract-a", "LOADED", "batch", 100, 90, bytes_processed=4096)
    monkeypatch.setattr(app_module, "BoundaryLedger", lambda: BoundaryLedger(db))
    return db


def test_runs_require_authentication(api_ledger):
    assert client.get("/api/v1/runs").status_code == 401


def test_list_runs_is_tenant_scoped(api_ledger):
    alpha = client.get("/api/v1/runs", headers={"X-API-Key": ALPHA_KEY}).json()
    beta = client.get("/api/v1/runs", headers={"X-API-Key": BETA_KEY}).json()
    assert [r["run_id"] for r in alpha["runs"]] == ["run-alpha"]
    assert [r["run_id"] for r in beta["runs"]] == ["run-beta"]


def test_summary_endpoint(api_ledger):
    payload = client.get("/api/v1/runs/summary", headers={"X-API-Key": ALPHA_KEY}).json()
    assert payload["tenant_id"] == "alpha"
    assert payload["total_runs"] == 1
    assert payload["rows_read"] == 100


def test_run_detail_and_cross_tenant_denial(api_ledger):
    own = client.get("/api/v1/runs/run-alpha", headers={"X-API-Key": ALPHA_KEY})
    assert own.status_code == 200
    assert own.json()["contract_name"] == "contract-a"

    # beta defaults to its own tenant -> the alpha run is not visible
    assert client.get("/api/v1/runs/run-alpha", headers={"X-API-Key": BETA_KEY}).status_code == 404
    # beta explicitly asking for alpha's tenant is an explicit denial
    cross = client.get("/api/v1/runs/run-alpha?tenant_id=alpha", headers={"X-API-Key": BETA_KEY})
    assert cross.status_code == 403


def test_run_detail_rejects_malformed_run_id(api_ledger):
    resp = client.get("/api/v1/runs/bad..id", headers={"X-API-Key": ALPHA_KEY})
    assert resp.status_code == 400


def test_admin_can_query_another_tenant(api_ledger):
    resp = client.get("/api/v1/runs?tenant_id=beta", headers={"X-API-Key": settings.master_api_key})
    assert resp.status_code == 200
    assert [r["run_id"] for r in resp.json()["runs"]] == ["run-beta"]


def test_quarantine_endpoint(api_ledger):
    payload = client.get("/api/v1/quarantine", headers={"X-API-Key": ALPHA_KEY}).json()
    assert payload["count"] == 1
    assert payload["quarantine"][0]["dominant_reason"] == "amount-positive"


def test_usage_endpoint_requires_metrics_capability(api_ledger):
    ok = client.get("/api/v1/usage", headers={"X-API-Key": ALPHA_KEY})
    assert ok.status_code == 200
    assert ok.json()["bytes_processed"] == 4096

    denied = client.get("/api/v1/usage", headers={"X-API-Key": PRODUCER_KEY})
    assert denied.status_code == 403
    assert "lacks capability 'metrics:read'" in denied.json()["detail"]


def test_readiness_checks_ledger(api_ledger):
    assert client.get("/ready").status_code == 200

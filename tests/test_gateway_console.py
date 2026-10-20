"""Section-4 gateway composition constraints.

The app assembly must stay a small cross-cutting core (startup recovery,
middleware, probes, asset serving) with endpoint groups split into routers,
so the interactive console ships as a static asset wired only to the real
gateway API. These tests pin the refactor from regressing into a monolith.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from datashell.core.security import APIKeyMetadata, Role, register_api_key
from datashell.server.app import CONSOLE_HTML_PATH, app

REPO_ROOT = Path(__file__).parent.parent
APP_SOURCE = REPO_ROOT / "datashell" / "server" / "app.py"

CONSOLE_KEY = "console_test_admin_key_" + uuid.uuid4().hex[:8]

# The dashboard renders live gateway data; these are the endpoints it must use.
DASHBOARD_WIRING = ("/api/v1/enforce", "/api/v1/runs", "/metrics", "/api/v1/settlement/preview")


def test_app_assembly_stays_beneath_the_line_budget() -> None:
    """app.py holds assembly + middleware + probes only; the split is the point."""
    assert APP_SOURCE.is_file()
    assert APP_SOURCE.read_text(encoding="utf-8").count("\n") < 400
    routers_dir = REPO_ROOT / "datashell" / "server" / "routers"
    assert {p.stem for p in routers_dir.glob("*.py")} >= {
        "admin", "contracts", "ledger", "runs", "settlements"}


def test_console_asset_is_a_self_contained_document() -> None:
    """No template engine, no CDN fonts, no fetches outside the gateway origin."""
    html = CONSOLE_HTML_PATH.read_text(encoding="utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<script>" in html and "<style>" in html
    assert "https://" not in html and "http://" not in html
    for wiring in DASHBOARD_WIRING:
        assert wiring in html


def test_console_is_served_at_root() -> None:
    """/ is public and serves the same document that ships in the wheel."""
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == CONSOLE_HTML_PATH.read_text(encoding="utf-8")
    assert "Data Shell" in response.text


def test_console_does_not_render_without_credentials() -> None:
    """The document is public but every data panel fails closed on 401."""
    with TestClient(app) as client:
        for path in (
            "/api/v1/contracts", "/api/v1/runs",
            "/api/v1/settlement/preview", "/metrics",
        ):
            assert client.get(path).status_code == 401, path


def test_contract_catalog_endpoint_lists_own_and_shared(client_factory=None) -> None:
    """GET /api/v1/contracts returns the tenant catalog powering the selector."""
    register_api_key(APIKeyMetadata(
        key=CONSOLE_KEY, tenant_id="console-tenant", role=Role.ADMIN,
        description="console catalog listing"))
    with TestClient(app) as client:
        forbidden_or_ok = client.get("/api/v1/contracts")
        assert forbidden_or_ok.status_code == 401  # without the header

        contract_payload = json.loads(
            (REPO_ROOT / "examples" / "vendor_settlements_contract.json").read_text())
        registered = client.post(
            "/api/v1/contracts",
            headers={"X-API-Key": CONSOLE_KEY},
            json=contract_payload,
        )
        assert registered.status_code == 200

        listing = client.get("/api/v1/contracts", headers={"X-API-Key": CONSOLE_KEY}).json()
    assert listing["tenant_id"] == "console-tenant"
    assert listing["count"] == len(listing["contracts"]) >= 1
    entry = next(c for c in listing["contracts"]
                 if c["name"] == contract_payload["metadata"]["name"])
    assert entry["version"] == contract_payload["metadata"]["version"]
    assert entry["source"] == "tenant"


def test_contract_catalog_listings_are_tenant_scoped() -> None:
    """A tenant's listing never exposes another tenant's private contracts."""
    private_name = f"console-private-{uuid.uuid4().hex[:8]}"
    neighbor_key = "console_test_neighbor_" + uuid.uuid4().hex[:8]
    register_api_key(APIKeyMetadata(
        key="console_test_second_" + uuid.uuid4().hex[:8],
        tenant_id="console-tenant", role=Role.ADMIN, description="private contract owner"))
    register_api_key(APIKeyMetadata(
        key=neighbor_key, tenant_id="console-neighbor", role=Role.AUDITOR,
        description="no-visibility neighbor"))

    with TestClient(app) as client:
        payload = json.loads(
            (REPO_ROOT / "examples" / "vendor_settlements_contract.json").read_text())
        payload["metadata"]["name"] = private_name
        registered = client.post(
            "/api/v1/contracts",
            headers={"X-API-Key": CONSOLE_KEY},
            json=payload,
        )
        assert registered.status_code == 200

        own = client.get("/api/v1/contracts", headers={"X-API-Key": CONSOLE_KEY}).json()
        neighbor = client.get("/api/v1/contracts", headers={"X-API-Key": neighbor_key}).json()
    assert any(c["name"] == private_name for c in own["contracts"])
    assert all(c["name"] != private_name for c in neighbor["contracts"])

"""Exercise managed API-key creation and revocation through the authenticated API."""
import secrets

from fastapi.testclient import TestClient

from datashell.core.security import APIKeyMetadata, Role, register_api_key, revoke_api_key
from datashell.server.app import app


def test_admin_can_create_and_revoke_a_managed_auditor_key():
    client = TestClient(app)
    admin_key = "test_admin_" + secrets.token_urlsafe(24)
    register_api_key(APIKeyMetadata(
        key=admin_key,
        tenant_id="test-admin-tenant",
        role=Role.ADMIN,
        description="admin key API test",
    ), persist=False)
    created_key = None
    try:
        response = client.post(
            "/api/v1/admin/keys",
            headers={"X-API-Key": admin_key},
            json={"tenant_id": "tenant-api-test", "role": "AUDITOR", "description": "test auditor"},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        created_key = payload["api_key"]
        assert len(payload["key_id"]) == 24
        assert payload["tenant_id"] == "tenant-api-test"

        metrics = client.get("/metrics", headers={"X-API-Key": created_key})
        assert metrics.status_code == 200

        revoked = client.delete(
            f"/api/v1/admin/keys/{payload['key_id']}",
            headers={"X-API-Key": admin_key},
        )
        assert revoked.status_code == 200, revoked.text
        assert client.get("/metrics", headers={"X-API-Key": created_key}).status_code == 401
    finally:
        if created_key:
            revoke_api_key(created_key)
        revoke_api_key(admin_key)

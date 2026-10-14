"""Tests for request limits and production-oriented runtime guardrails."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from datashell.core.config import EnterpriseSettings
from datashell.core.security import APIKeyMetadata, Role, TokenBucketRateLimiter
from datashell.server.middleware import RequestBodyLimitMiddleware


def test_production_model_rejects_missing_secrets_even_if_process_environment_is_not_production(monkeypatch):
    monkeypatch.delenv("DATASHELL_API_KEY", raising=False)
    monkeypatch.delenv("DATASHELL_HMAC_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="DATASHELL_API_KEY"):
        EnterpriseSettings(environment="production", allowed_origins=["https://console.example.com"])


def test_production_origins_require_exact_https_origins(monkeypatch):
    import secrets
    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    with pytest.raises(RuntimeError, match="exact HTTPS origin"):
        EnterpriseSettings(
            environment="production",
            allowed_origins=["https://console.example.com/path"],
        )
    with pytest.raises(RuntimeError, match="exact HTTPS origin"):
        EnterpriseSettings(environment="production", allowed_origins=["http://console.example.com"])


def test_production_signing_keyring_preserves_previous_verification_keys(monkeypatch):
    import json
    import secrets
    active_secret = secrets.token_hex(32)
    previous_secret = secrets.token_hex(32)
    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", active_secret)
    monkeypatch.setenv("DATASHELL_KEY_ID", "datashell-hmac-v2")
    monkeypatch.setenv("DATASHELL_HMAC_KEYRING", json.dumps({"datashell-hmac-v1": previous_secret}))
    cfg = EnterpriseSettings(environment="production", allowed_origins=["https://console.example.com"])
    assert cfg.active_key_id == "datashell-hmac-v2"
    assert cfg.trusted_hmac_keys["datashell-hmac-v1"] == previous_secret
    assert cfg.trusted_hmac_keys["datashell-hmac-v2"] == active_secret
    assert "datashell-hmac-v1" in cfg.trusted_key_ids


def test_production_rejects_wildcard_trusted_proxy(monkeypatch):
    import secrets
    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(RuntimeError, match="wildcard trust is forbidden"):
        EnterpriseSettings(environment="production", allowed_origins=["https://console.example.com"])


def test_runtime_limits_must_be_positive():
    with pytest.raises(RuntimeError, match="MAX_CONCURRENT_RUNS"):
        EnterpriseSettings(max_concurrent_runs=0)
    with pytest.raises(RuntimeError, match="MAX_UPLOAD_SIZE_BYTES"):
        EnterpriseSettings(max_upload_size_bytes=0)


def test_api_key_metadata_rejects_invalid_tenant_ids():
    with pytest.raises(ValueError):
        APIKeyMetadata(key="test-key", tenant_id=" tenant ", role=Role.PRODUCER, description="test")


def test_rate_limiter_does_not_grow_forever_for_idle_identities(monkeypatch):
    import datashell.core.security as security

    clock = {"now": 1000.0}
    monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
    limiter = TokenBucketRateLimiter(rate_per_minute=1)
    assert limiter.allow("first")
    clock["now"] += 61
    assert limiter.allow("second")
    assert "first" in limiter.buckets
    clock["now"] += 601
    assert limiter.allow("third")
    assert "first" not in limiter.buckets


def test_body_limit_rejects_oversized_content_length():
    async def scenario():
        called = {"app": False}
        sent = []

        async def app(scope, receive, send):
            called["app"] = True

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(app, max_body_bytes=10)
        await middleware(
            {"type": "http", "method": "POST", "headers": [(b"content-length", b"11")]},
            receive,
            send,
        )
        assert called["app"] is False
        assert sent[0]["status"] == 413
        assert json.loads(sent[1]["body"])["detail"]

    asyncio.run(scenario())


def test_body_limit_counts_chunked_body_without_content_length():
    async def scenario():
        sent = []
        calls = {"count": 0}

        async def app(scope, receive, send):
            while True:
                message = await receive()
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break

        async def receive():
            calls["count"] += 1
            if calls["count"] == 1:
                return {"type": "http.request", "body": b"123456", "more_body": True}
            return {"type": "http.request", "body": b"78901", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(app, max_body_bytes=10)
        await middleware({"type": "http", "method": "POST", "headers": []}, receive, send)
        assert sent[0]["status"] == 413

    asyncio.run(scenario())


def test_production_credentials_survive_cache_loss_and_revoke_durably(tmp_path, monkeypatch):
    import hashlib
    import datashell.core.security as security

    monkeypatch.setattr(security.settings, "environment", "production")
    monkeypatch.setattr(security.settings, "output_dir", tmp_path)
    key = "dm_live_" + "a" * 48
    metadata = APIKeyMetadata(key=key, tenant_id="tenant-durable", role=Role.PRODUCER, description="durable test")
    security.register_api_key(metadata, persist=True)
    security.API_KEY_STORE.pop(key, None)  # Simulate a process restart/cache loss.

    resolved = security.get_api_key_metadata(key)
    assert resolved is not None
    assert resolved.tenant_id == "tenant-durable"
    assert key.encode("utf-8") not in security.settings.security_db_path.read_bytes()

    key_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    assert security.revoke_api_key_id(key_id, actor_tenant_id="security-test-actor") is True
    assert security.get_api_key_metadata(key) is None
    import sqlite3
    conn = sqlite3.connect(security.settings.security_db_path)
    try:
        events = conn.execute(
            "SELECT event_type, actor_tenant_id, key_id FROM security_events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()
    assert [event[0] for event in events] == ["created", "revoked"]
    assert events[1][1] == "security-test-actor"
    assert events[0][2] == key_id


def test_body_preflight_rejects_missing_credentials_before_app_body_parser(monkeypatch):
    import datashell.core.security as security

    monkeypatch.setattr(security.settings, "auth_enabled", True)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/enforce",
        "headers": [],
        "client": ("198.51.100.234", 12345),
    }
    status_code, metadata, detail = security.preflight_authenticate_scope(scope)
    assert status_code == 401
    assert metadata is None
    assert "API-Key" in detail


def test_body_preflight_authenticates_valid_key_without_double_rate_limit(monkeypatch):
    import datashell.core.security as security

    monkeypatch.setattr(security.settings, "auth_enabled", True)
    key = "preflight-test-" + __import__("secrets").token_urlsafe(20)
    metadata = APIKeyMetadata(key=key, tenant_id="tenant-preflight", role=Role.PRODUCER, description="preflight")
    security.register_api_key(metadata, persist=False)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/enforce",
        "headers": [(b"x-api-key", key.encode("utf-8"))],
        "client": ("198.51.100.235", 12345),
    }
    try:
        status_code, resolved, detail = security.preflight_authenticate_scope(scope)
        assert status_code is None
        assert resolved is metadata
        assert detail == ""
    finally:
        security.revoke_api_key(key)


def test_body_preflight_rejects_missing_capability_before_payload_parse(monkeypatch):
    import datashell.core.security as security

    monkeypatch.setattr(security.settings, "auth_enabled", True)
    key = "preflight-producer-" + __import__("secrets").token_urlsafe(20)
    metadata = APIKeyMetadata(key=key, tenant_id="tenant-preflight", role=Role.PRODUCER, description="producer")
    security.register_api_key(metadata, persist=False)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/admin/keys",
        "headers": [(b"x-api-key", key.encode("utf-8"))],
        "client": ("198.51.100.236", 12345),
    }
    try:
        status_code, resolved, detail = security.preflight_authenticate_scope(scope)
        assert status_code == 403
        assert resolved is None
        assert "required capability" in detail
    finally:
        security.revoke_api_key(key)




def test_signing_key_resolution_never_falls_back_to_another_key():
    from datashell.core.crypto import CryptoEngine

    active = "active-key"
    keyring = {active: "active-secret"}
    assert CryptoEngine.resolve_verification_secret(active, keyring) == "active-secret"
    assert CryptoEngine.resolve_verification_secret("listed-but-not-provisioned", keyring) is None
    assert CryptoEngine.resolve_verification_secret("", keyring) is None
    assert CryptoEngine.resolve_verification_secret("active-key", None) is None


def test_production_authentication_and_signing_secrets_must_be_distinct(monkeypatch):
    import secrets

    shared = secrets.token_hex(32)
    monkeypatch.setenv("DATASHELL_API_KEY", shared)
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", shared)
    with pytest.raises(RuntimeError, match="must be distinct"):
        EnterpriseSettings(environment="production", allowed_origins=["https://console.example.com"])


def test_signing_key_rotation_requires_a_new_key_id(monkeypatch):
    import json
    import secrets

    monkeypatch.setenv("DATASHELL_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("DATASHELL_KEY_ID", "same-key-id")
    monkeypatch.setenv("DATASHELL_HMAC_KEYRING", json.dumps({"same-key-id": secrets.token_hex(32)}))
    with pytest.raises(RuntimeError, match="use a new key ID"):
        EnterpriseSettings(environment="production", allowed_origins=["https://console.example.com"])

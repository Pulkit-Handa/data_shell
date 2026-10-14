"""Artifact-verifier path-safety regression tests; full project dependencies are required."""
import pytest

pytest.importorskip("polars")

def test_invalid_certificate_signature_never_dereferences_artifact_paths(monkeypatch):
    from datashell.pipeline.artifacts import ArtifactGenerator
    from datashell.core.crypto import CryptoEngine
    from datashell.core.config import settings

    monkeypatch.setattr(CryptoEngine, "sha256_file", staticmethod(lambda path: pytest.fail("untrusted path was read")))
    cert = {
        "tenant_id": "tenant-untrusted-cert",
        "cryptographic_manifest": {
            "delivered_status": "DELIVERED",
            "delivered_payload_sha256": "0" * 64,
            "delivered_file_path": "/etc/passwd",
            "run_artifact_path": "/etc/passwd",
            "quarantine_artifacts": {"../../etc/passwd": {"sha256": "0" * 64}},
        },
        "digital_signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": settings.trusted_key_ids[0],
            "signature": "invalid-signature",
        },
    }
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is False
    assert result["status"] == "TAMPERED_OR_INVALID_SIGNATURE"
    assert result["artifact_details"] == {}


def test_validly_signed_certificate_cannot_direct_verifier_outside_storage_roots(monkeypatch):
    from datashell.pipeline.artifacts import ArtifactGenerator
    from datashell.core.crypto import CryptoEngine
    from datashell.core.config import settings

    monkeypatch.setattr(CryptoEngine, "sha256_file", staticmethod(lambda path: pytest.fail("unsafe path was read")))
    cert = {
        "tenant_id": "tenant-signed-path-test",
        "cryptographic_manifest": {
            "delivered_status": "DELIVERED",
            "delivered_payload_sha256": "0" * 64,
            "delivered_file_path": "/etc/passwd",
            "run_artifact_path": "/etc/passwd",
            "quarantine_artifacts": {"../../etc/passwd": {"sha256": "0" * 64}},
        },
        "digital_signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": settings.trusted_key_ids[0],
            "signature": "",
        },
    }
    payload = {key: value for key, value in cert.items() if key != "digital_signature"}
    cert["digital_signature"]["signature"] = CryptoEngine.sign_manifest(payload)
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is True
    assert result["status"] == "TAMPERED_UNDERLYING_ARTIFACTS"
    assert result["artifact_details"]["run_lineage_artifact"]["status"] == "UNSAFE_PATH"
    assert result["artifact_details"]["active_delivered_target"]["status"] == "UNSAFE_PATH"
    assert result["artifact_details"]["../../etc/passwd"]["status"] == "UNSAFE_PATH"


def test_previous_signing_key_remains_valid_during_rotation(monkeypatch):
    import secrets
    from datashell.pipeline.artifacts import ArtifactGenerator
    from datashell.core.crypto import CryptoEngine
    from datashell.core.config import settings

    old_secret = secrets.token_hex(32)
    legacy_id = "datashell-hmac-legacy"
    active_id = settings.active_key_id
    assert legacy_id != active_id, "rotation test requires a distinct legacy key id"
    monkeypatch.setattr(settings, "trusted_hmac_keys", {legacy_id: old_secret, active_id: settings.hmac_secret})
    monkeypatch.setattr(settings, "trusted_key_ids", [legacy_id, active_id])
    cert = {
        "tenant_id": "tenant-key-rotation",
        "cryptographic_manifest": {
            "delivered_status": "REJECTED",
            "delivered_payload_sha256": "",
            "delivered_file_path": None,
            "run_artifact_path": None,
            "quarantine_artifacts": {},
        },
        "signature_header": {"algorithm": "HMAC-SHA256", "key_id": legacy_id},
        "digital_signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": legacy_id,
            "signature": "",
        },
    }
    payload = {key: value for key, value in cert.items() if key != "digital_signature"}
    cert["digital_signature"]["signature"] = CryptoEngine.sign_manifest(payload, secret_key=old_secret)
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is True
    assert result["status"] == "AUTHENTIC_AND_UNMODIFIED"


def test_key_id_allowlist_without_secret_does_not_fall_back_to_active_key(monkeypatch):
    from datashell.pipeline.artifacts import ArtifactGenerator
    from datashell.core.config import settings

    monkeypatch.setattr(settings, "trusted_hmac_keys", {settings.active_key_id: settings.hmac_secret})
    monkeypatch.setattr(settings, "trusted_key_ids", [settings.active_key_id, "legacy-id-without-secret"])
    cert = {
        "tenant_id": "tenant-keyring-fallback-test",
        "cryptographic_manifest": {
            "delivered_status": "REJECTED",
            "delivered_payload_sha256": "",
            "delivered_file_path": None,
            "run_artifact_path": None,
            "quarantine_artifacts": {},
        },
        "signature_header": {"algorithm": "HMAC-SHA256", "key_id": "legacy-id-without-secret"},
        "digital_signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": "legacy-id-without-secret",
            "signature": "not-a-valid-signature",
        },
    }
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is False
    assert result["key_trusted"] is False
    assert result["status"] == "UNTRUSTED_KEY_IDENTIFIER"
    assert result["artifact_details"] == {}

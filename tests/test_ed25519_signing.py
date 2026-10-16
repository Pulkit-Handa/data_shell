"""Ed25519 (asymmetric) certificate signing and third-party verification.

The trust model must match the certificate's claims:
  * With an Ed25519 private key provisioned, certificates are asymmetric —
    a third party holding only the public key verifies them and cannot forge.
  * With the HMAC fallback, the certificate states honestly that it is
    symmetric and requires the shared secret.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from datashell.core.config import EnterpriseSettings, settings
from datashell.core.crypto import CryptoEngine
from datashell.contract.models import (
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityBehavior,
    QualitySpec,
    SchemaColumn,
    SourceDefinition,
)
from datashell.engines.delivery import TerminalStatus
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import DataShellPipeline


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")


def _contract(name, source: Path, target: Path) -> DataContract:
    return DataContract(
        metadata={"name": name, "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(
                path=str(source),
                schema=[
                    SchemaColumn(name="id", polars_type="Int64", nullable=False),
                    SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                ],
            )],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW, rules=[]),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(target))]),
        },
    )


def _run_certificate(monkeypatch, tmp_path, name):
    source = tmp_path / f"{name}.parquet"
    pl.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}).write_parquet(source)
    target = tmp_path / f"{name}_out.parquet"
    result = DataShellPipeline(_contract(name, source, target)).run_file(str(source))
    assert result.status == TerminalStatus.LOADED
    return result.audit_certificate


def test_config_derives_and_registers_ed25519_public_key():
    private_pem, public_pem = CryptoEngine.generate_ed25519_keypair()
    configured = EnterpriseSettings(ed25519_private_key=private_pem)
    registered = configured.ed25519_trusted_keys[configured.ed25519_active_key_id]
    # The derived public key must match the generated one.
    assert registered.strip() == public_pem.strip()


def test_ed25519_certificate_verified_by_public_key_only(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    private_pem, public_pem = CryptoEngine.generate_ed25519_keypair()
    monkeypatch.setattr(settings, "ed25519_private_key", private_pem)
    monkeypatch.setattr(settings, "ed25519_active_key_id", "test-ed25519")
    monkeypatch.setattr(settings, "ed25519_trusted_keys", {"test-ed25519": public_pem})

    cert = _run_certificate(monkeypatch, tmp_path, "ed25519-ok")
    assert cert["digital_signature"]["algorithm"] == "Ed25519"
    assert cert["digital_signature"]["integrity_type"] == "ASYMMETRIC_ED25519_SIGNATURE"
    assert cert["digital_signature"]["third_party_verifiable"] is True
    assert cert["digital_signature"]["requires_shared_secret"] is False

    # A third party has ONLY the public key: no private key, no HMAC secret.
    monkeypatch.setattr(settings, "ed25519_private_key", None)
    monkeypatch.setattr(settings, "hmac_secret", "an-unrelated-secret-value")
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is True
    assert result["signature_algorithm"] == "Ed25519"
    assert result["status"] == "AUTHENTIC_AND_UNMODIFIED"


def test_ed25519_tampered_certificate_is_rejected(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    private_pem, public_pem = CryptoEngine.generate_ed25519_keypair()
    monkeypatch.setattr(settings, "ed25519_private_key", private_pem)
    monkeypatch.setattr(settings, "ed25519_active_key_id", "test-ed25519-tamper")
    monkeypatch.setattr(settings, "ed25519_trusted_keys", {"test-ed25519-tamper": public_pem})

    cert = _run_certificate(monkeypatch, tmp_path, "ed25519-tamper")
    cert["reconciliation"]["rows_delivered"] = 999999  # forge a delivered count
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is False
    assert result["status"] == "TAMPERED_OR_INVALID_SIGNATURE"


def test_ed25519_untrusted_key_id_fails_closed(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    private_pem, public_pem = CryptoEngine.generate_ed25519_keypair()
    monkeypatch.setattr(settings, "ed25519_private_key", private_pem)
    monkeypatch.setattr(settings, "ed25519_active_key_id", "known-key")
    monkeypatch.setattr(settings, "ed25519_trusted_keys", {"known-key": public_pem})

    cert = _run_certificate(monkeypatch, tmp_path, "ed25519-untrusted")
    # Attacker rewrites the key id to one the verifier never provisioned.
    cert["digital_signature"]["key_id"] = "attacker-key"
    cert["signature_header"]["key_id"] = "attacker-key"
    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is False
    assert result["status"] == "UNTRUSTED_KEY_IDENTIFIER"


def test_ed25519_previous_key_remains_valid_during_rotation(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    old_priv, old_pub = CryptoEngine.generate_ed25519_keypair()
    new_priv, new_pub = CryptoEngine.generate_ed25519_keypair()

    # Certificates were signed with the old key.
    monkeypatch.setattr(settings, "ed25519_private_key", old_priv)
    monkeypatch.setattr(settings, "ed25519_active_key_id", "ed-old")
    monkeypatch.setattr(settings, "ed25519_trusted_keys", {"ed-old": old_pub})
    cert = _run_certificate(monkeypatch, tmp_path, "ed25519-rotation")

    # Operator rotates to a new active key, retaining the old public key.
    monkeypatch.setattr(settings, "ed25519_private_key", new_priv)
    monkeypatch.setattr(settings, "ed25519_active_key_id", "ed-new")
    monkeypatch.setattr(settings, "ed25519_trusted_keys", {"ed-old": old_pub, "ed-new": new_pub})

    result = ArtifactGenerator.verify_certificate_and_artifacts(cert)
    assert result["signature_valid"] is True
    assert result["status"] == "AUTHENTIC_AND_UNMODIFIED"


def test_hmac_fallback_claims_are_honest(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "ed25519_private_key", None)
    cert = _run_certificate(monkeypatch, tmp_path, "hmac-honest")
    assert cert["digital_signature"]["algorithm"] == "HMAC-SHA256"
    # Symmetric: a verifier needs the shared secret and cannot verify "third party".
    assert cert["digital_signature"]["third_party_verifiable"] is False
    assert cert["digital_signature"]["requires_shared_secret"] is True

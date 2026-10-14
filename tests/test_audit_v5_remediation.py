"""Exhaustive test suite verifying remediation of all Data Shell v5 audit findings.

Covers:
1. P0: Production configuration case-insensitivity & aliases (Production, PRODUCTION, prod, PROD, live)
       ensuring auth bypass and missing secrets are rejected closed under all spellings.
2. P1: Tenant-isolated delivery roots: contracts with identical relative paths land in distinct
       tenant namespaces; cross-tenant target tampering and production tempdir delivery are rejected.
3. P1: Full parity on QualityBehavior.WARN across run_file, run_streaming, and run_records:
       failing batch rules record warnings and permit delivery across all modes.
4. P1: Independent run-lineage artifact verification: a deleted run artifact is not masked by active target.
5. P1: Trusted key identifier registry and signature header binding to detect key-id tampering.
6. P1: Quarantine resolution across tenant-isolated and custom quarantine directories.
7. P2: ACTIVE_RUNS gauge reliability when path validation fails before streaming execution.
"""

import os
import shutil
import tempfile
import pytest
from pathlib import Path
import polars as pl
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from datashell.core.config import (
    _resolve_secret, normalize_environment, is_production,
    EnterpriseSettings, settings
)
from datashell.core.security import (
    authenticate_client, APIKeyMetadata, Role, Capability,
    register_api_key, API_KEY_STORE
)
from datashell.core.sandbox import FilesystemSandbox, PathSecurityViolation
from datashell.contract.models import (
    DataContract, SourceDefinition, SchemaColumn, DeliverySpec, DeliveryTarget,
    QualitySpec, QualityRule, BatchRule, QualityBehavior, RuleCheckType, BatchRuleCheckType
)
from datashell.pipeline.runner import DataShellPipeline, ACTIVE_RUNS
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.engines.delivery import TerminalStatus
from datashell.server.app import app


# --------------------------------------------------------------------------
# 1. P0: Production Case-Insensitive Validation & Alias Handling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prod_spelling", ["Production", "PRODUCTION", "production", "prod", "PROD", "live", "LIVE"])
def test_production_mode_refuses_disabled_auth_under_all_spellings(monkeypatch, prod_spelling):
    """Refuses to start with auth_enabled=False under any case variation or alias of production."""
    monkeypatch.setenv("ENVIRONMENT", prod_spelling)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DATASHELL_API_KEY", "a8F3_kL90-qX17_pWz92!Mv44_rT88*bQ66")
    monkeypatch.setenv("DATASHELL_HMAC_SECRET", "b9G4_mL01-rY28_qXa03@Nw55_sU99#cR77")

    with pytest.raises(RuntimeError, match="Production security violation.*AUTH_ENABLED cannot be False"):
        EnterpriseSettings(environment=prod_spelling, auth_enabled=False)


@pytest.mark.parametrize("prod_spelling", ["Production", "PRODUCTION", "production", "prod", "PROD"])
def test_production_mode_refuses_missing_secrets_under_all_spellings(monkeypatch, prod_spelling):
    """Refuses to generate fallback ephemeral credentials under any case variation of production."""
    monkeypatch.setenv("TEST_KEY", "")
    with pytest.raises(RuntimeError, match="Production security violation.*Missing production credential"):
        _resolve_secret("TEST_KEY", "Test Key", env_mode=prod_spelling)


def test_authenticate_client_fails_closed_in_production_aliases(monkeypatch):
    """authenticate_client raises HTTP 500 if auth_enabled is false in any production alias."""
    for prod_alias in ["Production", "PRODUCTION", "prod", "PROD"]:
        monkeypatch.setattr(settings, "environment", prod_alias)
        monkeypatch.setattr(settings, "auth_enabled", False)

        from fastapi import HTTPException
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/api/v1/enforce", "headers": []}
        req = Request(scope)

        with pytest.raises(HTTPException) as exc_info:
            authenticate_client(req, api_key=None)
        assert exc_info.value.status_code == 500
        assert "Authentication bypass is forbidden" in exc_info.value.detail


# --------------------------------------------------------------------------
# 2. P1: Tenant-Isolated Delivery Roots & Cross-Tenant Boundary Enforcement
# --------------------------------------------------------------------------

def test_tenant_isolated_relative_delivery_targets(tmp_path, monkeypatch):
    """Two tenants with identical relative targets must write to separate directories."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")

    # Tenant A and Tenant B specify the exact same relative delivery target
    target_rel = "./output/curated/settlements.parquet"

    path_a = FilesystemSandbox.sanitize_and_validate_path(
        target_rel, is_delivery_target=True, tenant_id="tenant-alpha"
    )
    path_b = FilesystemSandbox.sanitize_and_validate_path(
        target_rel, is_delivery_target=True, tenant_id="tenant-beta"
    )

    assert path_a != path_b
    assert "tenants/tenant-alpha" in str(path_a)
    assert "tenants/tenant-beta" in str(path_b)
    assert path_a.name == "settlements.parquet"
    assert path_b.name == "settlements.parquet"


def test_cross_tenant_target_tampering_is_rejected(tmp_path, monkeypatch):
    """A tenant attempting to deliver into another tenant's namespace must be blocked."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")

    # Tenant Beta attempts to deliver into Tenant Alpha's directory
    malicious_target = tmp_path / "output" / "tenants" / "tenant-alpha" / "hacked.parquet"

    with pytest.raises(PathSecurityViolation, match="Cross-tenant storage boundary violation"):
        FilesystemSandbox.sanitize_and_validate_path(
            malicious_target, is_delivery_target=True, tenant_id="tenant-beta"
        )


def test_production_rejects_temporary_directory_delivery(monkeypatch):
    """In production mode, system temp directory is forbidden as a delivery target."""
    monkeypatch.setattr(settings, "environment", "production")
    temp_target = Path(tempfile.gettempdir()) / "unauthorized_prod_delivery.parquet"

    with pytest.raises(PathSecurityViolation, match="system temporary directory.*not permitted for production"):
        FilesystemSandbox.sanitize_and_validate_path(
            temp_target, is_delivery_target=True, tenant_id="tenant-corp"
        )


def test_delivery_target_cannot_overwrite_reserved_artifact_names(tmp_path, monkeypatch):
    """Delivery targets cannot impersonate engine staged files, quarantine, or audit artifacts."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")

    reserved_names = [
        ".staged_run_123_delivered.parquet",
        "dm_run_abc123_data.parquet",
        "run1_held_records.parquet",
        "run1_structural_rejections.jsonl",
        "run1_audit.json",
        "run1_lineage_data.parquet"
    ]

    for r_name in reserved_names:
        with pytest.raises(PathSecurityViolation, match="conflicts with reserved engine artifact naming pattern"):
            FilesystemSandbox.sanitize_and_validate_path(
                f"./output/{r_name}", is_delivery_target=True, tenant_id="tenant-x"
            )


# --------------------------------------------------------------------------
# 3. P1: QualityBehavior.WARN Parity Across Streaming, File, and Records
# --------------------------------------------------------------------------

def test_warn_quality_behavior_parity_between_file_streaming_and_records(tmp_path, monkeypatch):
    """A failing batch rule in WARN mode must deliver rows and return LOADED across all 3 runners."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "quarantine")

    records = [{"id": i, "val": f"v_{i}"} for i in range(5)]
    source_parquet = tmp_path / "source_warn.parquet"
    pl.DataFrame(records).write_parquet(source_parquet)

    contract = DataContract(
        metadata={"name": "warn-parity-test", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(source_parquet),
                    schema=[
                        SchemaColumn(name="id", polars_type="Int64"),
                        SchemaColumn(name="val", polars_type="String")
                    ]
                )
            ],
            "quality": QualitySpec(
                behavior=QualityBehavior.WARN,
                batch_rules=[
                    BatchRule(
                        rule_id="min-rows-expected",
                        check=BatchRuleCheckType.MIN_ROW_COUNT,
                        value=100
                    )
                ]
            ),
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path="./output/warn_test.parquet")]
            )
        }
    )

    # 1. run_file
    pipeline_file = DataShellPipeline(contract, tenant_id="tenant-warn")
    res_file = pipeline_file.run_file(source_parquet)

    # 2. run_streaming
    pipeline_stream = DataShellPipeline(contract, tenant_id="tenant-warn")
    res_stream = pipeline_stream.run_streaming(source_parquet, chunk_size=2)

    # 3. run_records
    pipeline_records = DataShellPipeline(contract, tenant_id="tenant-warn")
    res_records = pipeline_records.run_records(records)

    # All three must agree: LOADED, 5 rows delivered, batch rule evaluated to False
    for res, mode in [(res_file, "file"), (res_stream, "streaming"), (res_records, "records")]:
        assert res.status == TerminalStatus.LOADED, f"Mode {mode} failed with status {res.status}"
        assert res.outcome.rows_delivered == 5, f"Mode {mode} delivered {res.outcome.rows_delivered} rows"
        assert res.ledger.footing_matched is True
        # Batch rule check failed
        assert len(res.audit_certificate["batch_evaluations"]) == 1
        assert res.audit_certificate["batch_evaluations"][0]["passed"] is False


# --------------------------------------------------------------------------
# 4. P1: Independent Lineage Artifact Verification (No Masking)
# --------------------------------------------------------------------------

def test_missing_run_lineage_artifact_is_not_masked_by_active_target(tmp_path, monkeypatch):
    """If the immutable run lineage artifact is deleted, verifier reports MISSING, even if active target exists."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "audits")

    contract = DataContract(
        metadata={"name": "lineage-mask-test", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="memory",
                    schema=[SchemaColumn(name="id", polars_type="Int64")]
                )
            ],
            "delivery": DeliverySpec(targets=[DeliveryTarget(path="./output/lineage.parquet")])
        }
    )

    pipeline = DataShellPipeline(contract, tenant_id="tenant-lineage")
    res = pipeline.run_records([{"id": 1}, {"id": 2}])
    assert res.status == TerminalStatus.LOADED

    # Verify initially authentic
    verify_initial = ArtifactGenerator.verify_certificate_and_artifacts(res.audit_certificate)
    assert verify_initial["status"] == "AUTHENTIC_AND_UNMODIFIED"

    # Now simulate deletion of the immutable run-specific lineage artifact:
    run_art_path = Path(res.audit_certificate["cryptographic_manifest"]["run_artifact_path"])
    assert run_art_path.exists()
    run_art_path.unlink()

    # Active target file remains intact!
    active_target = Path(res.audit_certificate["cryptographic_manifest"]["delivered_file_path"])
    assert active_target.exists()

    # Verifier MUST detect that the run lineage artifact is missing!
    verify_after = ArtifactGenerator.verify_certificate_and_artifacts(res.audit_certificate)
    assert verify_after["status"] == "MISSING_UNDERLYING_ARTIFACTS"
    assert verify_after["artifact_details"]["run_lineage_artifact"]["status"] == "MISSING"


# --------------------------------------------------------------------------
# 5. P1: Key Identifier Registry & Signature Header Tamper Detection
# --------------------------------------------------------------------------

def test_untrusted_or_altered_key_id_is_rejected(tmp_path, monkeypatch):
    """Altering the key_id or using an untrusted key invalidates certificate verification."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "audits")

    contract = DataContract(
        metadata={"name": "key-trust-test", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="memory",
                    schema=[SchemaColumn(name="id", polars_type="Int64")]
                )
            ]
        }
    )

    pipeline = DataShellPipeline(contract, tenant_id="tenant-keys")
    res = pipeline.run_records([{"id": 1}])
    cert = res.audit_certificate

    # Case A: Untrusted key_id in digital signature
    cert_tampered = dict(cert)
    cert_tampered["digital_signature"] = dict(cert["digital_signature"])
    cert_tampered["digital_signature"]["key_id"] = "unknown-untrusted-key-id"

    verification = ArtifactGenerator.verify_certificate_and_artifacts(cert_tampered)
    assert verification["status"] in ("UNTRUSTED_KEY_IDENTIFIER", "TAMPERED_OR_INVALID_SIGNATURE")
    assert verification["signature_valid"] is False

    # Case B: Header key_id modified
    cert_tampered2 = dict(cert)
    cert_tampered2["signature_header"] = dict(cert["signature_header"])
    cert_tampered2["signature_header"]["key_id"] = "attacker-forged-key"
    verification2 = ArtifactGenerator.verify_certificate_and_artifacts(cert_tampered2)
    assert verification2["signature_valid"] is False


# --------------------------------------------------------------------------
# 6. P1: Tenant-Isolated Quarantine Verification
# --------------------------------------------------------------------------

def test_tenant_isolated_quarantine_verification(tmp_path, monkeypatch):
    """Quarantine files in tenant directories are verified accurately."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "quarantine")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "audits")

    contract = DataContract(
        metadata={"name": "tenant-quarantine-test", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="memory",
                    schema=[SchemaColumn(name="amount", polars_type="Float64")]
                )
            ],
            "quality": QualitySpec(
                behavior=QualityBehavior.DROP_ROW,
                rules=[
                    QualityRule(
                        rule_id="pos-amt",
                        column="amount",
                        check=RuleCheckType.GREATER_THAN,
                        value=0.0
                    )
                ]
            )
        }
    )

    pipeline = DataShellPipeline(contract, tenant_id="tenant-fin")
    res = pipeline.run_records([
        {"amount": 50.0},
        {"amount": -5.0}  # Violates pos-amt, will be held in quarantine
    ])

    assert res.status == TerminalStatus.LOADED
    assert res.outcome.rows_delivered == 1
    assert len(res.outcome.quarantine_files) == 1
    assert "tenants/tenant-fin" in res.outcome.quarantine_files[0]

    # Verification must locate the tenant quarantine file and confirm it
    verification = ArtifactGenerator.verify_certificate_and_artifacts(res.audit_certificate)
    assert verification["status"] == "AUTHENTIC_AND_UNMODIFIED"
    assert verification["artifacts_verified"] is True


# --------------------------------------------------------------------------
# 7. P2: ACTIVE_RUNS Reliability When Path Sanitization Fails Early
# --------------------------------------------------------------------------

def test_active_runs_metric_decrements_when_path_sanitization_fails(tmp_path, monkeypatch):
    """ACTIVE_RUNS gauge must decrement even if path validation fails before streaming starts."""
    initial_gauge = ACTIVE_RUNS._value.get()

    contract = DataContract(
        metadata={"name": "failing-path-test", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="memory",
                    schema=[SchemaColumn(name="id", polars_type="Int64")]
                )
            ],
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path="./output/valid_target.parquet")]
            )
        }
    )

    pipeline = DataShellPipeline(contract, tenant_id="tenant-x")

    with pytest.raises(PathSecurityViolation):
        pipeline.run_streaming(
            "/fake/source.parquet",
            target_path_override="/etc/forbidden/path.parquet"
        )

    # The metric must not remain elevated!
    after_gauge = ACTIVE_RUNS._value.get()
    assert after_gauge == initial_gauge

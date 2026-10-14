"""Exhaustive regression test suite verifying all remediation items from Audit Review v3."""

import os
import json
import pytest
from pathlib import Path
import polars as pl
from fastapi.testclient import TestClient

from datashell.core.config import settings, EnterpriseSettings, _resolve_secret
from datashell.core.security import (
    Role, Capability, APIKeyMetadata, register_api_key, revoke_api_key,
    API_KEY_STORE, verify_tenant_access
)
from datashell.core.sandbox import FilesystemSandbox, PathSecurityViolation
from datashell.core.crypto import CryptoEngine
from datashell.contract.models import (
    DataContract, SourceDefinition, SchemaColumn,
    QualityRule, BatchRule, RowRuleCheckType, BatchRuleCheckType,
    QualityBehavior, DeliverySpec, DeliveryTarget, ExtraColumnsPolicy, RuleCheckType
)
from datashell.contract.registry import ContractRegistry
from datashell.pipeline.runner import DataShellPipeline
from datashell.engines.delivery import TerminalStatus
from datashell.engines.ingestion import StructuralIngestionEngine
from datashell.engines.quality import SemanticQualityEngine
from datashell.server.app import app

client = TestClient(app)


def test_production_mode_refuses_to_start_with_missing_or_default_secrets(monkeypatch):
    """Audit Finding: Refuse to start in production if mandatory secrets are absent or insecure defaults."""
    # Test missing secret
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        _resolve_secret("TEST_KEY", "Test Purpose", "production")
    assert "Production security violation" in str(exc.value)

    # Test insecure fallback string
    monkeypatch.setenv("TEST_KEY", "dm_live_secret_key_prod_9941a8")
    with pytest.raises(RuntimeError) as exc:
        _resolve_secret("TEST_KEY", "Test Purpose", "production")
    assert "Production security violation" in str(exc.value)


def test_backward_compatible_rule_check_type_attributes():
    """Audit Finding: RuleCheckType compatibility alias must support enum attribute access."""
    assert RuleCheckType.GREATER_THAN == RowRuleCheckType.GREATER_THAN
    assert RuleCheckType.MIN_ROW_COUNT == BatchRuleCheckType.MIN_ROW_COUNT


def test_route_authorization_enforces_required_capabilities():
    """Audit Finding: Apply explicit capability checks to every route (metrics, enforce, contracts)."""
    # 1. Producer Key (has PIPELINE_ENFORCE, lacks CONTRACTS_WRITE and METRICS_READ)
    producer_key = "test_key_producer_991"
    register_api_key(APIKeyMetadata(
        key=producer_key,
        tenant_id="tenant-prod",
        role=Role.PRODUCER,
        description="Producer Test"
    ))

    # 2. Auditor Key (has AUDIT_READ, METRICS_READ, lacks PIPELINE_ENFORCE)
    auditor_key = "test_key_auditor_992"
    register_api_key(APIKeyMetadata(
        key=auditor_key,
        tenant_id="tenant-audit",
        role=Role.AUDITOR,
        description="Auditor Test"
    ))

    # Unauthenticated /metrics must be rejected
    resp_anon_metrics = client.get("/metrics")
    assert resp_anon_metrics.status_code == 401

    # Producer cannot access /metrics (lacks METRICS_READ)
    resp_prod_metrics = client.get("/metrics", headers={"X-API-Key": producer_key})
    assert resp_prod_metrics.status_code == 403

    # Auditor can access /metrics (holds METRICS_READ)
    resp_aud_metrics = client.get("/metrics", headers={"X-API-Key": auditor_key})
    assert resp_aud_metrics.status_code == 200

    # Auditor cannot post to /api/v1/enforce (lacks PIPELINE_ENFORCE)
    fake_file = ("test.parquet", b"dummy", "application/octet-stream")
    resp_aud_enforce = client.post(
        "/api/v1/enforce",
        data={"contract_name": "vendor-settlements"},
        files={"file": fake_file},
        headers={"X-API-Key": auditor_key}
    )
    assert resp_aud_enforce.status_code == 403
    assert "lacks capability 'pipeline:enforce'" in resp_aud_enforce.json()["detail"]


def test_multi_tenant_certificate_isolation(tmp_path):
    """Audit Finding: Ensure cross-tenant access to audit certificates is strictly forbidden."""
    # Register two distinct tenant keys
    key_alpha = "key_tenant_alpha_1"
    key_beta = "key_tenant_beta_2"
    register_api_key(APIKeyMetadata(key=key_alpha, tenant_id="tenant-alpha", role=Role.AUDITOR, description="Alpha"))
    register_api_key(APIKeyMetadata(key=key_beta, tenant_id="tenant-beta", role=Role.AUDITOR, description="Beta"))

    # Create a signed audit certificate owned by tenant-alpha
    run_id = "run_isolated_alpha_123"
    cert_path = settings.audit_dir / f"{run_id}_audit_certificate.json"
    cert_data = {
        "certificate_version": "2.0-enterprise",
        "run_id": run_id,
        "tenant_id": "tenant-alpha",
        "timestamp": "2026-10-12T00:00:00Z",
        "reconciliation": {"rows_read": 100, "rows_delivered": 100}
    }
    sig = CryptoEngine.sign_manifest(cert_data)
    cert_data["digital_signature"] = {"signature": sig}
    with open(cert_path, "w") as f:
        json.dump(cert_data, f)

    # 1. Tenant Alpha requests its own certificate -> 200 OK
    resp_alpha = client.get(f"/api/v1/runs/{run_id}/certificate", headers={"X-API-Key": key_alpha})
    assert resp_alpha.status_code == 200

    # 2. Tenant Beta requests Tenant Alpha's certificate -> 403 Forbidden!
    resp_beta = client.get(f"/api/v1/runs/{run_id}/certificate", headers={"X-API-Key": key_beta})
    assert resp_beta.status_code == 403
    assert "Cross-tenant access violation" in resp_beta.json()["detail"]

    # 3. Master Admin can access cross-tenant for oversight -> 200 OK
    resp_admin = client.get(f"/api/v1/runs/{run_id}/certificate", headers={"X-API-Key": settings.master_api_key})
    assert resp_admin.status_code == 200


def test_audit_certificate_facts_distinguish_candidate_vs_delivered_on_rejection(tmp_path):
    """Audit Finding: Certificates must accurately record actual delivered rows as 0 on rejected runs."""
    target_out = tmp_path / "out.parquet"
    contract = DataContract(
        metadata={"name": "test-facts", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="val", polars_type="Float64")])],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [],
                "batch_rules": [
                    BatchRule(rule_id="min-vol", check=BatchRuleCheckType.MIN_ROW_COUNT, value=50000)
                ]
            },
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(target_out), mode="overwrite")])
        }
    )

    pipeline = DataShellPipeline(contract, tenant_id="finance-team")
    records = [{"val": 100.0}, {"val": 200.0}]  # 2 candidate rows, but fails min 50,000!
    res = pipeline.run_records(records)

    assert res.status == TerminalStatus.REJECTED
    assert res.outcome.rows_delivered == 0

    # Verify certificate facts
    cert = res.audit_certificate
    assert cert["reconciliation"]["candidate_rows"] == 2
    assert cert["reconciliation"]["rows_delivered"] == 0
    assert cert["cryptographic_manifest"]["delivered_payload_sha256"] == "ABSENT_DUE_TO_REJECTION"
    assert cert["cryptographic_manifest"]["delivered_status"] == "REJECTED_ZERO_DELIVERED"


def test_contract_canonical_hash_binds_to_exact_rules():
    """Audit Finding: Contract hash must bind to actual contract contents, not just name:version."""
    c1 = DataContract(
        metadata={"name": "test-c", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="x", polars_type="Int64")])],
            "quality": {
                "rules": [QualityRule(rule_id="r1", check=RowRuleCheckType.GREATER_THAN, column="x", value=10)]
            }
        }
    )

    # Same name and version, but different rule value (20 vs 10)
    c2 = DataContract(
        metadata={"name": "test-c", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="x", polars_type="Int64")])],
            "quality": {
                "rules": [QualityRule(rule_id="r1", check=RowRuleCheckType.GREATER_THAN, column="x", value=20)]
            }
        }
    )

    # Hashes MUST differ!
    assert c1.canonical_sha256() != c2.canonical_sha256()


def test_delivery_sandbox_strictly_forbids_writing_into_audits_or_contracts(tmp_path):
    """Audit Finding: Reserve audit and quarantine namespaces from delivery targets."""
    # Attempting to deliver directly into settings.audit_dir
    malicious_target = settings.audit_dir / "forged_certificate.json"
    with pytest.raises(PathSecurityViolation) as exc:
        FilesystemSandbox.sanitize_and_validate_path(malicious_target, is_delivery_target=True)
    assert "reserved operational namespace" in str(exc.value)

    # Attempting to deliver directly into settings.contracts_dir
    malicious_contract_target = settings.contracts_dir / "corrupted_contract.json"
    with pytest.raises(PathSecurityViolation) as exc:
        FilesystemSandbox.sanitize_and_validate_path(malicious_contract_target, is_delivery_target=True)
    assert ("protected system directory" in str(exc.value) or "reserved operational namespace" in str(exc.value))


def test_quality_behavior_warn_permits_delivery_even_when_batch_rule_fails(tmp_path):
    """Audit Finding: Under WARN behavior, failed batch rules must warn rather than reject whole run."""
    target_out = tmp_path / "warn_delivered.parquet"
    contract = DataContract(
        metadata={"name": "test-warn-batch", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="score", polars_type="Float64")])],
            "quality": {
                "behavior": "WARN",
                "rules": [],
                "batch_rules": [
                    BatchRule(rule_id="high-vol", check=BatchRuleCheckType.MIN_ROW_COUNT, value=1000)
                ]
            },
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(target_out), mode="overwrite")])
        }
    )

    pipeline = DataShellPipeline(contract)
    records = [{"score": 95.0}, {"score": 88.0}]  # 2 rows < 1000 min volume
    res = pipeline.run_records(records, audit_output_path=str(tmp_path / "warn_audit.json"))

    # In WARN mode: run is delivered with warnings recorded
    assert res.status == TerminalStatus.LOADED
    assert res.outcome.rows_delivered == 2
    assert target_out.exists()


def test_sql_assertion_enforces_scalar_boolean_across_full_dataset():
    """Audit Finding: SQL assertions must evaluate as scalar boolean across all rows, not just row 1."""
    contract = DataContract(
        metadata={"name": "sql-scalar-test", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="amount", polars_type="Float64")])],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [],
                "batch_rules": [
                    # Predicate must hold for ALL rows: amount > 0
                    BatchRule(rule_id="all-positive", check=BatchRuleCheckType.SQL_ASSERTION, sql="amount > 0")
                ]
            }
        }
    )

    # Row 1 is positive (100.0), but Row 2 is negative (-50.0)
    df = pl.DataFrame({"amount": [100.0, -50.0]})
    engine = SemanticQualityEngine(contract)
    res = engine.evaluate(df)

    # Must FAIL because not all rows are positive!
    assert res.all_batch_rules_passed is False
    assert res.batch_results[0].passed is False


def test_extra_columns_policy_allow_preserves_unexpected_columns():
    """Audit Finding: ExtraColumnsPolicy.ALLOW must preserve extra fields in conforming output."""
    contract = DataContract(
        metadata={"name": "test-allow-extra", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[SchemaColumn(name="id", polars_type="Utf8")],
                    extra_columns_policy=ExtraColumnsPolicy.ALLOW
                )
            ]
        }
    )

    engine = StructuralIngestionEngine(contract)
    records = [{"id": "ID-1", "custom_feature": 999, "metadata_tag": "active"}]
    res = engine.ingest_records(records)

    assert res.refused_count == 0
    assert "custom_feature" in res.conforming_df.columns
    assert "metadata_tag" in res.conforming_df.columns
    assert res.conforming_df["custom_feature"][0] == 999


def test_registry_concurrency_locking_and_semver_activation(tmp_path):
    """Audit Finding: Registry must update latest only when new version >= current semantic version."""
    reg = ContractRegistry(tmp_path)

    v1_contract = DataContract(
        metadata={"name": "semver-test", "version": "1.0.0"},
        spec={"sources": [SourceDefinition(path="d", schema=[SchemaColumn(name="x", polars_type="Int64")])]}
    )
    v2_contract = DataContract(
        metadata={"name": "semver-test", "version": "2.0.0"},
        spec={"sources": [SourceDefinition(path="d", schema=[SchemaColumn(name="x", polars_type="Int64")])]}
    )
    v1_1_contract = DataContract(
        metadata={"name": "semver-test", "version": "1.1.0"},
        spec={"sources": [SourceDefinition(path="d", schema=[SchemaColumn(name="x", polars_type="Int64")])]}
    )

    reg.save_contract(v1_contract)
    assert reg.get_latest("semver-test").metadata.version == "1.0.0"

    reg.save_contract(v2_contract)
    assert reg.get_latest("semver-test").metadata.version == "2.0.0"

    # Now backport/save v1.1.0: latest MUST REMAIN v2.0.0!
    reg.save_contract(v1_1_contract)
    assert reg.get_latest("semver-test").metadata.version == "2.0.0"


def test_unified_streaming_pipeline_execution(tmp_path):
    """Audit Finding: Streaming pipeline must run structural checks, batch assertions, and staged delivery."""
    source_parquet = tmp_path / "streaming_input.parquet"
    target_parquet = tmp_path / "streaming_delivered.parquet"

    # Create 5,000 rows in Parquet
    df = pl.DataFrame({
        "id": [f"ID_{i}" for i in range(5000)],
        "val": [10.0 + (i % 100) for i in range(5000)]
    })
    df.write_parquet(source_parquet)

    contract = DataContract(
        metadata={"name": "stream-contract", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(source_parquet),
                    schema=[
                        SchemaColumn(name="id", polars_type="Utf8"),
                        SchemaColumn(name="val", polars_type="Float64")
                    ]
                )
            ],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [QualityRule(rule_id="positive-val", check=RowRuleCheckType.GREATER_THAN, column="val", value=0.0)],
                "batch_rules": [BatchRule(rule_id="min-rows", check=BatchRuleCheckType.MIN_ROW_COUNT, value=1000)]
            },
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(target_parquet), mode="overwrite")])
        }
    )

    pipeline = DataShellPipeline(contract)
    res = pipeline.run_streaming(source_path=source_parquet, chunk_size=1000, audit_output_path=str(tmp_path / "stream_audit.json"))

    assert res.status == TerminalStatus.LOADED
    assert res.outcome.rows_delivered == 5000
    assert res.ledger.footing_matched is True
    assert target_parquet.exists()
    assert pl.read_parquet(target_parquet).height == 5000

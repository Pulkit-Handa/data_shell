"""Two-tenant isolation negative tests across all API/CLI/artifact/ledger paths.

Verifies that cross-tenant access is denied and produces no sensitive disclosure.
"""
from __future__ import annotations

import json
import pytest

pytest.importorskip("polars")

from datashell.core.security import (
    verify_tenant_access,
    Capability,
    Role,
    APIKeyMetadata,
)
from datashell.core.ledger import BoundaryLedger
from fastapi import HTTPException


class TestTenantAccessControl:
    """verify_tenant_access denies cross-tenant access for non-admin callers."""

    def test_same_tenant_allowed(self):
        """A tenant can access its own resources."""
        metadata = APIKeyMetadata(
            key="test-key",
            tenant_id="tenant-a",
            role=Role.PRODUCER,
            description="Test key",
        )
        # Should not raise
        verify_tenant_access("tenant-a", metadata)

    def test_cross_tenant_denied(self):
        """A non-admin tenant cannot access another tenant's resources."""
        metadata = APIKeyMetadata(
            key="test-key",
            tenant_id="tenant-a",
            role=Role.PRODUCER,
            description="Test key",
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_tenant_access("tenant-b", metadata)
        assert exc_info.value.status_code == 403

    def test_admin_can_access_any_tenant(self):
        """An admin with ADMIN_MANAGE can access any tenant's resources."""
        metadata = APIKeyMetadata(
            key="admin-key",
            tenant_id="admin-tenant",
            role=Role.ADMIN,
            description="Admin key",
        )
        # Should not raise
        verify_tenant_access("tenant-b", metadata)


class TestLedgerTenantIsolation:
    """Ledger queries are tenant-scoped by construction."""

    def test_cross_tenant_run_lookup_returns_none(self, tmp_path):
        """A run recorded by tenant-a is invisible to tenant-b."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        run_id = "dm_run_tenant001"
        ledger.record_run(
            run_id=run_id,
            contract_name="test-contract",
            rows_read=100,
            rows_delivered=90,
            metrics={"null_rate": 0.1},
            tenant_id="tenant-a",
            status="LOADED",
        )

        # Tenant B should not see tenant A's run
        result = ledger.get_run(run_id, "tenant-b")
        assert result is None

    def test_cross_tenant_list_runs_empty(self, tmp_path):
        """Tenant B's run list is empty when only tenant A has runs."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        ledger.record_run(
            run_id="dm_run_tenant002",
            contract_name="test-contract",
            rows_read=100,
            rows_delivered=90,
            metrics={},
            tenant_id="tenant-a",
            status="LOADED",
        )

        runs_b = ledger.list_runs("tenant-b")
        assert len(runs_b) == 0

    def test_cross_tenant_quarantine_isolated(self, tmp_path):
        """Quarantine records are tenant-scoped."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        ledger.record_quarantine(
            run_id="dm_run_tenant003",
            tenant_id="tenant-a",
            contract_name="test-contract",
            kind="held_quality",
            artifact_path="/tmp/tenant-a/held.parquet",
            row_count=5,
            reasons={"null_values": 5},
        )

        # Tenant B should not see tenant A's quarantine records
        quarantine_b = ledger.list_quarantine("tenant-b")
        assert len(quarantine_b) == 0

        # Tenant A should see its own
        quarantine_a = ledger.list_quarantine("tenant-a")
        assert len(quarantine_a) == 1

    def test_cross_tenant_usage_isolated(self, tmp_path):
        """Usage records are tenant-scoped."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        ledger.record_usage(
            run_id="dm_run_tenant004",
            tenant_id="tenant-a",
            contract_name="test-contract",
            status="LOADED",
            execution_mode="in_memory",
            rows_read=100,
            rows_delivered=90,
        )

        summary_b = ledger.usage_summary("tenant-b")
        assert summary_b["total_runs"] == 0

        summary_a = ledger.usage_summary("tenant-a")
        assert summary_a["total_runs"] == 1


class TestTenantStorageIsolation:
    """Tenant storage paths are isolated."""

    def test_tenant_storage_keys_are_distinct(self):
        """Different tenants get different storage keys."""
        from datashell.core.config import EnterpriseSettings

        settings = EnterpriseSettings()
        key_a = settings.tenant_storage_key("tenant-a")
        key_b = settings.tenant_storage_key("tenant-b")
        assert key_a != key_b

    def test_tenant_storage_key_rejects_path_traversal(self):
        """Tenant storage keys cannot contain path traversal characters."""
        from datashell.core.config import EnterpriseSettings

        settings = EnterpriseSettings()
        # Path traversal input should be hex-encoded, not raise
        key = settings.tenant_storage_key("../etc/passwd")
        # The encoded key should not contain path separators
        assert "/" not in key
        assert "\\" not in key
        # The encoded key should start with ~ (hex encoding marker)
        assert key.startswith("~")

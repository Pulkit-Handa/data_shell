"""Regression tests for tenant-safe quarantine path handling.

The quarantine directory is derived from contract input and then created with
``mkdir(parents=True)``.  Before this hardening a contract could point it at
another tenant's namespace, or at an operational SQLite sidecar such as
``datashell_history.db-wal``.  Creating a directory at a sidecar path
permanently breaks the ledger/credential databases, so these tests pin both
the database-name refusal and the tenant boundary.
"""

from __future__ import annotations

import pytest

pytest.importorskip("polars")

import polars as pl

from datashell.contract.models import DataContract
from datashell.core.config import settings
from datashell.core.sandbox import FilesystemSandbox, PathSecurityViolation
from datashell.pipeline.runner import DataShellPipeline


def _isolate_storage(monkeypatch, tmp_path) -> None:
    """Point every operational root at the test's private workspace."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "audit")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "quarantine")


def _contract(source_path, target_path, quarantine_path):
    return DataContract(
        metadata={"name": "quarantine-sandbox", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(source_path),
                "schema": [{"name": "val", "polars_type": "Float64", "nullable": False}],
            }],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [{
                    "rule_id": "val-positive",
                    "check": "GREATER_THAN",
                    "column": "val",
                    "value": 0,
                }],
            },
            "delivery": {
                "targets": [{
                    "type": "PARQUET",
                    "connectionRef": "local",
                    "path": str(target_path),
                    "mode": "overwrite",
                }],
                "quarantinePath": str(quarantine_path),
            },
        },
    )


@pytest.mark.parametrize(
    "filename",
    [
        "datashell_history.db-wal",
        "datashell_history.db-shm",
        "datashell_history.db-journal",
        "security_registry.sqlite3-wal",
        "security_registry.sqlite3-shm",
        "quarantine.lock",
    ],
)
def test_quarantine_path_cannot_create_a_database_sidecar_directory(
    tmp_path, monkeypatch, filename
):
    _isolate_storage(monkeypatch, tmp_path)
    candidate = settings.get_tenant_quarantine_dir("tenant-a") / filename

    with pytest.raises(PathSecurityViolation, match="operational database or lock file"):
        FilesystemSandbox.sanitize_and_validate_path(
            candidate, tenant_id="tenant-a", is_quarantine_path=True
        )

    assert not candidate.exists()


@pytest.mark.parametrize(
    "filename",
    [
        "datashell_history.db-wal",
        "security_registry.sqlite3-shm",
        "ledger.db-journal",
    ],
)
def test_delivery_target_cannot_write_to_a_database_sidecar(tmp_path, monkeypatch, filename):
    _isolate_storage(monkeypatch, tmp_path)
    candidate = settings.output_dir / filename

    with pytest.raises(PathSecurityViolation, match="operational database or lock file"):
        FilesystemSandbox.sanitize_and_validate_path(
            candidate, tenant_id="tenant-a", is_delivery_target=True
        )

    assert not candidate.exists()


def test_contract_rejects_database_sidecar_quarantine_path(tmp_path, monkeypatch):
    """A contract cannot even register a quarantine path that names a sidecar."""
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    pl.DataFrame({"val": [1.0]}).write_parquet(source)
    evil = settings.get_tenant_quarantine_dir("tenant-a") / "datashell_history.db-wal"

    with pytest.raises(PathSecurityViolation, match="operational database or lock file"):
        _contract(source, tmp_path / "out.parquet", evil)


@pytest.mark.parametrize("mode", ["file", "streaming"])
def test_runtime_rejects_database_sidecar_quarantine_path(tmp_path, monkeypatch, mode):
    """Runtime validation blocks the exploit even if a contract is mutated after parsing."""
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    pl.DataFrame({"val": [1.0]}).write_parquet(source)

    tenant_quarantine = settings.get_tenant_quarantine_dir("tenant-a")
    contract = _contract(source, tmp_path / "out.parquet", tenant_quarantine / "safe")
    sidecar = tenant_quarantine / "datashell_history.db-wal"
    contract.spec.delivery.quarantinePath = str(sidecar)

    pipeline = DataShellPipeline(contract, tenant_id="tenant-a")
    with pytest.raises(PathSecurityViolation, match="operational database or lock file"):
        if mode == "file":
            pipeline.run_file(source)
        else:
            pipeline.run_streaming(source)

    assert not sidecar.exists()


def test_contract_rejects_quarantine_path_inside_reserved_namespace(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    pl.DataFrame({"val": [1.0]}).write_parquet(source)
    evil = settings.audit_dir / "quarantine"

    with pytest.raises(PathSecurityViolation, match="reserved operational namespace"):
        _contract(source, tmp_path / "out.parquet", evil)


@pytest.mark.parametrize("mode", ["file", "streaming"])
def test_cross_tenant_quarantine_path_is_refused(tmp_path, monkeypatch, mode):
    """Tenant B cannot direct quarantine into tenant A's quarantine namespace."""
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    pl.DataFrame({"val": [1.0]}).write_parquet(source)

    foreign_quarantine = settings.get_tenant_quarantine_dir("tenant-a") / "foreign"
    contract = _contract(source, tmp_path / "out.parquet", foreign_quarantine)
    pipeline = DataShellPipeline(contract, tenant_id="tenant-b")

    with pytest.raises(PathSecurityViolation, match="Cross-tenant quarantine boundary"):
        if mode == "file":
            pipeline.run_file(source)
        else:
            pipeline.run_streaming(source)

    assert not foreign_quarantine.exists()


def test_quarantine_path_cannot_target_another_tenants_delivery_dir(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    foreign_delivery = settings.get_tenant_delivery_dir("tenant-a") / "quarantine"

    with pytest.raises(PathSecurityViolation, match="Cross-tenant storage boundary"):
        FilesystemSandbox.sanitize_and_validate_path(
            foreign_delivery,
            tenant_id="tenant-b",
            is_quarantine_path=True,
            create_parent=False,
        )

    assert not foreign_delivery.exists()


@pytest.mark.parametrize("mode", ["file", "streaming"])
def test_quarantine_path_inside_own_tenant_namespace_is_used(tmp_path, monkeypatch, mode):
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    pl.DataFrame({"val": [1.0, -5.0]}).write_parquet(source)

    quarantine = settings.get_tenant_quarantine_dir("tenant-a") / "custom"
    contract = _contract(source, tmp_path / "out.parquet", quarantine)
    pipeline = DataShellPipeline(contract, tenant_id="tenant-a")

    result = pipeline.run_file(source) if mode == "file" else pipeline.run_streaming(source)

    assert result.ledger.held_quality == 1
    assert result.outcome.quarantine_path is not None
    assert str(quarantine.resolve()) == result.outcome.quarantine_path
    assert list(quarantine.glob("*_held_records.parquet"))


def test_production_quarantine_rejects_temp_and_cross_tenant_roots(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "environment", "production")

    tenant_quarantine = settings.get_tenant_quarantine_dir("tenant-a") / "ok"
    assert FilesystemSandbox.sanitize_and_validate_path(
        tenant_quarantine,
        tenant_id="tenant-a",
        is_quarantine_path=True,
        create_parent=False,
    ) == tenant_quarantine.resolve()

    with pytest.raises(PathSecurityViolation):
        FilesystemSandbox.sanitize_and_validate_path(
            tmp_path / "scratch",
            tenant_id="tenant-a",
            is_quarantine_path=True,
            create_parent=False,
        )

    with pytest.raises(PathSecurityViolation, match="Cross-tenant quarantine boundary"):
        FilesystemSandbox.sanitize_and_validate_path(
            settings.quarantine_dir / "tenants" / "tenant-b" / "stolen",
            tenant_id="tenant-a",
            is_quarantine_path=True,
            create_parent=False,
        )


def test_shared_quarantine_root_is_rebased_into_the_tenant_namespace(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    tenant_quarantine = settings.get_tenant_quarantine_dir("tenant-a")

    resolved = FilesystemSandbox.sanitize_and_validate_path(
        settings.quarantine_dir / "custom",
        tenant_id="tenant-a",
        is_quarantine_path=True,
        create_parent=False,
    )

    assert resolved == (tenant_quarantine / "custom").resolve()
    assert not (settings.quarantine_dir / "custom").exists()


def test_quarantine_path_cannot_point_at_an_existing_file(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    tenant_quarantine = settings.get_tenant_quarantine_dir("tenant-a")
    existing_file = tenant_quarantine / "not-a-directory"
    existing_file.write_text("occupied")

    with pytest.raises(PathSecurityViolation, match="exists and is not a directory"):
        FilesystemSandbox.sanitize_and_validate_path(
            existing_file, tenant_id="tenant-a", is_quarantine_path=True
        )

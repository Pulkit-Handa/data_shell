"""Tests for the 20 GiB default and storage-headroom preflight hardening."""

from __future__ import annotations

import pytest

from datashell.core.config import (
    DEFAULT_MAX_UPLOAD_SIZE_BYTES,
    MULTIPART_OVERHEAD_BYTES,
    EnterpriseSettings,
    settings,
)
from datashell.core.fileops import InsufficientStorageError, ensure_disk_headroom
from datashell.contract.models import DataContract
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import DataShellPipeline


def test_builtin_default_upload_size_is_20_gib():
    assert DEFAULT_MAX_UPLOAD_SIZE_BYTES == 21_474_836_480


def test_default_body_limit_tracks_upload_limit():
    # Invariant holds whether the defaults or environment overrides are in play.
    assert settings.max_upload_size_bytes > 0
    assert settings.max_request_body_bytes >= settings.max_upload_size_bytes + MULTIPART_OVERHEAD_BYTES


def test_enterprise_settings_reject_nonpositive_headroom_multiplier():
    with pytest.raises(Exception):
        EnterpriseSettings(disk_headroom_multiplier=0)


def test_ensure_disk_headroom_raises_when_volume_too_small(monkeypatch, tmp_path):
    monkeypatch.setattr("datashell.core.fileops.available_bytes", lambda _root: 1_000)
    with pytest.raises(InsufficientStorageError, match="Insufficient free space"):
        ensure_disk_headroom(10_000, multiplier=2.0, root=tmp_path)


def test_ensure_disk_headroom_passes_with_room(monkeypatch, tmp_path):
    monkeypatch.setattr("datashell.core.fileops.available_bytes", lambda _root: 10**12)
    ensure_disk_headroom(10_000, multiplier=2.0, root=tmp_path)  # must not raise


def test_ensure_disk_headroom_ignores_empty_source(monkeypatch, tmp_path):
    monkeypatch.setattr("datashell.core.fileops.available_bytes", lambda _root: 0)
    ensure_disk_headroom(0, multiplier=2.0, root=tmp_path)  # must not raise


def _contract(source_path, target_path, quarantine_path=None):
    delivery = {"targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(target_path), "mode": "overwrite"}]}
    if quarantine_path is not None:
        delivery["quarantinePath"] = str(quarantine_path)
    return DataContract(
        metadata={"name": "preflight-contract", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(source_path),
                "schema": [{"name": "val", "polars_type": "Float64", "nullable": False}],
            }],
            "quality": {"behavior": "DROP_ROW", "rules": []},
            "delivery": delivery,
        },
    )


def test_pipeline_refuses_to_start_without_storage_headroom(tmp_path, monkeypatch):
    source = tmp_path / "in.csv"
    source.write_text("val\n1.0\n")
    contract = _contract(source, tmp_path / "out.parquet")
    monkeypatch.setattr("datashell.core.fileops.available_bytes", lambda _root: 0)
    with pytest.raises(InsufficientStorageError):
        DataShellPipeline(contract).run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "out.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )


def test_certificate_records_quarantine_paths_for_custom_location(tmp_path):
    """A custom quarantine location must not break certificate verification.

    Regression: the certificate used to store only the SHA-256 of each
    quarantine artifact, not its path, so a certificate was unverifiable whenever
    a contract directed quarantine somewhere other than the tenant root.
    """
    source = tmp_path / "in.csv"
    source.write_text("val\n1.0\n-5.0\n")
    quarantine_dir = tmp_path / "quarantine-custom"
    contract = DataContract(
        metadata={"name": "quarantine-path-contract", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(source),
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
                    "path": str(tmp_path / "out.parquet"),
                    "mode": "overwrite",
                }],
                "quarantinePath": str(quarantine_dir),
            },
        },
    )
    result = DataShellPipeline(contract).run_file(
        source_path=str(source),
        target_path_override=str(tmp_path / "out.parquet"),
        audit_output_path=str(tmp_path / "audit.json"),
    )
    assert result.ledger.held_quality == 1

    manifest = result.audit_certificate["cryptographic_manifest"]["quarantine_artifacts"]
    assert manifest, "expected at least one quarantine artifact for the held row"
    for entry in manifest.values():
        assert isinstance(entry, dict)
        assert entry["path"].startswith(str(quarantine_dir))

    report = ArtifactGenerator.verify_certificate_and_artifacts(result.audit_certificate)
    assert report["status"] == "AUTHENTIC_AND_UNMODIFIED"

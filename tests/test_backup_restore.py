"""Backup and restore tests for the Data Shell boundary ledger and artifacts.

Verifies that backup/restore produces consistent state and that restored
data passes integrity checks.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("polars")

from scripts.backup_restore import create_backup, restore_backup, verify_restore
from datashell.core.ledger import BoundaryLedger


class TestBackupRestore:
    """Backup and restore produce consistent state."""

    def test_backup_creates_manifest_with_checksums(self, tmp_path):
        """Backup creates a manifest with file checksums."""
        storage = tmp_path / "storage"
        storage.mkdir()
        output = storage / "output"
        output.mkdir()
        audits = output / "audits"
        audits.mkdir()

        # Create a test ledger
        db_path = output / "datashell_history.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE run_history (run_id TEXT PRIMARY KEY, tenant_id TEXT, contract_name TEXT, timestamp TEXT, rows_read INTEGER, rows_delivered INTEGER, metrics_json TEXT)")
        conn.execute("INSERT INTO run_history VALUES ('run1', 'tenant-a', 'test', '2026-01-01', 100, 90, '{}')")
        conn.commit()
        conn.close()

        # Create a test artifact
        artifact = output / "delivered.parquet"
        artifact.write_bytes(b"test data")

        backup_dir = tmp_path / "backup"
        manifest = create_backup(storage, backup_dir)

        assert manifest["version"] == 1
        assert len(manifest["files"]) >= 2  # ledger + artifact

        # Verify manifest file exists
        assert (backup_dir / "backup_manifest.json").exists()

    def test_restore_recreates_ledger_and_artifacts(self, tmp_path):
        """Restore recreates the ledger and artifacts correctly."""
        storage = tmp_path / "storage"
        storage.mkdir()
        output = storage / "output"
        output.mkdir()

        # Create a test ledger with all expected tables
        db_path = output / "datashell_history.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE run_history (run_id TEXT PRIMARY KEY, tenant_id TEXT, contract_name TEXT, timestamp TEXT, rows_read INTEGER, rows_delivered INTEGER, metrics_json TEXT)")
        conn.execute("CREATE TABLE quarantine_index (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, contract_name TEXT, timestamp TEXT NOT NULL, kind TEXT NOT NULL, artifact_path TEXT, row_count INTEGER DEFAULT 0, dominant_reason TEXT, reasons_json TEXT)")
        conn.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, contract_name TEXT, timestamp TEXT NOT NULL, status TEXT, execution_mode TEXT, rows_read INTEGER DEFAULT 0, rows_delivered INTEGER DEFAULT 0, bytes_processed INTEGER DEFAULT 0, billable INTEGER DEFAULT 1, unit TEXT DEFAULT 'row', units INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO run_history VALUES ('run1', 'tenant-a', 'test', '2026-01-01', 100, 90, '{}')")
        conn.commit()
        conn.close()

        # Create a test artifact
        artifact = output / "test_artifact.txt"
        artifact.write_bytes(b"test artifact content")

        # Backup
        backup_dir = tmp_path / "backup"
        create_backup(storage, backup_dir)

        # Restore to a different location
        restore_root = tmp_path / "restore"
        report = restore_backup(backup_dir, restore_root)

        assert report["errors"] == []
        assert report["restored_files"] >= 2
        assert report["ledger_ok"] is True

        # Verify restored data
        restored_db = restore_root / "output" / "datashell_history.db"
        assert restored_db.exists()
        conn = sqlite3.connect(str(restored_db))
        row = conn.execute("SELECT * FROM run_history WHERE run_id='run1'").fetchone()
        assert row is not None
        assert row[1] == "tenant-a"
        conn.close()

        restored_artifact = restore_root / "output" / "test_artifact.txt"
        assert restored_artifact.exists()
        assert restored_artifact.read_bytes() == b"test artifact content"

    def test_verify_restore_checks_pass(self, tmp_path):
        """Verification passes for a correctly restored backup."""
        storage = tmp_path / "storage"
        storage.mkdir()
        output = storage / "output"
        output.mkdir()
        audits = output / "audits"
        audits.mkdir()

        # Create a test ledger with all expected tables
        db_path = output / "datashell_history.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE run_history (run_id TEXT PRIMARY KEY, tenant_id TEXT, contract_name TEXT, timestamp TEXT, rows_read INTEGER, rows_delivered INTEGER, metrics_json TEXT)")
        conn.execute("CREATE TABLE quarantine_index (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, contract_name TEXT, timestamp TEXT NOT NULL, kind TEXT NOT NULL, artifact_path TEXT, row_count INTEGER DEFAULT 0, dominant_reason TEXT, reasons_json TEXT)")
        conn.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, contract_name TEXT, timestamp TEXT NOT NULL, status TEXT, execution_mode TEXT, rows_read INTEGER DEFAULT 0, rows_delivered INTEGER DEFAULT 0, bytes_processed INTEGER DEFAULT 0, billable INTEGER DEFAULT 1, unit TEXT DEFAULT 'row', units INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO run_history VALUES ('run1', 'tenant-a', 'test', '2026-01-01', 100, 90, '{}')")
        conn.commit()
        conn.close()

        # Create a valid certificate
        cert = {
            "cryptographic_manifest": {"merkle_root": "abc123"},
            "digital_signature": {"algorithm": "HMAC-SHA256", "key_id": "test"},
        }
        (audits / "run1_audit_certificate.json").write_text(json.dumps(cert))

        # Backup and restore
        backup_dir = tmp_path / "backup"
        create_backup(storage, backup_dir)
        restore_root = tmp_path / "restore"
        restore_backup(backup_dir, restore_root)

        # Verify
        report = verify_restore(restore_root)
        assert report["checks"]["ledger_integrity"] is True
        assert report["checks"]["run_count"] == 1
        assert report["checks"]["certificates_total"] == 1
        assert report["checks"]["certificates_valid_structure"] == 1
        assert report["checks"]["leftover_journals"] == 0

    def test_restore_detects_corruption(self, tmp_path):
        """Restore detects checksum mismatches."""
        storage = tmp_path / "storage"
        storage.mkdir()
        output = storage / "output"
        output.mkdir()

        # Create a test file
        test_file = output / "test.txt"
        test_file.write_bytes(b"original")

        # Backup
        backup_dir = tmp_path / "backup"
        create_backup(storage, backup_dir)

        # Corrupt the backup file (note: files are under output/ in the backup)
        (backup_dir / "output" / "test.txt").write_bytes(b"corrupted")

        # Restore should detect the mismatch
        restore_root = tmp_path / "restore"
        report = restore_backup(backup_dir, restore_root)
        assert len(report["errors"]) > 0
        assert "Checksum mismatch" in report["errors"][0]

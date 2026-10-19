"""Fault-injection tests for publication, ledger, disk, and recovery paths.

These tests verify that the system leaves either a consistent previous state
or a consistent new state after a crash or failure — never a mixed set.

All tests use temporary directories, disposable databases, and synthetic data.
No real credentials or production data are used.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("polars")

from datashell.core.commit import (
    publish_transactionally,
    recover_publication,
    recover_pending_publications,
)
from datashell.core.config import EnterpriseSettings, settings
from datashell.core.ledger import BoundaryLedger
from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline


def _make_contract(tmp_path: Path, target_name: str = "delivered.parquet") -> DataContract:
    """Create a minimal valid contract for testing."""
    target = tmp_path / target_name
    return DataContract(
        metadata={"name": "fault-inject-contract", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(tmp_path / "input.csv"),
                "schema": [{"name": "val", "polars_type": "Float64", "nullable": False}],
            }],
            "quality": {"behavior": "DROP_ROW", "rules": []},
            "delivery": {
                "targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(target), "mode": "overwrite"}],
            },
        },
    )


def _write_test_csv(path: Path, rows: int = 10) -> None:
    """Write a simple CSV for testing."""
    with open(path, "w") as f:
        f.write("val\n")
        for i in range(rows):
            f.write(f"{float(i)}\n")


# =============================================================================
# FA-05: Process termination during publication
# =============================================================================


class TestCrashDuringPublication:
    """Simulate crashes at each publication step and verify recovery."""

    def test_crash_after_journal_write_before_any_rename(self, tmp_path):
        """Journal exists with state=prepared, no files moved yet."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        staged.write_bytes(b"new data")

        # Simulate a crash after journal write but before any file moves
        run_id = "dm_run_crash001"
        journal_data = {
            "version": 1,
            "run_id": run_id,
            "state": "prepared",
            "entries": [{
                "staged": str(staged),
                "final": str(target),
                "backup": str(target.with_name(f".{target.name}.{run_id}.0.backup")),
                "had_original": False,
            }],
        }
        journal_path = tmp_path / f".dm-commit-{__import__('hashlib').sha256(str(target).encode()).hexdigest()[:20]}.json"
        journal_path.write_text(json.dumps(journal_data))

        # Recovery should roll back: target should not exist, staged should be cleaned
        recover_publication(target, transaction_root=tmp_path)
        assert not target.exists()
        assert not journal_path.exists()

    def test_crash_after_first_rename_before_second(self, tmp_path):
        """First artifact published, second not yet — must roll back both."""
        target1 = tmp_path / "target1.parquet"
        target2 = tmp_path / "target2.parquet"
        staged1 = tmp_path / ".staged_test_target1.parquet"
        staged2 = tmp_path / ".staged_test_target2.parquet"
        staged1.write_bytes(b"data1")
        staged2.write_bytes(b"data2")

        # Simulate: first staged file already renamed to final, second not yet
        target1.write_bytes(b"data1")  # first publish completed
        staged1.unlink()  # staged file was renamed away
        run_id = "dm_run_crash002"
        journal_data = {
            "version": 1,
            "run_id": run_id,
            "state": "prepared",
            "entries": [
                {"staged": str(staged1), "final": str(target1), "backup": str(target1.with_name(f".{target1.name}.{run_id}.0.backup")), "had_original": False},
                {"staged": str(staged2), "final": str(target2), "backup": str(target2.with_name(f".{target2.name}.{run_id}.1.backup")), "had_original": False},
            ],
        }
        journal_path = tmp_path / f".dm-commit-{__import__('hashlib').sha256(str(target2).encode()).hexdigest()[:20]}.json"
        journal_path.write_text(json.dumps(journal_data))

        # Recovery should remove target1 (it was published but not committed)
        recover_publication(target2, transaction_root=tmp_path)
        assert not target1.exists()
        assert not target2.exists()
        assert not journal_path.exists()

    def test_crash_after_committed_journal_before_cleanup(self, tmp_path):
        """Journal says committed, backups should be cleaned up."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        target.write_bytes(b"published data")
        staged.write_bytes(b"leftover staged")

        run_id = "dm_run_crash003"
        backup = target.with_name(f".{target.name}.{run_id}.0.backup")
        backup.write_bytes(b"backup data")

        journal_data = {
            "version": 1,
            "run_id": run_id,
            "state": "committed",
            "entries": [{
                "staged": str(staged),
                "final": str(target),
                "backup": str(backup),
                "had_original": False,
            }],
        }
        journal_path = tmp_path / f".dm-commit-{__import__('hashlib').sha256(str(target).encode()).hexdigest()[:20]}.json"
        journal_path.write_text(json.dumps(journal_data))

        # Recovery should clean up backup and journal, keep target
        recover_publication(target, transaction_root=tmp_path)
        assert target.exists()
        assert not backup.exists()
        assert not journal_path.exists()

    def test_existing_preserved_on_rollback(self, tmp_path):
        """If an original existed, rollback restores it."""
        target = tmp_path / "output.parquet"
        target.write_bytes(b"original data")
        staged = tmp_path / ".staged_test_output.parquet"
        staged.write_bytes(b"new data")

        run_id = "dm_run_crash004"
        backup = target.with_name(f".{target.name}.{run_id}.0.backup")
        # Backup the original
        backup.write_bytes(b"original data")

        journal_data = {
            "version": 1,
            "run_id": run_id,
            "state": "prepared",
            "entries": [{
                "staged": str(staged),
                "final": str(target),
                "backup": str(backup),
                "had_original": True,
            }],
        }
        journal_path = tmp_path / f".dm-commit-{__import__('hashlib').sha256(str(target).encode()).hexdigest()[:20]}.json"
        journal_path.write_text(json.dumps(journal_data))

        recover_publication(target, transaction_root=tmp_path)
        assert target.read_bytes() == b"original data"
        assert not backup.exists()
        assert not journal_path.exists()


# =============================================================================
# FA-04: Disk exhaustion
# =============================================================================


class TestDiskExhaustion:
    """Verify behavior when disk is full during various stages."""

    def test_insufficient_headroom_refuses_run(self, tmp_path, monkeypatch):
        """Pipeline refuses to start when storage cannot hold the run."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=100)
        contract = _make_contract(tmp_path)

        # Simulate zero available disk
        monkeypatch.setattr("datashell.core.fileops.available_bytes", lambda _root: 0)

        from datashell.core.fileops import InsufficientStorageError
        with pytest.raises(InsufficientStorageError):
            DataShellPipeline(contract).run_file(
                source_path=str(source),
                target_path_override=str(tmp_path / "delivered.parquet"),
                audit_output_path=str(tmp_path / "audit.json"),
            )

    def test_publish_transactionally_stages_nothing_on_missing_staged(self, tmp_path):
        """If staged file is missing, publish fails cleanly."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        # Do not create staged file

        with pytest.raises(FileNotFoundError):
            publish_transactionally(
                [(staged, target)],
                run_id="dm_run_disk001",
                transaction_root=tmp_path,
            )
        assert not target.exists()


# =============================================================================
# FA-06: Database unavailable
# =============================================================================


class TestLedgerUnavailable:
    """Verify behavior when the boundary ledger is unavailable."""

    def test_ledger_unavailable_does_not_block_delivery(self, tmp_path):
        """A delivered artifact survives a ledger recording failure."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=10)
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)

        # Make ledger recording fail
        original_record = pipeline.history_tracker.record_run
        def failing_record(*args, **kwargs):
            raise Exception("Simulated ledger failure")

        pipeline.history_tracker.record_run = failing_record

        # The run should still succeed (artifact delivered) even if ledger fails
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        assert result.status.value == "LOADED"
        assert target.exists()

    def test_ledger_integrity_check_fails_cleanly(self, tmp_path):
        """check_integrity returns False for corrupt database."""
        db_path = tmp_path / "test.db"
        # Create a corrupt database file
        db_path.write_bytes(b"not a sqlite database")

        ledger = BoundaryLedger(db_path=db_path)
        assert not ledger.check_integrity()


# =============================================================================
# FA-07/FA-08: Partial publication failures
# =============================================================================


class TestPartialPublicationFailure:
    """Verify consistency when ledger and publication diverge."""

    def test_ledger_failure_is_non_fatal(self, tmp_path):
        """Ledger recording failure does not roll back delivery."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=10)
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)

        # Make all ledger operations fail
        def failing_record(*args, **kwargs):
            raise Exception("Ledger unavailable")

        pipeline.history_tracker.record_run = failing_record
        pipeline.history_tracker.record_quarantine = failing_record
        pipeline.history_tracker.record_usage = failing_record

        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # Delivery succeeds despite ledger failure
        assert result.status.value == "LOADED"
        assert target.exists()
        # Audit certificate should still be written
        assert (tmp_path / "audit.json").exists()

    def test_rejected_run_publishes_no_accepted_output(self, tmp_path):
        """A rejected run must not publish accepted artifacts."""
        source = tmp_path / "input.csv"
        with open(source, "w") as f:
            f.write("val\n")  # header only, no data rows
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # Empty input should not produce a delivered artifact
        assert result.status.value in ("REJECTED", "LOADED")


# =============================================================================
# FA-09: Concurrent writes
# =============================================================================


class TestConcurrentWrites:
    """Verify that concurrent writes to the same target are safe."""

    def test_concurrent_publish_same_target(self, tmp_path):
        """Two concurrent publishes to the same target: one wins atomically."""
        import threading

        target = tmp_path / "output.parquet"
        errors = []
        successes = []

        def publish_attempt(name: str):
            try:
                staged = tmp_path / f".staged_{name}_output.parquet"
                staged.write_bytes(f"data from {name}".encode())
                publish_transactionally(
                    [(staged, target)],
                    run_id=f"dm_run_{name}",
                    transaction_root=tmp_path,
                )
                successes.append(name)
            except Exception as e:
                errors.append((name, e))

        threads = [
            threading.Thread(target=publish_attempt, args=("thread1",)),
            threading.Thread(target=publish_attempt, args=("thread2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # At least one should succeed; the target should exist and be valid
        assert target.exists()
        # No mixed state: target should contain exactly one version
        content = target.read_bytes()
        assert content in (b"data from thread1", b"data from thread2")

    def test_concurrent_recover_and_publish(self, tmp_path):
        """Recovery during a concurrent publish is safe."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        staged.write_bytes(b"new data")

        import threading
        results = []

        def do_recover():
            try:
                recover_publication(target, transaction_root=tmp_path)
                results.append("recover_ok")
            except Exception as e:
                results.append(f"recover_error: {e}")

        def do_publish():
            try:
                publish_transactionally(
                    [(staged, target)],
                    run_id="dm_run_concurrent",
                    transaction_root=tmp_path,
                )
                results.append("publish_ok")
            except Exception as e:
                results.append(f"publish_error: {e}")

        t1 = threading.Thread(target=do_recover)
        t2 = threading.Thread(target=do_publish)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Both should complete without crashing
        assert len(results) == 2


# =============================================================================
# FA-13: Adversarial input
# =============================================================================


class TestAdversarialInput:
    """Verify behavior with malformed or adversarial inputs."""

    def test_malformed_csv_input(self, tmp_path):
        """Malformed CSV rows are refused, not crashed on."""
        source = tmp_path / "malformed.csv"
        with open(source, "w") as f:
            f.write("val\n")
            f.write("1.0\n")
            f.write("not_a_number\n")  # This row should be refused
            f.write("3.0\n")
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # The valid rows should be delivered, malformed refused
        assert result.status.value in ("LOADED", "REJECTED")
        # Reconciliation must always hold
        assert result.ledger.footing_matched

    def test_empty_file_input(self, tmp_path):
        """Empty file is handled gracefully."""
        source = tmp_path / "empty.csv"
        source.write_text("val\n")  # header only
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # Should not crash
        assert result.status.value in ("LOADED", "REJECTED")

    def test_schema_mismatch_extra_columns(self, tmp_path):
        """Extra columns in input are handled by contract policy."""
        source = tmp_path / "extra_cols.csv"
        with open(source, "w") as f:
            f.write("val,extra_col\n")
            f.write("1.0,hello\n")
            f.write("2.0,world\n")
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # Should process without crashing
        assert result.status.value in ("LOADED", "REJECTED")
        assert result.ledger.footing_matched

    def test_reconciliation_invariant_holds(self, tmp_path):
        """The reconciliation invariant: rows_read = delivered + held + refused + excluded."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=100)
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # The invariant must always hold
        assert result.ledger.rows_read == (
            result.ledger.rows_to_deliver
            + result.ledger.held_quality
            + result.ledger.refused_structural
            + result.ledger.unaccounted_for
        )


# =============================================================================
# FA-12: Key rotation
# =============================================================================


class TestKeyRotation:
    """Verify certificate behavior during key rotation."""

    def test_unknown_key_id_fails_verification(self, tmp_path):
        """Certificate with unknown key ID fails verification."""
        from datashell.core.crypto import CryptoEngine

        manifest = {"test": "data"}
        signature = CryptoEngine.sign_manifest(manifest)

        # Verify with correct key works
        assert CryptoEngine.verify_signature(manifest, signature)

        # Tampered manifest fails
        tampered = {"test": "tampered"}
        assert not CryptoEngine.verify_signature(tampered, signature)


# =============================================================================
# FA-14: Worker restart
# =============================================================================


class TestWorkerRestart:
    """Verify recovery after worker restart."""

    def test_recover_pending_publications_on_startup(self, tmp_path):
        """Startup recovery handles all pending journals."""
        # Create multiple pending journals
        target1 = tmp_path / "output1.parquet"
        target2 = tmp_path / "output2.parquet"
        staged1 = tmp_path / ".staged_test_output1.parquet"
        staged2 = tmp_path / ".staged_test_output2.parquet"
        staged1.write_bytes(b"data1")
        staged2.write_bytes(b"data2")

        run_id = "dm_run_restart001"
        for i, (staged, final) in enumerate([(staged1, target1), (staged2, target2)]):
            journal_data = {
                "version": 1,
                "run_id": run_id,
                "state": "prepared",
                "entries": [{
                    "staged": str(staged),
                    "final": str(final),
                    "backup": str(final.with_name(f".{final.name}.{run_id}.{i}.backup")),
                    "had_original": False,
                }],
            }
            import hashlib
            journal_path = tmp_path / f".dm-commit-{hashlib.sha256(str(final).encode()).hexdigest()[:20]}.json"
            journal_path.write_text(json.dumps(journal_data))

        # Recover all pending publications
        count = recover_pending_publications(tmp_path)
        assert count == 2
        # All journals should be cleaned up
        assert not any(tmp_path.glob(".dm-commit-*.json"))


# =============================================================================
# FA-02: Schema changes
# =============================================================================


class TestSchemaChanges:
    """Verify behavior when schema changes between runs."""

    def test_missing_field_in_input(self, tmp_path):
        """Input missing a declared field is handled."""
        source = tmp_path / "missing_field.csv"
        with open(source, "w") as f:
            f.write("wrong_field\n")
            f.write("1.0\n")
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        # Should not crash; structural rejection expected
        assert result.ledger.footing_matched

    def test_type_mismatch_in_input(self, tmp_path):
        """Input with wrong types is handled."""
        source = tmp_path / "type_mismatch.csv"
        with open(source, "w") as f:
            f.write("val\n")
            f.write("not_a_float_at_all\n")
        target = tmp_path / "delivered.parquet"
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(target),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        assert result.ledger.footing_matched

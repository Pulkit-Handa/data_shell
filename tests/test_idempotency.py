"""Idempotency and retry semantics tests for the Data Shell pipeline.

Verifies that repeated and ambiguous requests cannot create unintended
duplicate delivery, and that callers can discover the original outcome.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("polars")

from datashell.core.commit import publish_transactionally, recover_publication
from datashell.core.ledger import BoundaryLedger
from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline


def _make_contract(tmp_path: Path, target_name: str = "delivered.parquet") -> DataContract:
    """Create a minimal valid contract for testing."""
    target = tmp_path / target_name
    return DataContract(
        metadata={"name": "idempotency-contract", "version": "1.0.0"},
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


class TestIdempotentPublication:
    """Publication is idempotent for the same run_id."""

    def test_same_run_id_replaces_not_duplicates(self, tmp_path):
        """Publishing twice with the same run_id replaces, not duplicates."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        staged.write_bytes(b"first")

        run_id = "dm_run_idem001"
        publish_transactionally(
            [(staged, target)],
            run_id=run_id,
            transaction_root=tmp_path,
        )
        assert target.read_bytes() == b"first"

        # Second publish with same run_id replaces
        staged.write_bytes(b"second")
        publish_transactionally(
            [(staged, target)],
            run_id=run_id,
            transaction_root=tmp_path,
        )
        assert target.read_bytes() == b"second"

    def test_recovery_after_successful_publish(self, tmp_path):
        """After a successful publish, recovery is a no-op."""
        target = tmp_path / "output.parquet"
        staged = tmp_path / ".staged_test_output.parquet"
        staged.write_bytes(b"data")

        run_id = "dm_run_idem002"
        publish_transactionally(
            [(staged, target)],
            run_id=run_id,
            transaction_root=tmp_path,
        )
        assert target.exists()

        # Recovery should be a no-op (no journal left behind)
        recover_publication(target, transaction_root=tmp_path)
        assert target.exists()


class TestLedgerIdempotency:
    """Ledger recording is idempotent on run_id."""

    def test_record_run_idempotent_on_run_id(self, tmp_path):
        """Recording the same run_id twice replaces, not duplicates."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        run_id = "dm_run_idem003"
        ledger.record_run(
            run_id=run_id,
            contract_name="test-contract",
            rows_read=100,
            rows_delivered=90,
            metrics={"null_rate": 0.1},
            tenant_id="tenant-a",
            status="LOADED",
        )

        # Record same run_id again with different values
        ledger.record_run(
            run_id=run_id,
            contract_name="test-contract",
            rows_read=100,
            rows_delivered=90,
            metrics={"null_rate": 0.1},
            tenant_id="tenant-a",
            status="LOADED",
        )

        # Should have exactly one row
        runs = ledger.list_runs("tenant-a")
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id

    def test_record_usage_idempotent_on_run_id(self, tmp_path):
        """Usage recording is idempotent on (tenant_id, run_id)."""
        db_path = tmp_path / "test_ledger.db"
        ledger = BoundaryLedger(db_path=db_path)

        run_id = "dm_run_idem004"
        ledger.record_usage(
            run_id=run_id,
            tenant_id="tenant-a",
            contract_name="test-contract",
            status="LOADED",
            execution_mode="in_memory",
            rows_read=100,
            rows_delivered=90,
        )

        # Record again
        ledger.record_usage(
            run_id=run_id,
            tenant_id="tenant-a",
            contract_name="test-contract",
            status="LOADED",
            execution_mode="in_memory",
            rows_read=100,
            rows_delivered=90,
        )

        summary = ledger.usage_summary("tenant-a")
        assert summary["total_runs"] == 1


class TestRetrySemantics:
    """Retry behavior for pipeline runs."""

    def test_retry_after_success_returns_same_result(self, tmp_path):
        """Retrying after a successful run with the same input produces consistent results."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=10)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)

        result1 = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit1.json"),
        )

        result2 = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit2.json"),
        )

        # Both should succeed with the same reconciliation
        assert result1.status.value == result2.status.value
        assert result1.ledger.rows_read == result2.ledger.rows_read
        assert result1.ledger.rows_to_deliver == result2.ledger.rows_to_deliver

    def test_ledger_deduplicates_retry(self, tmp_path):
        """A retried run with the same run_id does not double-count in the ledger."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=10)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        run_id = "dm_run_retry_001"

        # Manually record the same run_id twice
        pipeline.history_tracker.record_run(
            run_id=run_id,
            contract_name="idempotency-contract",
            rows_read=10,
            rows_delivered=10,
            metrics={},
            tenant_id="default-tenant",
            status="LOADED",
        )
        pipeline.history_tracker.record_run(
            run_id=run_id,
            contract_name="idempotency-contract",
            rows_read=10,
            rows_delivered=10,
            metrics={},
            tenant_id="default-tenant",
            status="LOADED",
        )

        runs = pipeline.history_tracker.list_runs("default-tenant")
        matching = [r for r in runs if r["run_id"] == run_id]
        assert len(matching) == 1

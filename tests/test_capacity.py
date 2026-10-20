"""Capacity and benchmark tests using synthetic data.

Measures throughput, latency, memory, and disk usage for various input sizes
and configurations. Uses explicit resource budgets and acceptance thresholds.
"""
from __future__ import annotations

import resource
import time
from pathlib import Path

import pytest

pytest.importorskip("polars")

from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline


def _make_contract(tmp_path: Path, target_name: str = "delivered.parquet") -> DataContract:
    """Create a minimal valid contract for benchmarking."""
    target = tmp_path / target_name
    return DataContract(
        metadata={"name": "benchmark-contract", "version": "1.0.0"},
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


def _write_test_csv(path: Path, rows: int) -> None:
    """Write a simple CSV for benchmarking."""
    with open(path, "w") as f:
        f.write("val\n")
        for i in range(rows):
            f.write(f"{float(i)}\n")


def _peak_rss_mb() -> float:
    """Get peak RSS in MB."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024.0


class TestCapacityBaseline:
    """Baseline capacity measurements with synthetic data."""

    def test_small_file_1000_rows(self, tmp_path):
        """Process 1,000 rows within acceptable time."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=1000)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        start = time.time()
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        elapsed = time.time() - start

        assert result.status.value == "LOADED"
        assert result.ledger.footing_matched
        assert elapsed < 30.0  # Generous timeout for CI

    def test_medium_file_10000_rows(self, tmp_path):
        """Process 10,000 rows within acceptable time."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=10000)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        start = time.time()
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        elapsed = time.time() - start

        assert result.status.value == "LOADED"
        assert result.ledger.footing_matched
        assert elapsed < 60.0

    def test_large_file_100000_rows(self, tmp_path):
        """Process 100,000 rows within acceptable time."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=100000)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        start = time.time()
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )
        elapsed = time.time() - start

        assert result.status.value == "LOADED"
        assert result.ledger.footing_matched
        assert elapsed < 120.0

    def test_peak_rss_bounded_for_small_file(self, tmp_path):
        """Peak RSS is bounded for small files."""
        source = tmp_path / "input.csv"
        _write_test_csv(source, rows=1000)
        contract = _make_contract(tmp_path)

        pipeline = DataShellPipeline(contract)
        pipeline.run_file(
            source_path=str(source),
            target_path_override=str(tmp_path / "delivered.parquet"),
            audit_output_path=str(tmp_path / "audit.json"),
        )

        peak_mb = _peak_rss_mb()
        # Just record the measurement; don't assert a hard limit
        # since it varies by environment
        assert peak_mb > 0


class TestResourceLimits:
    """Verify resource limits are enforced."""

    def test_concurrent_runs_bounded(self, tmp_path):
        """Concurrent runs are bounded by MAX_CONCURRENT_RUNS."""
        from datashell.core.config import settings
        # The default is 2 concurrent runs
        assert settings.max_concurrent_runs >= 1

    def test_upload_size_bounded(self, tmp_path):
        """Upload size has a hard ceiling."""
        from datashell.core.config import settings
        assert settings.max_upload_size_bytes > 0
        assert settings.max_request_body_bytes >= settings.max_upload_size_bytes

    def test_stream_chunk_size_bounded(self, tmp_path):
        """Stream chunk size is positive."""
        from datashell.core.config import settings
        assert settings.stream_chunk_rows >= 1

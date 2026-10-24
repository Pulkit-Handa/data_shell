"""Production Volumetric and Systems Stress Testing Suite.

Evaluates:
- Row-level throughput (rows/sec) up to 1,000,000 rows.
- Peak memory footprint (tracemalloc / resource).
- Per-stage latency breakdown (Tier 1 vs Tier 2 vs Footing vs Delivery).
- Zero-deficit arithmetic preservation across high scale.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityRule,
    RowRuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.engines.delivery import TerminalStatus
from datashell.pipeline.runner import DataShellPipeline


class ProcessRSSSampler:
    """Sample total process RSS, including native Polars/Arrow/DuckDB allocations."""
    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _rss_bytes() -> int:
        # Linux procfs includes native allocations that tracemalloc cannot see.
        try:
            with open("/proc/self/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        # Portable fallback: ru_maxrss is a process-lifetime high-water mark.
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value * (1024 if sys.platform != "darwin" else 1))

    def _sample(self):
        while not self._stop.is_set():
            self.peak_bytes = max(self.peak_bytes, self._rss_bytes())
            self._stop.wait(self.interval)

    def __enter__(self):
        self.peak_bytes = self._rss_bytes()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.peak_bytes = max(self.peak_bytes, self._rss_bytes())


def create_stress_contract() -> DataContract:
    """Builds a realistic financial settlement contract with multi-column rules."""
    return DataContract(
        metadata={"name": "volumetric-stress-contract", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    format="parquet",
                    schema=[
                        SchemaColumn(name="transaction_id", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                        SchemaColumn(name="fee", polars_type="Float64", nullable=False),
                        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="status", polars_type="Utf8", nullable=False),
                    ]
                )
            ],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [
                    QualityRule(rule_id="r1-amount-positive",
                                check=RowRuleCheckType.GREATER_THAN, column="amount", value=0.0),
                    QualityRule(rule_id="r2-fee-non-negative",
                                check=RowRuleCheckType.GREATER_EQUAL, column="fee", value=0.0),
                    QualityRule(rule_id="r3-currency-allowed", check=RowRuleCheckType.ALLOWED_VALUES,
                                column="currency", value=["USD", "EUR", "GBP", "JPY"]),
                    QualityRule(rule_id="r4-custom-margin",
                                check=RowRuleCheckType.CUSTOM_EXPR, expression="amount >= fee"),
                ],
                "batch_rules": [
                    BatchRule(rule_id="b1-min-volume", check=BatchRuleCheckType.MIN_ROW_COUNT, value=1000),
                    BatchRule(rule_id="b2-avg-fee-ratio", check=BatchRuleCheckType.SQL_ASSERTION,
                              sql="AVG(fee) / NULLIF(AVG(amount), 0) < 0.20"),
                ]
            },
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path="./stress_output.parquet", mode="overwrite")],
                quarantinePath="./output/quarantine"
            )
        }
    )


def generate_synthetic_batch(n_rows: int, defect_rate: float = 0.05) -> Path:
    """Generates an in-memory synthetic Parquet dataset with controlled defect rates."""
    import numpy as np

    print(f"  Generating {n_rows:,} synthetic records (seeded {defect_rate*100:.1f}% defects)...")
    
    n_defects = int(n_rows * defect_rate)
    n_valid = n_rows - n_defects

    # Valid rows
    valid_ids = [f"TX-{i:09d}" for i in range(n_valid)]
    valid_amounts = np.random.uniform(10.0, 5000.0, n_valid)
    valid_fees = valid_amounts * np.random.uniform(0.01, 0.05, n_valid)
    valid_currencies = np.random.choice(["USD", "EUR", "GBP", "JPY"], n_valid)
    valid_status = ["CLEARED"] * n_valid

    # Defect rows (negative amounts, unallowed currencies, or fee > amount)
    defect_ids = [f"BAD-{i:09d}" for i in range(n_defects)]
    defect_amounts = np.random.uniform(-100.0, -1.0, n_defects)  # Negative
    defect_fees = np.random.uniform(0.0, 10.0, n_defects)
    defect_currencies = np.random.choice(["XYZ", "UNRECOGNIZED"], n_defects)
    defect_status = ["FAILED"] * n_defects

    # Combine into Polars DataFrame
    df = pl.DataFrame({
        "transaction_id": valid_ids + defect_ids,
        "amount": np.concatenate([valid_amounts, defect_amounts]),
        "fee": np.concatenate([valid_fees, defect_fees]),
        "currency": np.concatenate([valid_currencies, defect_currencies]),
        "status": valid_status + defect_status,
    })

    # Shuffle
    df = df.sample(fraction=1.0, shuffle=True, seed=42)

    fd, tmp_name = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    tmp_path = Path(tmp_name)
    df.write_parquet(tmp_path)
    return tmp_path


def run_benchmark():
    contract = create_stress_contract()
    pipeline = DataShellPipeline(contract)

    test_scales = [10_000, 100_000, 500_000, 1_000_000]

    # The delivery target and the audit certificate must live under the same
    # storage root, and in production both must stay under the configured
    # output directory. Use the configured directories rather than a relative
    # "./output" so the benchmark measures the deployed layout instead of the
    # repository working directory.
    from datashell.core.config import settings

    bench_output_dir = settings.output_dir.resolve() / "benchmarks"
    bench_audit_dir = settings.audit_dir.resolve() / "benchmarks"
    bench_output_dir.mkdir(parents=True, exist_ok=True)
    bench_audit_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 75)
    print("      DATA SHELL — VOLUMETRIC & SYSTEMS BENCHMARK SUITE")
    print("=" * 75)
    print(f"Storage root: {settings.storage_root.resolve()}")
    print(f"{'Scale (Rows)':<14} | {'Duration':<10} | {'Throughput':<16} | "
          f"{'Delivered':<12} | {'Peak RSS':<10} | {'Footing':<10}")
    print("-" * 75)

    for scale in test_scales:
        data_path = generate_synthetic_batch(scale, defect_rate=0.04)

        audit_path = bench_audit_dir / f"benchmark_{scale}_audit.json"
        start_time = time.perf_counter()
        with ProcessRSSSampler() as rss_sampler:
            result = pipeline.run_file(
                source_path=data_path,
                target_path_override=str(bench_output_dir / f"benchmark_{scale}.parquet"),
                audit_output_path=str(audit_path)
            )
        duration = time.perf_counter() - start_time

        expected_delivered = scale - int(scale * 0.04)
        actual_delivered = result.outcome.rows_delivered
        footing_str = "MATCHED" if result.ledger.footing_matched else "MISMATCH"
        if result.status != TerminalStatus.LOADED:
            raise RuntimeError(f"Benchmark {scale:,} rows unexpectedly ended with {result.status.value}")
        if not result.ledger.footing_matched:
            raise RuntimeError(f"Benchmark {scale:,} rows failed reconciliation footing")
        if actual_delivered != expected_delivered:
            raise RuntimeError(
                f"Benchmark {scale:,} rows delivered {actual_delivered:,}; expected {expected_delivered:,}"
            )
        if not result.outcome.target_path or not Path(result.outcome.target_path).is_file():
            raise RuntimeError(f"Benchmark {scale:,} rows reported delivery but target artifact is missing")
        actual_file_rows = pq.ParquetFile(result.outcome.target_path).metadata.num_rows
        if actual_file_rows != expected_delivered:
            raise RuntimeError(
                f"Benchmark target contains {actual_file_rows:,} rows; expected {expected_delivered:,}"
            )

        throughput = scale / duration if duration > 0 else 0
        peak_mb = rss_sampler.peak_bytes / (1024 * 1024)
        throughput_str = f"{int(throughput):,d} r/s"
        print(
            f"{scale:<14,d} | {duration:<9.2f}s | {throughput_str:<16} | "
            f"{actual_delivered:<12,d} | {peak_mb:<8.1f}MB | {footing_str:<10}"
        )

        # Remove generated inputs and benchmark artifacts so repeated runs are clean.
        data_path.unlink(missing_ok=True)
        Path(result.outcome.target_path).unlink(missing_ok=True)
        if result.outcome.run_artifact_path:
            Path(result.outcome.run_artifact_path).unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)

    print("=" * 75)
    print("Volumetric stress test completed successfully: every scale passed "
          "status, row-count, file-content, and footing assertions.")


if __name__ == "__main__":
    run_benchmark()

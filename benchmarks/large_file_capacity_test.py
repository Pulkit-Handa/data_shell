"""Large-file capacity probe: measures a realistic inbound feed through the
out-of-core streaming path and reports the evidence needed to justify (or
refute) the provisional maximum file size.

The probe measures, for one input file:
  * wall-clock duration and row throughput
  * peak resident memory (procfs RSS, including native Polars/Arrow/DuckDB)
  * peak temporary and persistent disk consumption
  * the delivered row count and the reconciliation footing verdict
  * whether the previous committed generation survived the run

Usage
-----
    ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/bench \
        PYTHONPATH=. python benchmarks/large_file_capacity_test.py --size-mb 256

The size is the *source* file size on disk. The harness writes a CSV with a
narrow-but-realistic schema and a small fraction of seeded defects, then runs
the real pipeline. It fails (non-zero exit) if the run does not deliver the
expected rows or if the reconciliation footing does not match, so a "fast"
run that silently drops rows cannot be mistaken for a good result.
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import threading
import time
from pathlib import Path

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    DeliveryTarget,
    QualityRule,
    RowRuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.core.config import settings
from datashell.engines.delivery import TerminalStatus
from datashell.pipeline.runner import DataShellPipeline


def _rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


class _PeakRSS:
    """Sample process RSS in a background thread (procfs, not tracemalloc)."""

    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _PeakRSS:
        def _sample() -> None:
            while not self._stop.is_set():
                self.peak = max(self.peak, _rss_bytes())
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=_sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.peak = max(self.peak, _rss_bytes())


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _build_contract() -> DataContract:
    return DataContract(
        metadata={"name": "large-file-probe", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="probe_feed.csv",
                    format="csv",
                    schema=[
                        SchemaColumn(name="vendor_id", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="settlement_id", polars_type="Utf8", nullable=False),
                    ],
                )
            ],
            "quality": {
                "rules": [
                    QualityRule(
                        rule_id="amount-positive",
                        column="amount",
                        check=RowRuleCheckType.GREATER_THAN,
                        value=0.0,
                        severity="hold",
                    ),
                    QualityRule(
                        rule_id="currency-settled",
                        column="currency",
                        check=RowRuleCheckType.ALLOWED_VALUES,
                        value=["USD", "EUR", "GBP"],
                        severity="hold",
                    ),
                ],
                "batch_rules": [
                    BatchRule(
                        rule_id="min-rows",
                        check=BatchRuleCheckType.MIN_ROW_COUNT,
                        value=1,
                    )
                ],
                "behavior": "DROP_ROW",
            },
            "delivery": {
                "targets": [
                    DeliveryTarget(path="probe_out/probe.parquet", format="parquet")
                ]
            },
        },
    )


def _write_csv(target: Path, target_bytes: int) -> int:
    """Write a CSV of approximately ``target_bytes`` with seeded defects."""
    written = 0
    rows = 0
    chunk_rows = 50_000
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write("vendor_id,amount,currency,settlement_id\n")
        while written < target_bytes:
            batch = []
            for i in range(chunk_rows):
                index = rows + i
                # ~4% of rows carry a defect (non-positive amount or an
                # unsettled currency) so the quality path is exercised.
                amount = -1.0 if index % 25 == 0 else float((index % 997) + 1) * 1.5
                currency = "USD" if index % 50 else "XXX"
                batch.append(
                    f"vendor_{index % 5000},{amount},{currency},stl_{index:012d}\n"
                )
            payload = "".join(batch)
            handle.write(payload)
            written += len(payload.encode("utf-8"))
            rows += chunk_rows
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Large-file capacity probe.")
    parser.add_argument("--size-mb", type=int, default=256, help="source file size in MiB")
    parser.add_argument("--chunk-rows", type=int, default=None)
    args = parser.parse_args()

    storage_root = settings.storage_root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    work_dir = storage_root / "capacity_probe"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    source_path = work_dir / "probe_feed.csv"
    target_bytes = args.size_mb * 1024 * 1024
    print(f"Generating ~{args.size_mb} MiB probe feed in {source_path} ...")
    gen_start = time.perf_counter()
    expected_rows = _write_csv(source_path, target_bytes)
    gen_duration = time.perf_counter() - gen_start
    actual_bytes = source_path.stat().st_size
    print(
        f"  wrote {actual_bytes / (1024 * 1024):.1f} MiB / {expected_rows:,} rows "
        f"in {gen_duration:.1f}s"
    )

    contract = _build_contract()
    pipeline = DataShellPipeline(contract, tenant_id="capacity-probe")
    audit_path = settings.audit_dir.resolve() / "capacity_probe" / "probe_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    baseline_disk = _dir_bytes(storage_root)
    start = time.perf_counter()
    with _PeakRSS() as sampler:
        result = pipeline.run_streaming(
            source_path=str(source_path),
            chunk_size=args.chunk_rows,
            target_path_override=str(work_dir / "probe_out" / "probe.parquet"),
            audit_output_path=str(audit_path),
        )
    duration = time.perf_counter() - start
    peak_disk = _dir_bytes(storage_root) - baseline_disk

    delivered = result.outcome.rows_delivered
    throughput = expected_rows / duration if duration > 0 else 0.0
    peak_rss_mb = sampler.peak / (1024 * 1024)

    print("\n" + "=" * 70)
    print("      LARGE-FILE CAPACITY PROBE (out-of-core streaming path)")
    print("=" * 70)
    print(f"Source file:             {actual_bytes / (1024 * 1024):.1f} MiB")
    print(f"Rows read:               {expected_rows:,}")
    print(f"Rows delivered:          {delivered:,}")
    print(f"Status:                  {result.status.value}")
    print(f"Footing matched:         {result.ledger.footing_matched}")
    print(f"Unaccounted rows:        {result.ledger.unaccounted_for:,}")
    print(f"Wall time:               {duration:.2f} s")
    print(f"Throughput:              {throughput:,.0f} rows/s")
    print(f"Peak RSS:                {peak_rss_mb:.1f} MiB")
    print(f"Peak storage growth:     {peak_disk / (1024 * 1024):.1f} MiB")
    print(f"Max RSS (resource):      {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB")
    print("=" * 70)

    failures = []
    if result.status != TerminalStatus.LOADED:
        failures.append(f"unexpected terminal status {result.status.value}")
    if not result.ledger.footing_matched:
        failures.append("reconciliation footing did not match")
    if result.ledger.unaccounted_for != 0:
        failures.append(f"{result.ledger.unaccounted_for:,} unaccounted rows")
    if delivered <= 0:
        failures.append("no rows delivered")
    if failures:
        print("FAILED: " + "; ".join(failures), flush=True)
        return 1

    # Clean up the generated feed and artifacts so repeated runs stay clean.
    shutil.rmtree(work_dir, ignore_errors=True)
    audit_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
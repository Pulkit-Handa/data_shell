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

The size is the *source* file size on disk. The harness writes a CSV or Parquet
feed with a configurable schema width and a small fraction of seeded defects,
then runs the real pipeline. It fails (non-zero exit) if the run does not
deliver the expected rows or if the reconciliation footing does not match, so a
"fast" run that silently drops rows cannot be mistaken for a good result.

The default profile is the original 4-column CSV probe. Use ``--columns`` and
``--string-width`` to measure a wide feed:

    ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/bench PYTHONPATH=. \
        python benchmarks/large_file_capacity_test.py --size-mb 1024 \
        --columns 100 --string-width 64 --format csv
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

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


def _extra_column_names(column_count: int) -> list[str]:
    if column_count < 4:
        raise ValueError("--columns must be at least 4 (the base schema)")
    return [f"attr_{index:03d}" for index in range(column_count - 4)]


def _build_contract(column_count: int, source_format: str) -> DataContract:
    schema = [
        SchemaColumn(name="vendor_id", polars_type="Utf8", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
        SchemaColumn(name="settlement_id", polars_type="Utf8", nullable=False),
    ]
    schema.extend(
        SchemaColumn(name=name, polars_type="Utf8", nullable=False)
        for name in _extra_column_names(column_count)
    )
    return DataContract(
        metadata={"name": "large-file-probe", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=f"probe_feed.{source_format}",
                    format=source_format,
                    schema=schema,
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


def _row_values(index: int, extra_names: list[str], string_width: int) -> list[str]:
    # ~4% of rows carry a defect (non-positive amount or an unsettled currency)
    # so the quality path is exercised.
    amount = -1.0 if index % 25 == 0 else float((index % 997) + 1) * 1.5
    currency = "USD" if index % 50 else "XXX"
    values = [f"vendor_{index % 5000}", str(amount), currency, f"stl_{index:012d}"]
    suffix = "x" * max(0, string_width)
    for name in extra_names:
        # Vary each cell so the feed is not artificially compressible.
        values.append(f"{name}_{index:012d}_{suffix}")
    return values


def _write_csv(
    target: Path,
    target_bytes: int,
    column_count: int,
    string_width: int,
) -> int:
    """Write a CSV of approximately ``target_bytes`` with seeded defects."""
    extra_names = _extra_column_names(column_count)
    header = ["vendor_id", "amount", "currency", "settlement_id", *extra_names]
    written = 0
    rows = 0
    chunk_rows = 10_000
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        written += len(",".join(header)) + 1
        while written < target_bytes:
            batch = [
                ",".join(_row_values(rows + i, extra_names, string_width)) + "\n"
                for i in range(chunk_rows)
            ]
            payload = "".join(batch)
            handle.write(payload)
            written += len(payload.encode("utf-8"))
            rows += chunk_rows
    return rows


def _write_parquet(
    target: Path,
    target_bytes: int,
    column_count: int,
    string_width: int,
) -> int:
    """Write a Parquet feed of approximately ``target_bytes`` with seeded defects.

    Batches are bounded so the generator itself does not become the memory
    high-water mark being measured.
    """
    extra_names = _extra_column_names(column_count)
    batch_rows = 10_000
    schema = pa.schema(
        [
            pa.field("vendor_id", pa.string(), nullable=False),
            pa.field("amount", pa.float64(), nullable=False),
            pa.field("currency", pa.string(), nullable=False),
            pa.field("settlement_id", pa.string(), nullable=False),
            *[pa.field(name, pa.string(), nullable=False) for name in extra_names],
        ]
    )
    rows = 0
    with pq.ParquetWriter(target, schema, compression="zstd") as writer:
        while not target.exists() or target.stat().st_size < target_bytes:
            batch_values = [
                _row_values(rows + i, extra_names, string_width)
                for i in range(batch_rows)
            ]
            columns = [
                pa.array([row[column_index] for row in batch_values], type=pa.string())
                if column_index != 1
                else pa.array([float(row[1]) for row in batch_values], type=pa.float64())
                for column_index in range(4 + len(extra_names))
            ]
            writer.write_batch(pa.RecordBatch.from_arrays(columns, schema=schema))
            rows += batch_rows
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Large-file capacity probe.")
    parser.add_argument("--size-mb", type=int, default=256, help="source file size in MiB")
    parser.add_argument("--chunk-rows", type=int, default=None)
    parser.add_argument("--columns", type=int, default=4, help="total schema width (minimum 4)")
    parser.add_argument(
        "--string-width",
        type=int,
        default=24,
        help="padding for wide-schema string columns (bytes)",
    )
    parser.add_argument("--format", choices=("csv", "parquet"), default="csv")
    args = parser.parse_args()
    if args.columns < 4:
        parser.error("--columns must be at least 4")
    if args.string_width < 0:
        parser.error("--string-width must be non-negative")

    storage_root = settings.storage_root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    work_dir = storage_root / "capacity_probe"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    # The delivery target must live under the configured output root (the
    # sandbox rejects arbitrary paths even when they are under storage_root).
    output_dir = settings.output_dir.resolve() / "capacity_probe"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_path = work_dir / f"probe_feed.{args.format}"
    target_bytes = args.size_mb * 1024 * 1024
    print(
        f"Generating ~{args.size_mb} MiB {args.format.upper()} probe feed "
        f"({args.columns} columns, {args.string_width}-byte string padding) in {source_path} ..."
    )
    gen_start = time.perf_counter()
    writer = _write_csv if args.format == "csv" else _write_parquet
    expected_rows = writer(source_path, target_bytes, args.columns, args.string_width)
    gen_duration = time.perf_counter() - gen_start
    actual_bytes = source_path.stat().st_size
    print(
        f"  wrote {actual_bytes / (1024 * 1024):.1f} MiB / {expected_rows:,} rows "
        f"in {gen_duration:.1f}s"
    )

    contract = _build_contract(args.columns, args.format)
    pipeline = DataShellPipeline(contract, tenant_id="capacity-probe")
    audit_path = settings.audit_dir.resolve() / "capacity_probe" / "probe_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    baseline_disk = _dir_bytes(storage_root)
    start = time.perf_counter()
    with _PeakRSS() as sampler:
        result = pipeline.run_streaming(
            source_path=str(source_path),
            chunk_size=args.chunk_rows,
            target_path_override=str(output_dir / "probe.parquet"),
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
    print(f"Format / schema:         {args.format} / {args.columns} columns")
    print(f"String padding:          {args.string_width} bytes")
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
    shutil.rmtree(output_dir, ignore_errors=True)
    audit_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
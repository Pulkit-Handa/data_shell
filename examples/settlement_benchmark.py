#!/usr/bin/env python3
"""Settlement vs. ingestion benchmark.

Measures, on this machine:
  * end-to-end pipeline time for a 100,000-row feed (ingest + quality +
    reconciliation + signed certificate + delivery), and
  * settlement calculation, receipt issuance, and receipt verification
    latency and peak Python memory at 100k and 1,000,000 qualified units.

The settlement engine consumes only compact run summaries (counts and
digests), so its cost must be independent of feed size; this script prints the
measured numbers rather than asserting a claim.

Usage:
    python examples/settlement_benchmark.py [--rows 100000] [--iterations 200]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_STORAGE = REPO_ROOT / "output" / "settlement_benchmark"
DATA_CONTRACT_PATH = REPO_ROOT / "examples" / "vendor_settlements_contract.json"


def _configure_environment(storage: Path) -> None:
    values = {
        "DATASHELL_STORAGE_ROOT": storage,
        "DATASHELL_OUTPUT_DIR": storage / "output",
        "DATASHELL_DATA_DIR": storage / "data",
        "DATASHELL_CONTRACTS_DIR": storage / "contracts_registry",
        "DATASHELL_AUDIT_DIR": storage / "output" / "audits",
        "DATASHELL_QUARANTINE_DIR": storage / "output" / "quarantine",
        "DATASHELL_HMAC_SECRET": "settlement-benchmark-hmac-secret-0123456789",
        "TMPDIR": storage / "tmp",
    }
    for name, value in values.items():
        os.environ[name] = str(value)
    for sub in ("output", "output/audits", "output/quarantine", "data",
                "contracts_registry", "tmp"):
        (storage / sub).mkdir(parents=True, exist_ok=True)


def _generate_feed(storage: Path, total: int) -> Path:
    refused = max(1, round(total * 0.01))
    held = max(1, round(total * 0.005))
    delivered = total - refused - held
    source = storage / f"bench_feed_{total}.jsonl"
    amounts = [f"{100 + (i % 900)}.{i % 100:02d}" for i in range(1000)]
    with open(source, "w", encoding="utf-8") as handle:
        for i in range(delivered):
            handle.write('{"settlement_id": "SET-%09d", "amount": %s, "currency": "USD"}\n'
                         % (i, amounts[i % len(amounts)]))
        for i in range(held):
            handle.write('{"settlement_id": "HOLD-%09d", "amount": -12.50, "currency": "USD"}\n' % i)
        for i in range(refused):
            handle.write('{"settlement_id": "REJ-%09d", "amount": "NOT_A_FLOAT", "currency": "USD"}\n' % i)
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    args = parser.parse_args(argv)

    storage = args.storage.resolve()
    if storage.exists():
        shutil.rmtree(storage)
    _configure_environment(storage)

    from datashell.contract.models import DataContract
    from datashell.core.ledger import BoundaryLedger
    from datashell.pipeline.runner import DataShellPipeline
    from datashell.settlement.certificate import (
        build_settlement_receipt,
        verify_settlement_receipt,
    )
    from datashell.settlement.engine import calculate_settlement
    from datashell.settlement.models import CommercialContract, SettlementStatement

    source = _generate_feed(storage, args.rows)
    audit = storage / "output" / "audits" / "bench_audit.json"
    target = storage / "output" / "bench_delivered.parquet"
    contract = DataContract(**json.loads(DATA_CONTRACT_PATH.read_text()))

    print("=" * 70)
    print("  SETTLEMENT VS. INGESTION BENCHMARK")
    print("=" * 70)
    print(f"  python {platform.python_version()} on {platform.platform()}")

    # ---------------------------------------------------------------- ingestion
    pipeline = DataShellPipeline(contract, tenant_id="bench", drift_enabled=False)
    started = time.perf_counter()
    result = pipeline.run_file(
        source_path=str(source), target_path_override=str(target),
        audit_output_path=str(audit))
    ingest_seconds = time.perf_counter() - started
    ledger = result.ledger
    print()
    print(f"  Pipeline (ingest+quality+reconcile+certificate+deliver):")
    print(f"    rows={ledger.rows_read:,} delivered={ledger.actual_delivered:,} "
          f"held={ledger.held_quality:,} refused={ledger.refused_structural:,}")
    print(f"    wall time: {ingest_seconds:.2f}s "
          f"({ledger.rows_read / ingest_seconds:,.0f} rows/s)")
    print(f"    source size: {source.stat().st_size / 1e6:.1f} MB")

    # ---------------------------------------------------------------- settlement
    boundary = BoundaryLedger(storage / "output" / "datashell_history.db")
    run_record = boundary.get_run(result.run_id, "bench")
    cert_data = json.loads(audit.read_text())

    demo_contract = CommercialContract(
        contract_id="ctr_bench", contract_version=1, buyer_id="b", seller_id="s",
        feed_id="vendor-settlements", currency="USD",
        pricing={"model": "PER_ACCEPTED_UNIT", "unit_price_minor": 5},
        acceptance={"policy": "PARTIAL_WITH_MINIMUM", "minimum_accepted_units": 1,
                    "expected_units": ledger.actual_delivered},
        effective_at="2026-01-01T00:00:00Z")

    results = {}
    for label, units in (("100k", ledger.actual_delivered), ("1M", 1_000_000)):
        fabric = dict(run_record)
        fabric["rows_delivered"] = units
        fabric["rows_read"] = units

        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(args.iterations):
            statement = calculate_settlement(
                tenant_id="bench", contract=demo_contract, ledger_run=fabric,
                certificate_digest="0" * 64, certificate_valid=True, actor="bench")
        calculate_seconds = time.perf_counter() - started
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        receipts = [build_settlement_receipt(statement, cert_data)
                    for _ in range(args.iterations)]
        started = time.perf_counter()
        for receipt in receipts:
            verify_settlement_receipt(receipt, delivery_certificate=cert_data)
        verify_seconds = time.perf_counter() - started

        results[label] = {
            "qualified_units": units,
            "gross_amount_minor": statement.calculation.gross_amount_minor,
            "calculate_mean_ms": 1000 * calculate_seconds / args.iterations,
            "verify_mean_ms": 1000 * verify_seconds / args.iterations,
            "python_peak_bytes": peak,
        }
        print()
        print(f"  Settlement at {label} qualified units ({args.iterations} iterations):")
        print(f"    calculate mean: {results[label]['calculate_mean_ms']:.3f} ms "
              f"(python peak {peak / 1024:.1f} KiB)")
        print(f"    verify mean:    {results[label]['verify_mean_ms']:.3f} ms")
        print(f"    gross:          {statement.calculation.gross_amount_minor:,} minor units")

    independence = (abs(results["100k"]["calculate_mean_ms"]
                        - results["1M"]["calculate_mean_ms"])
                    < 0.05 + 0.5 * min(results["100k"]["calculate_mean_ms"],
                                       results["1M"]["calculate_mean_ms"]))
    print()
    print(f"  Settlement cost independent of feed size (within tolerance): {independence}")

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pipeline": {
            "rows": ledger.rows_read,
            "delivered": ledger.actual_delivered,
            "seconds": ingest_seconds,
            "rows_per_second": ledger.rows_read / ingest_seconds,
            "source_mb": source.stat().st_size / 1e6,
        },
        "settlement": results,
        "settlement_scale_independent": independence,
    }
    report_path = storage / "benchmark_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"  Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

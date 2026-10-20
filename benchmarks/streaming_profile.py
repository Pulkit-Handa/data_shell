"""Streaming-path profile: wall time, throughput, and peak RSS for the out-of-core backend.

Generates a synthetic settlement feed (Parquet or CSV), then re-executes itself in a
fresh child process so the reported peak RSS is the pipeline's own high-water mark
rather than the generator's. The child asserts the reconciliation footing
(read = delivered + held + refused, zero unaccounted) before reporting, so a run that
silently drops rows fails loudly instead of printing a flattering number.

Usage:
    python benchmarks/streaming_profile.py --rows 5000000 --format parquet
    python benchmarks/streaming_profile.py --rows 5000000 --format csv

What the two formats measure:
- Parquet feeds flow through the vectorized fast path (typed Arrow chunks), so this
  profile is the ceiling for the out-of-core backend.
- CSV feeds flow through the strict per-row caster by design — chunk columns arrive
  as strings and the Tier-1 engine falls back to row-by-row casting to preserve exact
  per-row rejection diagnostics — so expect materially lower throughput and higher
  allocator churn.

DATASHELL_INGEST_MODE is forced to "stream" in the child so the measurement is the
out-of-core backend regardless of the auto-promotion threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONTRACT = REPO_ROOT / "examples" / "vendor_settlements_contract.json"


def generate_feed(path: Path, rows: int, fmt: str) -> None:
    """Generate a synthetic settlement feed matching the example contract schema."""
    frame = pl.select(
        settlement_id=pl.int_range(0, rows, dtype=pl.Int64).cast(pl.Utf8),
        amount=(pl.int_range(0, rows) * 37 % 1000 / 4.0 + 100.0),
        currency=pl.Series(["USD", "EUR", "GBP"]).gather(pl.int_range(0, rows) % 3),
    )
    if fmt == "parquet":
        frame.write_parquet(path)
    else:
        frame.write_csv(path)


def run_child(rows: int, fmt: str, feed: Path) -> dict[str, Any]:
    """Run the pipeline in a fresh child process so ru_maxrss is the pipeline's own peak.

    The parent pins DATASHELL_INGEST_MODE=stream in the child's environment, so
    settings see the forced backend before any module binds them.
    """
    from datashell.contract.models import DataContract
    from datashell.core.config import settings
    from datashell.pipeline.runner import DataShellPipeline

    contract = DataContract.model_validate(json.loads(EXAMPLE_CONTRACT.read_text()))
    out_dir = settings.output_dir.resolve() / "streaming_profile"
    audit_dir = settings.audit_dir.resolve() / "streaming_profile"
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)

    pipeline = DataShellPipeline(contract, tenant_id="benchmark-tenant", drift_enabled=False)
    start = time.perf_counter()
    result = pipeline.run_file(
        source_path=str(feed),
        target_path_override=str(out_dir / "streamed_curated.parquet"),
        audit_output_path=str(audit_dir / "streamed_audit_certificate.json"),
    )
    wall = time.perf_counter() - start

    ledger = result.ledger
    delivered = result.outcome.rows_delivered
    footing_ok = (
        result.status.value == "LOADED"
        and ledger.rows_read == rows
        and ledger.unaccounted_for == 0
        and ledger.footing_matched
        and delivered == rows
    )
    if not footing_ok:
        print(
            f"streaming profile FAILED: status={result.status.value} read={ledger.rows_read} "
            f"delivered={delivered} unaccounted={ledger.unaccounted_for}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return {
        "rows": rows,
        "format": fmt,
        "feed_mib": round(feed.stat().st_size / 1024 / 1024, 1),
        "wall_seconds": round(wall, 2),
        "rows_per_second": int(rows / wall),
        "rows_delivered": delivered,
        "unaccounted_for": ledger.unaccounted_for,
        "footing_matched": ledger.footing_matched,
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    parser.add_argument("--run", action="store_true", help="internal: child execution mode")
    args = parser.parse_args()

    if args.run:
        feed = Path(os.environ["_STREAMING_PROFILE_FEED"])
        report = run_child(args.rows, args.format, feed)
        print(json.dumps(report))
        return 0

    with tempfile.TemporaryDirectory(prefix="datashell_stream_profile_") as tmp:
        feed = Path(tmp) / f"feed_{args.rows}.{args.format}"
        generate_feed(feed, args.rows, args.format)
        env = dict(os.environ, _STREAMING_PROFILE_FEED=str(feed), DATASHELL_INGEST_MODE="stream")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--run", "--rows", str(args.rows),
             "--format", args.format],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            return proc.returncode
        report = json.loads(proc.stdout.strip().splitlines()[-1])

    print("=" * 72)
    print("      DATA SHELL — OUT-OF-CORE STREAMING PROFILE")
    print("=" * 72)
    print(f"Feed:              {report['rows']:,} rows, {report['format']} ({report['feed_mib']} MiB)")
    print("Backend:           out-of-core (DATASHELL_INGEST_MODE=stream)")
    print(f"Wall time:         {report['wall_seconds']}s")
    print(f"Throughput:        {report['rows_per_second']:,} rows/s")
    print(f"Delivered:         {report['rows_delivered']:,}")
    print(f"Unaccounted:       {report['unaccounted_for']} (footing matched: "
          f"{report['footing_matched']})")
    print(f"Peak RSS:          {report['peak_rss_mib']} MiB (pipeline high-water mark)")
    print("=" * 72)
    print("Parquet flows through the vectorized fast path; CSV streams through the")
    print("strict per-row caster by design (exact per-row rejection diagnostics).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

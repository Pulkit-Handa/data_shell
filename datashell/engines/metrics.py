"""Per-feed metric extraction for cross-period drift baselines.

A *feed metric* is a deterministic, per-column aggregate over the admitted
(delivered) dataset. Metrics are persisted in the boundary ledger and compared
run-over-run by the drift engine, so they must be computed identically whether a
run executed in-memory, from a file, or out-of-core.

We intentionally keep the metric set small and stable — a null rate for every
declared column, plus mean and sum for numeric columns. That is enough to catch
the failure mode drift exists for (a feed whose values silently shift period over
period) without generating spurious alerts from volatile cardinality statistics.

The ``<kind>_<column>`` naming below is the *contract* that lets the drift
engine determine a metric's semantics; ``metric_kind`` is the single source of
truth for that mapping. New metric families must register here (and declare
their comparison policy in the drift engine) rather than being guessed from
column names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

# Metric-name prefixes produced by this module. The drift engine imports these
# instead of re-declaring the convention.
RATE_METRIC_PREFIX = "null_rate_"   # proportion in [0.0, 1.0]
MEAN_METRIC_PREFIX = "avg_"         # arithmetic mean of a numeric column
SUM_METRIC_PREFIX = "sum_"          # sum of a numeric column

RATE = "rate"
MEAN = "mean"
SUM = "sum"
UNKNOWN = "unknown"


def metric_kind(name: str) -> str:
    """Classify a feed metric by its declared naming contract.

    Returns one of ``rate`` (proportion in [0, 1]), ``mean``, ``sum``, or
    ``unknown`` for metric names outside this convention (for example metrics
    persisted by future or extended configurations).
    """
    if name.startswith(RATE_METRIC_PREFIX):
        return RATE
    if name.startswith(MEAN_METRIC_PREFIX):
        return MEAN
    if name.startswith(SUM_METRIC_PREFIX):
        return SUM
    return UNKNOWN


def _metric_expressions(schema: dict[str, pl.DataType]) -> list[pl.Expr]:
    """Build the deterministic metric expressions for a column schema."""
    exprs: list[pl.Expr] = []
    for name, dtype in schema.items():
        # Null rate is meaningful for every column type.
        exprs.append(pl.col(name).is_null().mean().alias(f"null_rate_{name}"))
        if dtype.is_numeric():
            exprs.append(pl.col(name).mean().alias(f"avg_{name}"))
            exprs.append(pl.col(name).sum().alias(f"sum_{name}"))
    return exprs


def _floats_from_row(row: dict[str, object]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for name, value in row.items():
        if value is None:
            continue
        # Polars hands back the named-row values as dynamically typed objects
        # (scalar wrappers around the column dtype); converting through Any
        # mirrors their runtime duck-typing without weakening the result type.
        candidate: Any = value
        try:
            metrics[name] = float(candidate)
        except (TypeError, ValueError):
            continue
    return metrics


def compute_metrics(df: pl.DataFrame) -> dict[str, float]:
    """Compute feed metrics for an in-memory (admitted) dataframe."""
    if df.height == 0 or not df.columns:
        return {}
    exprs = _metric_expressions({name: dtype for name, dtype in zip(df.columns, df.dtypes, strict=True)})
    if not exprs:
        return {}
    return _floats_from_row(df.select(exprs).row(0, named=True))


def compute_metrics_from_parquet(
    path: str | Path,
    *,
    max_cells_per_batch: int = 1_000_000,
) -> dict[str, float]:
    """Compute feed metrics out-of-core from a delivered/lined-up Parquet file.

    A single projection containing one expression per column is not actually
    bounded for wide feeds: Polars may decode many columns concurrently, and a
    100-column x 4M-row file can add multiple gigabytes to RSS even though the
    result is one row. Scan the file in row batches sized by the cell budget and
    accumulate the same aggregates, so peak memory is bounded by one batch
    rather than by the file or schema width.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    if max_cells_per_batch < 1:
        raise ValueError("max_cells_per_batch must be a positive integer")

    lf = pl.scan_parquet(str(p))
    schema = lf.collect_schema()
    columns = list(schema.items())
    if not columns:
        return {}

    numeric_columns = {
        name for name, dtype in columns if dtype.is_numeric()
    }
    null_counts: dict[str, int] = {name: 0 for name, _ in columns}
    sums: dict[str, float] = {name: 0.0 for name in numeric_columns}
    non_null_counts: dict[str, int] = {name: 0 for name in numeric_columns}
    total_rows = 0

    exprs: list[pl.Expr] = []
    for name, _dtype in columns:
        exprs.append(pl.col(name).is_null().sum().alias(f"__nulls_{name}"))
        if name in numeric_columns:
            exprs.append(pl.col(name).sum().alias(f"__sum_{name}"))
            exprs.append(pl.col(name).count().alias(f"__count_{name}"))

    # Keep the batch bounded by cells, not rows: 1M cells is ~10k rows for a
    # 100-column feed and ~250k rows for a 4-column feed.
    batch_rows = max(1, max_cells_per_batch // len(columns))
    parquet_file = pq.ParquetFile(p)
    for batch in parquet_file.iter_batches(batch_size=batch_rows):
        df = pl.from_arrow(batch)
        if not isinstance(df, pl.DataFrame):
            raise TypeError("Parquet record batch did not convert to a DataFrame")
        if df.height == 0:
            continue
        total_rows += df.height
        row = df.select(exprs).row(0, named=True)
        for name, _dtype in columns:
            null_counts[name] += int(row[f"__nulls_{name}"] or 0)
            if name in numeric_columns:
                value = row[f"__sum_{name}"]
                if value is not None:
                    sums[name] += float(value)
                non_null_counts[name] += int(row[f"__count_{name}"] or 0)

    if total_rows == 0:
        return {}
    metrics: dict[str, float] = {}
    for name, _dtype in columns:
        metrics[f"null_rate_{name}"] = null_counts[name] / total_rows
        if name in numeric_columns:
            # Polars returns 0.0 (not null) for an all-null numeric sum, so keep
            # the sum metric present and only omit the mean when count is zero.
            if non_null_counts[name] > 0:
                metrics[f"avg_{name}"] = sums[name] / non_null_counts[name]
            metrics[f"sum_{name}"] = sums[name]
    return metrics

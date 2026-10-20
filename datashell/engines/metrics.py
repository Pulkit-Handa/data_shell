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


def compute_metrics_from_parquet(path: str | Path) -> dict[str, float]:
    """Compute feed metrics out-of-core from a delivered/lined-up Parquet file.

    Uses a lazy scan so multi-gigabyte outputs are aggregated in bounded memory,
    mirroring the in-memory metric definitions exactly.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    lf = pl.scan_parquet(str(p))
    schema = lf.collect_schema()
    exprs = _metric_expressions({name: dtype for name, dtype in schema.items()})
    if not exprs:
        return {}
    return _floats_from_row(lf.select(exprs).collect().row(0, named=True))

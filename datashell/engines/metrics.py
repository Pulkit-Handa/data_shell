"""Per-feed metric extraction for cross-period drift baselines.

A *feed metric* is a deterministic, per-column aggregate over the admitted
(delivered) dataset. Metrics are persisted in the boundary ledger and compared
run-over-run by the drift engine, so they must be computed identically whether a
run executed in-memory, from a file, or out-of-core.

We intentionally keep the metric set small and stable — a null rate for every
declared column, plus mean and sum for numeric columns. That is enough to catch
the failure mode drift exists for (a feed whose values silently shift period over
period) without generating spurious alerts from volatile cardinality statistics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Union

import polars as pl


def _metric_expressions(schema: Dict[str, pl.DataType]) -> List[pl.Expr]:
    """Build the deterministic metric expressions for a column schema."""
    exprs: List[pl.Expr] = []
    for name, dtype in schema.items():
        # Null rate is meaningful for every column type.
        exprs.append(pl.col(name).is_null().mean().alias(f"null_rate_{name}"))
        if dtype.is_numeric():
            exprs.append(pl.col(name).mean().alias(f"avg_{name}"))
            exprs.append(pl.col(name).sum().alias(f"sum_{name}"))
    return exprs


def _floats_from_row(row: Dict[str, object]) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    for name, value in row.items():
        if value is None:
            continue
        try:
            metrics[name] = float(value)
        except (TypeError, ValueError):
            continue
    return metrics


def compute_metrics(df: pl.DataFrame) -> Dict[str, float]:
    """Compute feed metrics for an in-memory (admitted) dataframe."""
    if df.height == 0 or not df.columns:
        return {}
    exprs = _metric_expressions({name: dtype for name, dtype in zip(df.columns, df.dtypes)})
    if not exprs:
        return {}
    return _floats_from_row(df.select(exprs).row(0, named=True))


def compute_metrics_from_parquet(path: Union[str, Path]) -> Dict[str, float]:
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

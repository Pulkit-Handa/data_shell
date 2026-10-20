"""Tier 2: Inside — Semantic Quality Enforcement Engine (Polars & DuckDB).

Evaluates:
- Row-level rules in Polars (columnar, compiled once).
- Batch-level rules in DuckDB (out-of-core aggregate validation, including on empty sets).
- Strict quality behaviors: DROP_ROW, REJECT_RUN, WARN.
- Preserves stable rule_id attribution on every quarantined record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from datashell.contract.compiler import BatchRuleCompiler, RuleCompiler
from datashell.contract.models import DataContract, QualityBehavior, QualityRule
from datashell.core.config import settings


@dataclass
class RuleViolationCount:
    rule_id: str
    description: str | None
    violation_count: int


@dataclass
class BatchRuleResult:
    rule_id: str
    check: str
    passed: bool
    actual_value: str
    expected_value: str
    description: str | None


@dataclass
class QualityResult:
    admitted_df: pl.DataFrame
    held_df: pl.DataFrame
    total_candidate_rows: int
    admitted_count: int
    held_count: int
    rule_violation_counts: dict[str, int]
    batch_results: list[BatchRuleResult]
    all_batch_rules_passed: bool
    should_reject_run: bool
    behavior: QualityBehavior


class SemanticQualityEngine:
    """
    Tier 2 gatekeeper: Vectorized row evaluation via Polars expressions,
    followed by DuckDB batch/aggregate validations over the dataset.
    """

    def __init__(self, contract: DataContract):
        self.contract = contract
        self.quality_spec = contract.spec.quality
        self.behavior = self.quality_spec.behavior
        self.compiled_row_rules: list[tuple[QualityRule, pl.Expr]] = [
            (r, RuleCompiler.compile_row_rule(r))
            for r in self.quality_spec.rules
        ]

    def evaluate(self, candidate_df: pl.DataFrame, evaluate_batch: bool = True) -> QualityResult:
        """
        Executes columnar row checks and DuckDB batch rules on candidate dataframe.
        """
        total_rows = candidate_df.height
        clean_cols = [c for c in candidate_df.columns]

        # 1. Row-level evaluation in Polars
        rule_violation_counts: dict[str, int] = {}
        rule_pass_cols = []

        if total_rows > 0 and self.compiled_row_rules:
            expr_list = []
            for rule, compiled_expr in self.compiled_row_rules:
                pass_col_name = f"__dm_pass_{rule.rule_id}"
                rule_pass_cols.append((rule.rule_id, pass_col_name))
                expr_list.append(compiled_expr.fill_null(False).alias(pass_col_name))

            evaluated_df = candidate_df.with_columns(expr_list)
            all_pass_expr = pl.all_horizontal([pl.col(c) for _, c in rule_pass_cols])
            evaluated_df = evaluated_df.with_columns(all_pass_expr.alias("__dm_passed_all"))

            for r_id, col_name in rule_pass_cols:
                failed_count = evaluated_df.filter(~pl.col(col_name)).height
                rule_violation_counts[r_id] = failed_count

            # Apply Declared Behavior Semantics
            if self.behavior == QualityBehavior.WARN:
                # In WARN mode: violations are recorded, but rows are NOT dropped from delivery
                admitted_df = candidate_df
                held_df = candidate_df.clear().with_columns(
                    pl.lit("").cast(pl.String).alias("__dm_violated_rule_id")
                )
            else:
                # DROP_ROW or REJECT_RUN: Separate conforming from failing
                admitted_df = evaluated_df.filter(pl.col("__dm_passed_all")).select(clean_cols)
                held_raw = evaluated_df.filter(~pl.col("__dm_passed_all"))

                if held_raw.height > 0:
                    chained_expr = pl.lit("UNKNOWN")
                    for r_id, col_name in reversed(rule_pass_cols):
                        chained_expr = pl.when(~pl.col(col_name)).then(pl.lit(r_id)).otherwise(chained_expr)

                    held_df = (
                        held_raw
                        .with_columns(chained_expr.alias("__dm_violated_rule_id"))
                        .select(clean_cols + ["__dm_violated_rule_id"])
                    )
                else:
                    held_df = candidate_df.clear().with_columns(
                        pl.lit("").cast(pl.String).alias("__dm_violated_rule_id")
                    )

        else:
            # Zero rows or zero rules
            admitted_df = candidate_df
            held_df = candidate_df.clear().with_columns(
                pl.lit("").cast(pl.String).alias("__dm_violated_rule_id")
            )
            for r, _ in self.compiled_row_rules:
                rule_violation_counts[r.rule_id] = 0

        # 2. Batch-level evaluation in DuckDB
        if evaluate_batch:
            batch_results, all_batch_passed = self.evaluate_batch_rules(admitted_df)
        else:
            batch_results = []
            all_batch_passed = True

        # Determine if run must be rejected according to declared contract behavior
        should_reject_run = False
        # REJECT_RUN: ANY row failure OR batch failure rejects the run.
        # DROP_ROW: a batch failure still stops delivery (failing rows were already dropped).
        if (
            (self.behavior == QualityBehavior.REJECT_RUN and (held_df.height > 0 or not all_batch_passed))
            or (self.behavior == QualityBehavior.DROP_ROW and not all_batch_passed)
        ):
            should_reject_run = True

        return QualityResult(
            admitted_df=admitted_df,
            held_df=held_df,
            total_candidate_rows=total_rows,
            admitted_count=admitted_df.height,
            held_count=held_df.height,
            rule_violation_counts=rule_violation_counts,
            batch_results=batch_results,
            all_batch_rules_passed=all_batch_passed,
            should_reject_run=should_reject_run,
            behavior=self.behavior
        )

    def evaluate_batch_rules(
        self,
        dataset_source: pl.DataFrame | Path | str | Any,
        fallback_arrow_schema: Any = None
    ) -> tuple[list[BatchRuleResult], bool]:
        """
        Evaluates batch rules in DuckDB over an Arrow table, Polars DataFrame, or out-of-core Parquet file.
        Uses pyarrow.dataset to stream Parquet data on demand without materializing the full dataset into RAM.

        DuckDB is an optional dependency (``pip install datashell[duckdb]``); contracts
        that only use row rules run fine without it.
        """
        batch_results: list[BatchRuleResult] = []
        all_batch_passed = True

        if not self.quality_spec.batch_rules:
            return batch_results, all_batch_passed

        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ImportError(
                "Batch rules require the DuckDB engine. Install it with: pip install datashell[duckdb]"
            ) from exc

        con = duckdb.connect(
            database=":memory:",
            config={
                "enable_external_access": False,
                "memory_limit": settings.duckdb_memory_limit,
                "threads": settings.duckdb_threads,
                "temp_directory": str(settings.temp_dir),
            }
        )
        try:
            con.execute("SET preserve_insertion_order = false")

            # Register source as Arrow dataset/table
            if isinstance(dataset_source, pl.DataFrame):
                arrow_table = dataset_source.to_arrow()
                con.register("admitted_dataset", arrow_table)
            elif isinstance(dataset_source, (Path, str)):
                p = Path(dataset_source)
                if p.exists() and p.stat().st_size > 0:
                    import pyarrow.dataset as ds
                    arrow_ds = ds.dataset(str(p.resolve()))
                    con.register("admitted_dataset", arrow_ds)
                elif fallback_arrow_schema is not None:
                    import pyarrow as pa
                    empty_table = pa.Table.from_batches([], schema=fallback_arrow_schema)
                    con.register("admitted_dataset", empty_table)
                else:
                    con.execute("CREATE VIEW admitted_dataset AS SELECT 1 AS dummy WHERE 1=0")
            else:
                con.register("admitted_dataset", dataset_source)

            for b_rule in self.quality_spec.batch_rules:
                sql_check = BatchRuleCompiler.compile_batch_sql(b_rule, "admitted_dataset")
                rows = con.execute(sql_check).fetchall()
                if not rows or len(rows) != 1 or len(rows[0]) < 3:
                    raise ValueError(f"Batch rule '{b_rule.rule_id}' did not evaluate to exactly 1 scalar result row")
                res = rows[0]
                passed = bool(res[0])
                actual_val = str(res[1]) if res[1] is not None else "NULL"
                expected_val = str(res[2]) if res[2] is not None else "UNKNOWN"

                batch_results.append(BatchRuleResult(
                    rule_id=b_rule.rule_id,
                    check=b_rule.check.value,
                    passed=passed,
                    actual_value=actual_val,
                    expected_value=expected_val,
                    description=b_rule.description
                ))
                if not passed:
                    all_batch_passed = False

            return batch_results, all_batch_passed
        finally:
            con.close()


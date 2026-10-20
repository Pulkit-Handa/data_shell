"""Compiler for contract rules into vectorized Polars expressions and DuckDB SQL queries."""

from typing import Any

import polars as pl

from datashell.contract.models import BatchRule, BatchRuleCheckType, QualityRule, RowRuleCheckType


def parse_polars_type(type_str: str) -> pl.DataType:
    """Map string representation in contract to Polars DataType."""
    normalized = type_str.strip().lower()
    mapping = {
        "utf8": pl.String(),
        "string": pl.String(),
        "str": pl.String(),
        "int64": pl.Int64(),
        "int32": pl.Int32(),
        "int16": pl.Int16(),
        "int8": pl.Int8(),
        "uint64": pl.UInt64(),
        "uint32": pl.UInt32(),
        "float64": pl.Float64(),
        "float32": pl.Float32(),
        "boolean": pl.Boolean(),
        "bool": pl.Boolean(),
        "date": pl.Date(),
        "datetime": pl.Datetime(),
    }
    if normalized in mapping:
        return mapping[normalized]
    # Fallback to getattr if standard polars type
    if hasattr(pl, type_str):
        attr = getattr(pl, type_str)
        if isinstance(attr, pl.DataType) or (isinstance(attr, type) and issubclass(attr, pl.DataType)):
            value = attr() if callable(attr) else attr
            if isinstance(value, pl.DataType):
                return value
    raise ValueError(f"Unsupported Polars type in contract: '{type_str}'")


class RuleCompiler:
    """Compiles row rules into Polars Boolean expressions."""

    @staticmethod
    def compile_row_rule(rule: QualityRule) -> pl.Expr:
        """
        Returns a Polars expression that evaluates to TRUE when the record passes
        and FALSE (or null) when it violates the rule.

        The contract model already rejects rules that do not carry a ``column``
        (see ``QualityRule`` validation); the assertion keeps the type checker
        honest without weakening runtime behavior for CUSTOM_EXPR.
        """
        if rule.check != RowRuleCheckType.CUSTOM_EXPR and not rule.column:
            raise ValueError(f"Rule {rule.rule_id} with check {rule.check} requires a target column")
        # The guard above guarantees ``rule.column`` for every branch that
        # reaches ``col``; narrowing with an explicit local keeps the type
        # checker honest without changing runtime behavior.
        # The guard above guarantees a real column for every branch that uses
        # ``col``; the sentinel only keeps the CUSTOM_EXPR path (which never
        # touches ``col``) well-typed at runtime.
        col: pl.Expr = pl.col(rule.column) if rule.column else pl.col("__never__")

        if rule.check == RowRuleCheckType.NOT_NULL:
            return col.is_not_null()

        elif rule.check == RowRuleCheckType.GREATER_THAN:
            return col.is_not_null() & (col > rule.value)

        elif rule.check == RowRuleCheckType.GREATER_EQUAL:
            return col.is_not_null() & (col >= rule.value)

        elif rule.check == RowRuleCheckType.LESS_THAN:
            return col.is_not_null() & (col < rule.value)

        elif rule.check == RowRuleCheckType.LESS_EQUAL:
            return col.is_not_null() & (col <= rule.value)

        elif rule.check == RowRuleCheckType.EQUALS:
            return col.is_not_null() & (col == rule.value)

        elif rule.check == RowRuleCheckType.NOT_EQUALS:
            return col.is_not_null() & (col != rule.value)

        elif rule.check == RowRuleCheckType.ALLOWED_VALUES:
            allowed = rule.value if isinstance(rule.value, list) else [rule.value]
            return col.is_not_null() & col.is_in(allowed)

        elif rule.check == RowRuleCheckType.REGEX_MATCH:
            pattern = rule.value if isinstance(rule.value, str) else ""
            return col.is_not_null() & col.str.contains(pattern)

        elif rule.check == RowRuleCheckType.BETWEEN:
            if not (isinstance(rule.value, (list, tuple)) and len(rule.value) == 2):
                raise ValueError(f"Rule {rule.rule_id} check BETWEEN requires [min, max] list value")
            low: float = float(rule.value[0])
            high: float = float(rule.value[1])
            return col.is_not_null() & (col >= low) & (col <= high)

        elif rule.check == RowRuleCheckType.CUSTOM_EXPR:
            if not rule.expression:
                raise ValueError(f"Rule {rule.rule_id} marked CUSTOM_EXPR must supply expression string")
            return pl.sql_expr(rule.expression)

        else:
            raise ValueError(f"Unsupported row check type: {rule.check}")


class BatchRuleCompiler:
    """Compiles whole-run batch rules into DuckDB SQL check queries."""

    @staticmethod
    def _quote_identifier(name: str) -> str:
        """Safely quote a column identifier for DuckDB (defense in depth).

        Column names are already validated against the declared schema, but
        quoting prevents any identifier containing quotes from breaking out.
        """
        return '"' + str(name).replace('"', '""') + '"'

    @staticmethod
    def compile_batch_sql(rule: BatchRule, table_name: str = "admitted_dataset") -> str:
        """
        Returns a SQL query returning 1 row with columns:
        passed (BOOLEAN), actual_value (VARCHAR), expected_value (VARCHAR).

        The ``BatchRule`` model already validates which fields each check type
        requires; these guards surface the same contract to the type checker.
        """
        def require_column() -> str:
            if rule.column is None:
                raise ValueError(f"Batch rule '{rule.rule_id}' check {rule.check} requires a 'column'")
            return rule.column

        def require_value() -> Any:
            if rule.value is None:
                raise ValueError(f"Batch rule '{rule.rule_id}' check {rule.check} requires a 'value'")
            return rule.value

        def require_sql() -> str:
            if rule.sql is None:
                raise ValueError(f"Batch rule '{rule.rule_id}' check {rule.check} requires a 'sql' assertion")
            return rule.sql

        if rule.check == BatchRuleCheckType.MIN_ROW_COUNT:
            min_count = int(require_value())
            return f"""
                SELECT
                    COUNT(*) >= {min_count} AS passed,
                    CAST(COUNT(*) AS VARCHAR) AS actual_value,
                    '>= {min_count}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.MAX_ROW_COUNT:
            max_count = int(require_value())
            return f"""
                SELECT
                    COUNT(*) <= {max_count} AS passed,
                    CAST(COUNT(*) AS VARCHAR) AS actual_value,
                    '<= {max_count}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.MAX_NULL_RATE:
            max_rate = float(require_value())
            col = BatchRuleCompiler._quote_identifier(require_column())
            return f"""
                SELECT
                    (CAST(SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS DOUBLE)
                        / NULLIF(COUNT(*), 0)) <= {max_rate} AS passed,
                    CAST(ROUND(CAST(SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS DOUBLE)
                        / NULLIF(COUNT(*), 0), 4) AS VARCHAR) AS actual_value,
                    '<= {max_rate}' AS expected_value
                FROM {table_name}
            """

        elif rule.check in (BatchRuleCheckType.UNIQUE, BatchRuleCheckType.COMPOSITE_UNIQUE):
            if rule.check == BatchRuleCheckType.UNIQUE:
                key_expr = f"({BatchRuleCompiler._quote_identifier(require_column())})"
            else:
                if not rule.columns or len(rule.columns) < 2:
                    raise ValueError(
                        f"Batch rule '{rule.rule_id}' check COMPOSITE_UNIQUE requires 'columns' with >= 2 entries")
                cols = ", ".join(BatchRuleCompiler._quote_identifier(c) for c in rule.columns)
                key_expr = f"({cols})"
            return f"""
                SELECT
                    COUNT(*) = COUNT(DISTINCT {key_expr}) AS passed,
                    CAST(COUNT(*) - COUNT(DISTINCT {key_expr}) AS VARCHAR) AS actual_value,
                    '0 duplicate keys' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.SUM_EQUALS:
            expected = float(require_value())
            tolerance = float(rule.tolerance) if rule.tolerance is not None else 0.0
            col = BatchRuleCompiler._quote_identifier(require_column())
            return f"""
                SELECT
                    ABS(COALESCE(SUM({col}), 0) - ({expected})) <= {tolerance} AS passed,
                    CAST(COALESCE(SUM({col}), 0) AS VARCHAR) AS actual_value,
                    '= {expected} (tolerance {tolerance})' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.SUM_BETWEEN:
            bounds = require_value()
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ValueError(f"Batch rule '{rule.rule_id}' check SUM_BETWEEN requires a [low, high] value")
            low, high = float(bounds[0]), float(bounds[1])
            col = BatchRuleCompiler._quote_identifier(require_column())
            return f"""
                SELECT
                    (COALESCE(SUM({col}), 0) >= {low} AND COALESCE(SUM({col}), 0) <= {high}) AS passed,
                    CAST(COALESCE(SUM({col}), 0) AS VARCHAR) AS actual_value,
                    'between {low} and {high}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.SQL_ASSERTION:
            sql = require_sql()
            sql_upper = sql.upper()
            has_agg = any(agg in sql_upper for agg in ("AVG(", "SUM(", "COUNT(", "MIN(", "MAX(", "MEDIAN(", "STDDEV("))
            if has_agg:
                return f"""
                    SELECT
                        COALESCE(CAST(({sql}) AS BOOLEAN), FALSE) AS passed,
                        CAST(COALESCE(CAST(({sql}) AS BOOLEAN), FALSE) AS VARCHAR) AS actual_value,
                        'TRUE' AS expected_value
                    FROM {table_name}
                """
            else:
                return f"""
                    SELECT
                        COALESCE(BOOL_AND(CAST(({sql}) AS BOOLEAN)), FALSE) AS passed,
                        CAST(COALESCE(BOOL_AND(CAST(({sql}) AS BOOLEAN)), FALSE) AS VARCHAR) AS actual_value,
                        'TRUE' AS expected_value
                    FROM {table_name}
                """

        else:
            raise ValueError(f"Unsupported batch check type: {rule.check}")


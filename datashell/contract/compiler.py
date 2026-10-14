"""Compiler for contract rules into vectorized Polars expressions and DuckDB SQL queries."""

from typing import List, Tuple, Any
import polars as pl
from datashell.contract.models import QualityRule, BatchRule, RowRuleCheckType, BatchRuleCheckType


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
        if isinstance(attr, pl.DataType) or issubclass(attr, pl.DataType):
            return attr() if callable(attr) else attr
    raise ValueError(f"Unsupported Polars type in contract: '{type_str}'")


class RuleCompiler:
    """Compiles row rules into Polars Boolean expressions."""

    @staticmethod
    def compile_row_rule(rule: QualityRule) -> pl.Expr:
        """
        Returns a Polars expression that evaluates to TRUE when the record passes
        and FALSE (or null) when it violates the rule.
        """
        col = pl.col(rule.column) if rule.column else None

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
            if not isinstance(rule.value, list):
                allowed = [rule.value]
            else:
                allowed = rule.value
            return col.is_not_null() & col.is_in(allowed)

        elif rule.check == RowRuleCheckType.REGEX_MATCH:
            return col.is_not_null() & col.str.contains(rule.value)

        elif rule.check == RowRuleCheckType.BETWEEN:
            if not (isinstance(rule.value, (list, tuple)) and len(rule.value) == 2):
                raise ValueError(f"Rule {rule.rule_id} check BETWEEN requires [min, max] list value")
            low, high = rule.value
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
    def compile_batch_sql(rule: BatchRule, table_name: str = "admitted_dataset") -> str:
        """
        Returns a SQL query returning 1 row with columns:
        passed (BOOLEAN), actual_value (VARCHAR), expected_value (VARCHAR).
        """
        if rule.check == BatchRuleCheckType.MIN_ROW_COUNT:
            min_count = int(rule.value)
            return f"""
                SELECT 
                    COUNT(*) >= {min_count} AS passed,
                    CAST(COUNT(*) AS VARCHAR) AS actual_value,
                    '>= {min_count}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.MAX_ROW_COUNT:
            max_count = int(rule.value)
            return f"""
                SELECT 
                    COUNT(*) <= {max_count} AS passed,
                    CAST(COUNT(*) AS VARCHAR) AS actual_value,
                    '<= {max_count}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.MAX_NULL_RATE:
            max_rate = float(rule.value)
            col = rule.column
            return f"""
                SELECT 
                    (CAST(SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(*), 0)) <= {max_rate} AS passed,
                    CAST(ROUND(CAST(SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(*), 0), 4) AS VARCHAR) AS actual_value,
                    '<= {max_rate}' AS expected_value
                FROM {table_name}
            """

        elif rule.check == BatchRuleCheckType.SQL_ASSERTION:
            sql_upper = rule.sql.upper()
            has_agg = any(agg in sql_upper for agg in ("AVG(", "SUM(", "COUNT(", "MIN(", "MAX(", "MEDIAN(", "STDDEV("))
            if has_agg:
                return f"""
                    SELECT 
                        COALESCE(CAST(({rule.sql}) AS BOOLEAN), FALSE) AS passed,
                        CAST(COALESCE(CAST(({rule.sql}) AS BOOLEAN), FALSE) AS VARCHAR) AS actual_value,
                        'TRUE' AS expected_value
                    FROM {table_name}
                """
            else:
                return f"""
                    SELECT 
                        COALESCE(BOOL_AND(CAST(({rule.sql}) AS BOOLEAN)), FALSE) AS passed,
                        CAST(COALESCE(BOOL_AND(CAST(({rule.sql}) AS BOOLEAN)), FALSE) AS VARCHAR) AS actual_value,
                        'TRUE' AS expected_value
                    FROM {table_name}
                """

        else:
            raise ValueError(f"Unsupported batch check type: {rule.check}")


"""Tier 1: At the Door — Structural Ingestion & Schema Conformance Engine.

Ensures that records whose types cannot be realized against the declared schema
never enter the pipeline at all. Downstream stages never need to defend against them.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import json
import csv
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import polars as pl

from datashell.contract.models import (
    DataContract,
    SchemaColumn,
    ExtraColumnsPolicy,
    SchemaCompatibilityPolicy,
    SourceParserOptions,
)
from datashell.contract.compiler import parse_polars_type


@dataclass
class StructuralRejection:
    row_index: int
    raw_record: Dict[str, Any]
    failed_column: str
    expected_type: str
    error_message: str


@dataclass
class IngestionResult:
    rows_read: int
    refused_count: int
    conforming_df: pl.DataFrame
    rejections: List[StructuralRejection] = field(default_factory=list)


# Exact integer bounds
INT_BOUNDS = {
    pl.Int8(): (-128, 127),
    pl.Int16(): (-32768, 32767),
    pl.Int32(): (-2147483648, 2147483647),
    pl.Int64(): (-9223372036854775808, 9223372036854775807),
    pl.UInt8(): (0, 255),
    pl.UInt16(): (0, 65535),
    pl.UInt32(): (0, 4294967295),
    pl.UInt64(): (0, 18446744073709551615),
}


class StructuralIngestionEngine:
    """
    Tier 1 gatekeeper: Reads source datasets, casts types strictly according
    to the contract, and immediately ejects unparseable or malformed rows.
    """

    def __init__(self, contract: DataContract):
        self.contract = contract
        source_def = contract.spec.sources[0] if contract.spec.sources else None
        self.declared_schema = source_def.schema_ if source_def else []
        self.extra_policy = source_def.extra_columns_policy if source_def else ExtraColumnsPolicy.STRIP
        self.declared_col_names = {col.name for col in self.declared_schema}
        self.schema_compatibility = (
            source_def.schema_compatibility if source_def else SchemaCompatibilityPolicy.EVOLVING
        )
        self.parser = source_def.parser if source_def else SourceParserOptions()
        # Extra tokens (beyond "" which is already treated as null) to coerce to
        # null during strict Tier-1 ingestion, e.g. ["NA", "UNKNOWN"].
        self._null_tokens = {t for t in (self.parser.null_values or [])}

        self._polars_schema = {
            col.name: parse_polars_type(col.polars_type)
            for col in self.declared_schema
        }
        self._nullable_map = {
            col.name: col.nullable
            for col in self.declared_schema
        }

    def ingest_from_file(self, file_path: Union[str, Path]) -> IngestionResult:
        """Ingest CSV, JSONL, or Parquet file from path."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")

        suffix = path.suffix.lower()
        if suffix == ".parquet":
            return self._ingest_parquet_optimized(path)
        elif suffix in (".jsonl", ".ndjson"):
            return self._ingest_jsonl(path)
        elif suffix == ".csv":
            return self._ingest_csv(path)
        else:
            raise ValueError(f"Unsupported file format: {suffix}")

    def iter_batches(self, file_path: Union[str, Path], batch_size: int = 100_000):
        """Yield source data as Polars DataFrames of roughly ``batch_size`` rows.

        Streams Parquet, CSV, and JSONL/NDJSON inputs without materializing the
        entire file, so callers can process arbitrarily large datasets in bounded
        memory. This is the ingestion primitive used by the out-of-core pipeline.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")

        suffix = path.suffix.lower()
        if suffix == ".parquet":
            yield from self._iter_parquet_batches(path, batch_size)
        elif suffix in (".jsonl", ".ndjson"):
            yield from self._iter_jsonl_batches(path, batch_size)
        elif suffix == ".csv":
            yield from self._iter_csv_batches(path, batch_size)
        else:
            raise ValueError(f"Unsupported file format: {suffix}")

    def _iter_parquet_batches(self, path: Path, batch_size: int):
        parquet_file = pq.ParquetFile(path)
        for batch in parquet_file.iter_batches(batch_size=batch_size):
            yield pl.from_arrow(batch)

    def _iter_csv_batches(self, path: Path, batch_size: int):
        # Force every field to Utf8 so strict Tier-1 casting (and its per-row
        # rejection diagnostics) behaves identically to the non-streaming CSV
        # reader instead of letting pyarrow infer a type and then hard-fail on a
        # single malformed value partway through the file.
        p = self.parser
        if p.has_header:
            with open(path, "r", encoding=p.encoding, newline="") as handle:
                header = next(csv.reader(handle, delimiter=p.delimiter, quotechar=p.quote_char), [])
            if not header:
                raise ValueError(f"CSV source '{path}' has no header row")
            column_names = header
        else:
            column_names = [col.name for col in self.declared_schema]
            if not column_names:
                raise ValueError("Headerless CSV requires a declared schema to name columns")
        string_types = {name: pa.string() for name in column_names}

        # PyArrow streams CSV in byte blocks, and its per-block resident memory
        # grows well faster than the block itself (string builders over-allocate),
        # so keep the block modest and slice it into row batches. The batch is
        # the unit the pipeline validates, so bounding the block bounds memory
        # regardless of file size.
        block_size = min(max(batch_size * 32, 1 << 20), 4 << 20)
        read_kwargs: Dict[str, Any] = {"block_size": block_size, "encoding": p.encoding}
        if not p.has_header:
            read_kwargs["column_names"] = column_names
        # NOTE: configured null tokens are applied during strict Tier-1 casting in
        # ``ingest_records``, not by pyarrow here, because ConvertOptions.null_values
        # does not affect columns pinned to the ``string`` type.
        reader = pa_csv.open_csv(
            str(path),
            read_options=pa_csv.ReadOptions(**read_kwargs),
            parse_options=pa_csv.ParseOptions(delimiter=p.delimiter, quote_char=p.quote_char),
            convert_options=pa_csv.ConvertOptions(column_types=string_types),
        )
        try:
            for block in reader:
                offset = 0
                while offset < block.num_rows:
                    yield pl.from_arrow(block.slice(offset, batch_size))
                    offset += batch_size
        finally:
            reader.close()

    def _iter_jsonl_batches(self, path: Path, batch_size: int):
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding=self.parser.encoding) as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                records.append(json.loads(stripped))
                if len(records) >= batch_size:
                    yield pl.DataFrame(records, infer_schema_length=None)
                    records = []
        if records:
            yield pl.DataFrame(records, infer_schema_length=None)

    def ingest_dataframe(self, chunk_df: pl.DataFrame) -> IngestionResult:
        """Vectorized Tier-1 ingestion for a chunk that already matches the contract.

        Uses zero-copy columnar casts when every declared column is present with
        its declared dtype and nullability. Falls back to the strict row-by-row
        path whenever a conversion, extra-column policy, missing column, or
        nullability check requires per-row diagnostics, preserving exact
        rejection semantics while avoiding Python-per-cell work on the fast path.
        """
        rows_read = chunk_df.height
        columns = set(chunk_df.columns)

        if self.extra_policy == ExtraColumnsPolicy.REJECT:
            extra_cols = columns - self.declared_col_names
            if extra_cols:
                extra_name = sorted(extra_cols)[0]
                rejections = [
                    StructuralRejection(
                        row_index=i,
                        raw_record={"extra": extra_name},
                        failed_column=extra_name,
                        expected_type="NONE (Extra column rejected)",
                        error_message=f"Extra unexpected column '{extra_name}' rejected by contract policy",
                    )
                    for i in range(rows_read)
                ]
                return IngestionResult(
                    rows_read=rows_read,
                    refused_count=rows_read,
                    conforming_df=pl.DataFrame(schema=self._polars_schema),
                    rejections=rejections,
                )

        if not self.declared_schema or not self.declared_col_names.issubset(columns):
            return self.ingest_records(chunk_df.to_dicts())

        for col in self.declared_schema:
            series = chunk_df.get_column(col.name)
            if series.dtype != self._polars_schema[col.name]:
                return self.ingest_records(chunk_df.to_dicts())
            if not col.nullable and series.null_count() > 0:
                return self.ingest_records(chunk_df.to_dicts())
            # Strict Tier-1 semantics reject NaN/Infinity for numeric columns;
            # fall back so the row path can produce per-row diagnostics rather
            # than silently admitting non-finite floats.
            if series.dtype in (pl.Float32, pl.Float64) and not series.is_finite().all():
                return self.ingest_records(chunk_df.to_dicts())

        if self.extra_policy == ExtraColumnsPolicy.ALLOW:
            conforming_df = chunk_df
        else:
            conforming_df = chunk_df.select([col.name for col in self.declared_schema])

        return IngestionResult(
            rows_read=rows_read,
            refused_count=0,
            conforming_df=conforming_df,
            rejections=[],
        )

    def ingest_records(self, raw_records: List[Dict[str, Any]]) -> IngestionResult:
        """Ingest raw python dictionary records directly."""
        rows_read = len(raw_records)
        conforming_rows: List[Dict[str, Any]] = []
        rejections: List[StructuralRejection] = []

        for idx, row in enumerate(raw_records):
            is_valid = True
            cleaned_row: Dict[str, Any] = {}

            # Extra columns check
            if self.extra_policy == ExtraColumnsPolicy.REJECT:
                extra_cols = set(row.keys()) - self.declared_col_names
                if extra_cols:
                    rejections.append(StructuralRejection(
                        row_index=idx,
                        raw_record=row,
                        failed_column=sorted(extra_cols)[0],
                        expected_type="NONE (Extra column rejected)",
                        error_message=f"Extra unexpected column '{sorted(extra_cols)[0]}' rejected by contract policy"
                    ))
                    continue

            # STRICT schema compatibility: a row that omits a declared column is a
            # structural failure regardless of nullability, so schema drift is
            # refused at the door instead of being silently back-filled with null.
            if self.schema_compatibility == SchemaCompatibilityPolicy.STRICT:
                missing_cols = sorted(self.declared_col_names - set(row.keys()))
                if missing_cols:
                    rejections.append(StructuralRejection(
                        row_index=idx,
                        raw_record=row,
                        failed_column=missing_cols[0],
                        expected_type="DECLARED_COLUMN_PRESENT",
                        error_message=(
                            f"Missing declared column(s) {missing_cols} under STRICT "
                            f"schema_compatibility"
                        )
                    ))
                    continue

            for col in self.declared_schema:
                col_name = col.name
                val = row.get(col_name)

                # Nullability check (empty string and configured parser null tokens
                # both count as null).
                if val is None or val == "" or (isinstance(val, str) and val in self._null_tokens):
                    if not col.nullable:
                        rejections.append(StructuralRejection(
                            row_index=idx,
                            raw_record=row,
                            failed_column=col_name,
                            expected_type=col.polars_type,
                            error_message="Non-nullable column received null/empty value"
                        ))
                        is_valid = False
                        break
                    cleaned_row[col_name] = None
                    continue

                # Type realization check
                try:
                    target_type = self._polars_schema[col_name]
                    cast_val = self._cast_scalar(val, target_type)
                    cleaned_row[col_name] = cast_val
                except (ValueError, TypeError, OverflowError) as err:
                    rejections.append(StructuralRejection(
                        row_index=idx,
                        raw_record=row,
                        failed_column=col_name,
                        expected_type=col.polars_type,
                        error_message=f"Cannot realize type {col.polars_type}: {str(err)}"
                    ))
                    is_valid = False
                    break

            if is_valid:
                if self.extra_policy == ExtraColumnsPolicy.ALLOW:
                    for k, v in row.items():
                        if k not in self.declared_col_names:
                            cleaned_row[k] = v
                conforming_rows.append(cleaned_row)

        if conforming_rows:
            if self.extra_policy == ExtraColumnsPolicy.ALLOW:
                conforming_df = pl.DataFrame(conforming_rows)
                # Ensure declared columns match their strict types
                for col_name, dt in self._polars_schema.items():
                    if col_name in conforming_df.columns:
                        conforming_df = conforming_df.with_columns(pl.col(col_name).cast(dt))
            else:
                conforming_df = pl.DataFrame(conforming_rows, schema=self._polars_schema)
        else:
            conforming_df = pl.DataFrame(schema=self._polars_schema)

        return IngestionResult(
            rows_read=rows_read,
            refused_count=len(rejections),
            conforming_df=conforming_df,
            rejections=rejections
        )

    def _cast_scalar(self, val: Any, target_type: pl.DataType) -> Any:
        """Strict non-lossy casting validator for scalar values."""
        if target_type == pl.String():
            return str(val)

        # Integer types (Int8, Int16, Int32, Int64, UInt8, UInt16, UInt32, UInt64)
        if target_type in INT_BOUNDS:
            if isinstance(val, bool):
                raise ValueError("Boolean cannot be implicitly cast to Integer")
            
            # Check for lossy float conversion (e.g. 3.8 should NOT truncate to 3)
            if isinstance(val, float):
                if math.isnan(val) or math.isinf(val) or not val.is_integer():
                    raise ValueError(f"Lossy conversion: Float {val} cannot be cast to Integer")
                val_int = int(val)
            else:
                s_val = str(val).strip()
                if "." in s_val:
                    f_val = float(s_val)
                    if not f_val.is_integer():
                        raise ValueError(f"Lossy conversion: String '{val}' has fractional part")
                    val_int = int(f_val)
                else:
                    val_int = int(s_val)

            # Bounds check
            min_val, max_val = INT_BOUNDS[target_type]
            if not (min_val <= val_int <= max_val):
                raise OverflowError(f"Value {val_int} out of range for {target_type} [{min_val}, {max_val}]")
            return val_int

        # Float types
        if target_type in (pl.Float64(), pl.Float32()):
            if isinstance(val, bool):
                raise ValueError("Boolean cannot be implicitly cast to Float")
            f_val = float(val)
            if math.isnan(f_val) or math.isinf(f_val):
                raise ValueError(f"NaN/Infinity is not permitted for numeric float column")
            return f_val

        # Boolean
        if target_type == pl.Boolean():
            if isinstance(val, bool):
                return val
            s = str(val).strip().lower()
            if s in ("true", "1", "t", "yes"):
                return True
            if s in ("false", "0", "f", "no"):
                return False
            raise ValueError(f"Value '{val}' cannot be realized as Boolean")

        # Date & Datetime
        if target_type == pl.Date():
            if isinstance(val, date):
                return val
            # Try ISO format
            return date.fromisoformat(str(val).strip()[:10])

        if target_type == pl.Datetime():
            if isinstance(val, datetime):
                return val
            s = str(val).strip().replace("Z", "+00:00")
            return datetime.fromisoformat(s)

        return val

    def _ingest_parquet_optimized(self, path: Path) -> IngestionResult:
        """
        Ingests Parquet files using zero-copy columnar Arrow/Polars processing.
        Eliminates Python dictionary materialization when data structurally conforms.
        """
        table = pq.read_table(path)
        rows_read = table.num_rows
        df = pl.from_arrow(table)

        # Check for extra unexpected columns
        extra_cols = set(df.columns) - self.declared_col_names
        if extra_cols and self.extra_policy == ExtraColumnsPolicy.REJECT:
            rejections = [
                StructuralRejection(
                    row_index=i,
                    raw_record={"extra": sorted(extra_cols)[0]},
                    failed_column=sorted(extra_cols)[0],
                    expected_type="NONE (Extra column rejected)",
                    error_message=f"Extra unexpected column '{sorted(extra_cols)[0]}' rejected by contract policy"
                )
                for i in range(rows_read)
            ]
            empty_df = pl.DataFrame(schema=self._polars_schema)
            return IngestionResult(
                rows_read=rows_read,
                refused_count=rows_read,
                conforming_df=empty_df,
                rejections=rejections
            )

        if self.extra_policy == ExtraColumnsPolicy.STRIP:
            df = df.select([c for c in df.columns if c in self.declared_col_names])

        # Check for missing declared columns
        missing_cols = self.declared_col_names - set(df.columns)
        if missing_cols:
            return self.ingest_records(table.to_pylist())

        # Check nullability on declared non-nullable columns vectorially
        non_nullable_cols = [c.name for c in self.declared_schema if not c.nullable]
        if non_nullable_cols:
            null_predicates = [pl.col(c).is_null() for c in non_nullable_cols]
            has_null_expr = pl.any_horizontal(null_predicates)
            failed_nulls_df = df.filter(has_null_expr)
            if failed_nulls_df.height > 0:
                # If null violations exist, fallback to detailed row rejections
                return self.ingest_records(table.to_pylist())

        # Columnar cast to declared schema
        try:
            cast_exprs = [pl.col(col.name).cast(self._polars_schema[col.name]) for col in self.declared_schema]
            if self.extra_policy == ExtraColumnsPolicy.ALLOW:
                other_cols = [pl.col(c) for c in df.columns if c not in self.declared_col_names]
                conforming_df = df.with_columns(cast_exprs).select([pl.col(col.name) for col in self.declared_schema] + other_cols)
            else:
                conforming_df = df.select(cast_exprs)

            return IngestionResult(
                rows_read=rows_read,
                refused_count=0,
                conforming_df=conforming_df,
                rejections=[]
            )
        except Exception:
            # Fall back to row-by-row scalar ingestion for detailed rejection diagnostics
            return self.ingest_records(table.to_pylist())

    def _ingest_jsonl(self, path: Path) -> IngestionResult:
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding=self.parser.encoding) as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    records.append(json.loads(stripped))
        return self.ingest_records(records)

    def _ingest_csv(self, path: Path) -> IngestionResult:
        p = self.parser
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding=p.encoding, newline="") as f:
            if p.has_header:
                reader = csv.DictReader(f, delimiter=p.delimiter, quotechar=p.quote_char)
            else:
                fieldnames = [col.name for col in self.declared_schema]
                if not fieldnames:
                    raise ValueError("Headerless CSV requires a declared schema to name columns")
                reader = csv.DictReader(f, fieldnames=fieldnames, delimiter=p.delimiter, quotechar=p.quote_char)
            for row in reader:
                records.append(dict(row))
        return self.ingest_records(records)

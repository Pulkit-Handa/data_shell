"""Enterprise contract models matching the daas/v1 DataProduct specification,
with rigorous cross-field validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from datashell.core.sandbox import FilesystemSandbox


def known_row_check_names() -> set[str]:
    """Built-in + plugin + in-process check names (deferred to avoid cycles)."""
    from datashell.contract import rule_registry

    return set(rule_registry.available_row_checks()) | set(rule_registry.available_batch_checks())


def rule_registry_row_group() -> str:
    from datashell.contract import rule_registry

    return rule_registry.ROW_CHECK_GROUP


def rule_registry_batch_group() -> str:
    from datashell.contract import rule_registry

    return rule_registry.BATCH_CHECK_GROUP


class QualityBehavior(StrEnum):
    DROP_ROW = "DROP_ROW"      # Held rows quarantined; valid rows delivered unless batch check fails
    REJECT_RUN = "REJECT_RUN"  # Whole run rejected if any row rule OR batch rule fails
    WARN = "WARN"              # Violations recorded in audit certificate, but rows delivered without dropping


class ExtraColumnsPolicy(StrEnum):
    REJECT = "REJECT"          # Any unexpected column causes structural rejection
    STRIP = "STRIP"            # Unexpected columns stripped from conforming dataset
    ALLOW = "ALLOW"            # Unexpected columns preserved as strings/raw


class RowRuleCheckType(StrEnum):
    NOT_NULL = "NOT_NULL"
    GREATER_THAN = "GREATER_THAN"
    GREATER_EQUAL = "GREATER_EQUAL"
    LESS_THAN = "LESS_THAN"
    LESS_EQUAL = "LESS_EQUAL"
    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    ALLOWED_VALUES = "ALLOWED_VALUES"
    REGEX_MATCH = "REGEX_MATCH"
    BETWEEN = "BETWEEN"
    CUSTOM_EXPR = "CUSTOM_EXPR"


class BatchRuleCheckType(StrEnum):
    MIN_ROW_COUNT = "MIN_ROW_COUNT"
    MAX_ROW_COUNT = "MAX_ROW_COUNT"
    MAX_NULL_RATE = "MAX_NULL_RATE"
    UNIQUE = "UNIQUE"                          # single-column key must be unique
    COMPOSITE_UNIQUE = "COMPOSITE_UNIQUE"      # multi-column key must be unique
    SUM_EQUALS = "SUM_EQUALS"                  # control total: column sum equals expected
    SUM_BETWEEN = "SUM_BETWEEN"                # control total: column sum within [min, max]
    SQL_ASSERTION = "SQL_ASSERTION"


class SchemaCompatibilityPolicy(StrEnum):
    """How the boundary reacts when the source does not present the declared schema."""

    STRICT = "STRICT"        # Missing declared columns are structural failures
    EVOLVING = "EVOLVING"    # Missing nullable columns are filled with null (default)


class _RuleCheckTypeMeta(type):
    """Metaclass providing backwards-compatible enum attribute access."""
    def __getattr__(cls, name: str) -> Any:
        if hasattr(RowRuleCheckType, name):
            return getattr(RowRuleCheckType, name)
        if hasattr(BatchRuleCheckType, name):
            return getattr(BatchRuleCheckType, name)
        raise AttributeError(f"RuleCheckType has no attribute '{name}'")


class RuleCheckType(metaclass=_RuleCheckTypeMeta):
    """Backwards-compatible interface supporting both Row and Batch rule enum attributes."""
    pass


class SchemaColumn(BaseModel):
    name: str
    polars_type: str = Field(
        description="Polars datatype: Utf8, String, Int64, Int32, Int16, Int8, "
                    "UInt64, UInt32, Float64, Float32, Boolean, Date, Datetime")
    nullable: bool = True
    description: str | None = None


class SourceParserOptions(BaseModel):
    """Configurable physical parsing options for delimited sources.

    Applies to CSV/JSONL readers. Defaults preserve the historical behaviour
    (comma-delimited, double-quote quoting, header row present, UTF-8).
    """

    delimiter: str = ","
    quote_char: str = '"'
    has_header: bool = True
    encoding: str = "utf-8"
    null_values: list[str] = Field(default_factory=list)

    @field_validator("delimiter")
    @classmethod
    def validate_delimiter(cls, v: str) -> str:
        if not isinstance(v, str) or len(v) != 1:
            raise ValueError("parser.delimiter must be a single character")
        return v

    @field_validator("quote_char")
    @classmethod
    def validate_quote_char(cls, v: str) -> str:
        if not isinstance(v, str) or len(v) != 1:
            raise ValueError("parser.quote_char must be a single character")
        return v

    @field_validator("encoding")
    @classmethod
    def validate_encoding(cls, v: str) -> str:
        import codecs
        if not v:
            raise ValueError("parser.encoding must be a non-empty codec name")
        try:
            codecs.lookup(v)
        except LookupError as err:
            raise ValueError(f"Unknown parser.encoding '{v}': {err}") from err
        return v


class SourceDefinition(BaseModel):
    type: str = "LOCAL"  # LOCAL
    connectionRef: str | None = None
    path: str
    format: str = "parquet"  # parquet, csv, jsonl
    schema_: list[SchemaColumn] = Field(alias="schema", default_factory=list)
    extra_columns_policy: ExtraColumnsPolicy = ExtraColumnsPolicy.STRIP
    schema_compatibility: SchemaCompatibilityPolicy = SchemaCompatibilityPolicy.EVOLVING
    parser: SourceParserOptions = Field(default_factory=SourceParserOptions)

    model_config = {"populate_by_name": True}

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        fmt = v.lower()
        if fmt not in ("parquet", "csv", "jsonl", "ndjson"):
            raise ValueError(f"Unsupported source format '{v}'. Supported: parquet, csv, jsonl")
        return fmt


class QualityRule(BaseModel):
    """One vectorized row rule.

    ``check`` is a built-in ``RowRuleCheckType`` member, ``CUSTOM_EXPR``, or an
    extended check name provided by a plugin (the
    ``datashell.rule_checks.row`` entry-point group; see
    ``datashell.contract.rule_registry``). Names are UPPER_SNAKE_CASE; built-ins
    cannot be shadowed, and unknown names are rejected here so invalid
    contracts fail at validation, not at 2 a.m. enforcement.
    """
    rule_id: str = Field(description="Stable identifier kept across contract versions")
    check: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{1,63}$",
        description="Built-in check member, CUSTOM_EXPR, or a registered plugin check name")
    column: str | None = None
    value: Any | None = None
    expression: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_rule_parameters(self) -> QualityRule:
        if self.check == RowRuleCheckType.CUSTOM_EXPR:
            if not self.expression:
                raise ValueError(f"Rule '{self.rule_id}' with check CUSTOM_EXPR must supply 'expression'")
        elif self.check == RowRuleCheckType.NOT_NULL:
            if not self.column:
                raise ValueError(f"Rule '{self.rule_id}' with check NOT_NULL must supply 'column'")
        elif self.check in (
            RowRuleCheckType.GREATER_THAN, RowRuleCheckType.GREATER_EQUAL,
            RowRuleCheckType.LESS_THAN, RowRuleCheckType.LESS_EQUAL,
            RowRuleCheckType.EQUALS, RowRuleCheckType.NOT_EQUALS,
            RowRuleCheckType.ALLOWED_VALUES, RowRuleCheckType.REGEX_MATCH
        ):
            if not self.column:
                raise ValueError(f"Rule '{self.rule_id}' with check {self.check} must supply 'column'")
            if self.value is None:
                raise ValueError(f"Rule '{self.rule_id}' with check {self.check} must supply 'value'")
        elif self.check == RowRuleCheckType.BETWEEN:
            if not self.column:
                raise ValueError(f"Rule '{self.rule_id}' with check BETWEEN must supply 'column'")
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError(f"Rule '{self.rule_id}' with check BETWEEN requires a [min, max] list value")
        elif self.check not in known_row_check_names():
            raise ValueError(
                f"Rule '{self.rule_id}' has unknown check '{self.check}'. Built-ins are "
                f"fixed; extended checks come from the "
                f"'{rule_registry_row_group()}' entry-point group (see 'datashell rules')")
        return self


class BatchRule(BaseModel):
    """One whole-run batch rule (same naming rules as ``QualityRule.check``)."""

    rule_id: str = Field(description="Stable identifier kept across contract versions")
    check: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{1,63}$",
        description="Built-in check member, SQL_ASSERTION, or a registered plugin check name")
    column: str | None = None
    columns: list[str] | None = None
    value: Any | None = None
    tolerance: float | None = None
    sql: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_batch_rule_parameters(self) -> BatchRule:
        if self.check == BatchRuleCheckType.SQL_ASSERTION:
            if not self.sql:
                raise ValueError(f"Batch rule '{self.rule_id}' with check SQL_ASSERTION must supply 'sql'")
        elif self.check in (BatchRuleCheckType.MIN_ROW_COUNT, BatchRuleCheckType.MAX_ROW_COUNT):
            if self.value is None or not isinstance(self.value, (int, float)):
                raise ValueError(f"Batch rule '{self.rule_id}' with check {self.check} requires numeric 'value'")
        elif self.check == BatchRuleCheckType.MAX_NULL_RATE:
            if not self.column:
                raise ValueError(f"Batch rule '{self.rule_id}' with check MAX_NULL_RATE must supply 'column'")
            if self.value is None or not (0.0 <= float(self.value) <= 1.0):
                raise ValueError(
                    f"Batch rule '{self.rule_id}' with check MAX_NULL_RATE "
                    "requires 'value' between 0.0 and 1.0")
        elif self.check == BatchRuleCheckType.UNIQUE:
            if not self.column:
                raise ValueError(f"Batch rule '{self.rule_id}' with check UNIQUE must supply 'column'")
        elif self.check == BatchRuleCheckType.COMPOSITE_UNIQUE:
            if not self.columns or len(self.columns) < 2:
                raise ValueError(
                    f"Batch rule '{self.rule_id}' with check COMPOSITE_UNIQUE "
                    "must supply 'columns' with at least 2 entries"
                )
            if len(set(self.columns)) != len(self.columns):
                raise ValueError(f"Batch rule '{self.rule_id}' COMPOSITE_UNIQUE columns must be distinct")
        elif self.check == BatchRuleCheckType.SUM_EQUALS:
            if not self.column:
                raise ValueError(f"Batch rule '{self.rule_id}' with check SUM_EQUALS must supply 'column'")
            if self.value is None or not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
                raise ValueError(f"Batch rule '{self.rule_id}' with check SUM_EQUALS requires numeric 'value'")
            if self.tolerance is not None and float(self.tolerance) < 0:
                raise ValueError(f"Batch rule '{self.rule_id}' SUM_EQUALS 'tolerance' cannot be negative")
        elif self.check == BatchRuleCheckType.SUM_BETWEEN:
            if not self.column:
                raise ValueError(f"Batch rule '{self.rule_id}' with check SUM_BETWEEN must supply 'column'")
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError(f"Batch rule '{self.rule_id}' with check SUM_BETWEEN requires a [min, max] list value")
            low, high = self.value
            if float(low) > float(high):
                raise ValueError(f"Batch rule '{self.rule_id}' SUM_BETWEEN requires min <= max")
        if self.check not in known_row_check_names():
            raise ValueError(
                f"Batch rule '{self.rule_id}' has unknown check '{self.check}'. Built-ins are "
                f"fixed; extended checks come from the "
                f"'{rule_registry_batch_group()}' entry-point group (see 'datashell rules')")
        if self.column and self.columns:
            raise ValueError(f"Batch rule '{self.rule_id}' cannot set both 'column' and 'columns'")
        return self


class QualitySpec(BaseModel):
    behavior: QualityBehavior = QualityBehavior.DROP_ROW
    rules: list[QualityRule] = Field(default_factory=list)
    batch_rules: list[BatchRule] = Field(default_factory=list)


class DeliveryTarget(BaseModel):
    type: str = "PARQUET"
    connectionRef: str | None = None
    path: str
    mode: str = "overwrite"

    @model_validator(mode="after")
    def validate_target_support(self) -> DeliveryTarget:
        target_type = self.type.upper()
        if target_type != "PARQUET":
            raise ValueError(
                f"Unsupported delivery target type '{self.type}'. "
                f"The boundary engine currently executes 'PARQUET'. (DELTA and POSTGRES are planned)."
            )
        target_mode = self.mode.lower()
        if target_mode != "overwrite":
            raise ValueError(
                f"Unsupported delivery target mode '{self.mode}'. "
                f"The boundary engine executes atomic 'overwrite' delivery."
            )
        # Sandbox check
        FilesystemSandbox.sanitize_and_validate_path(self.path, create_parent=False)
        return self


class TransformationSpec(BaseModel):
    map: list[dict[str, Any]] = Field(default_factory=list)
    reduce: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transformations(self) -> TransformationSpec:
        if self.map or self.reduce:
            raise ValueError(
                "Transformations map/reduce are not implemented in boundary enforcement runtime. "
                "Per Data Shell architecture: tagging and transformations must occur downstream."
            )
        return self


class DeliverySpec(BaseModel):
    refreshSchedule: str | None = None
    targets: list[DeliveryTarget] = Field(default_factory=list)
    quarantinePath: str | None = None

    @field_validator("quarantinePath")
    @classmethod
    def validate_quarantine_path(cls, v: str | None) -> str | None:
        if v:
            FilesystemSandbox.sanitize_and_validate_path(v, create_parent=False)
        return v


class ContractMetadata(BaseModel):
    name: str
    owner: str = "data-platform"
    version: str = "1.0.0"
    status: str = "ACTIVE"
    description: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", v) or ".." in v:
            raise ValueError(f"Contract name '{v}' contains invalid characters or path traversal")
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", v) or ".." in v:
            raise ValueError(f"Contract version '{v}' contains invalid characters or path traversal")
        return v


class ContractSpec(BaseModel):
    sources: list[SourceDefinition] = Field(default_factory=list)
    quality: QualitySpec = Field(default_factory=QualitySpec)
    transformation: TransformationSpec = Field(default_factory=TransformationSpec)
    delivery: DeliverySpec = Field(default_factory=DeliverySpec)

    @model_validator(mode="after")
    def validate_cross_field_integrity(self) -> ContractSpec:
        # 1. Schema must be present
        if not self.sources or not self.sources[0].schema_:
            raise ValueError("Contract must declare at least one source with a non-empty schema")
        
        declared_cols = {col.name for col in self.sources[0].schema_}

        # 2. Rule ID uniqueness
        seen_rule_ids = set()
        for r in self.quality.rules:
            if r.rule_id in seen_rule_ids:
                raise ValueError(f"Duplicate rule_id detected: '{r.rule_id}' is defined multiple times")
            seen_rule_ids.add(r.rule_id)

            # 3. Column existence check
            if r.column and r.column not in declared_cols:
                raise ValueError(
                    f"Rule '{r.rule_id}' references column '{r.column}' which is "
                    f"not in declared schema {sorted(declared_cols)}"
                )

        for b in self.quality.batch_rules:
            if b.rule_id in seen_rule_ids:
                raise ValueError(f"Duplicate rule_id detected: '{b.rule_id}' is defined multiple times")
            seen_rule_ids.add(b.rule_id)

            if b.column and b.column not in declared_cols:
                raise ValueError(
                    f"Batch rule '{b.rule_id}' references column '{b.column}' which is "
                    f"not in declared schema {sorted(declared_cols)}"
                )

            for col in (b.columns or []):
                if col not in declared_cols:
                    raise ValueError(
                        f"Batch rule '{b.rule_id}' references column '{col}' which is "
                        f"not in declared schema {sorted(declared_cols)}"
                    )

        return self


class DataContract(BaseModel):
    apiVersion: str = "daas/v1"
    kind: str = "DataProduct"
    metadata: ContractMetadata
    spec: ContractSpec

    def canonical_sha256(self) -> str:
        """Computes deterministic SHA-256 digest of complete contract content."""
        from datashell.core.crypto import CryptoEngine
        return CryptoEngine.canonical_json_hash(self.model_dump(by_alias=True))

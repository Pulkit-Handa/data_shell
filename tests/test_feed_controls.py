"""Feed-control tests for the P0 contract-surface additions.

Covers first-class unique/composite-key checks, batch control totals,
configurable parser options, and explicit schema-compatibility policies.
"""

from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityBehavior,
    QualityRule,
    QualitySpec,
    RuleCheckType,
    SchemaColumn,
    SchemaCompatibilityPolicy,
    SourceDefinition,
    SourceParserOptions,
)
from datashell.engines.delivery import TerminalStatus
from datashell.engines.ingestion import StructuralIngestionEngine
from datashell.pipeline.runner import DataShellPipeline


def _schema(*cols):
    return [
        SchemaColumn(name="id", polars_type="Int64", nullable=True),
        SchemaColumn(name="account", polars_type="Utf8", nullable=True),
        SchemaColumn(name="amount", polars_type="Float64", nullable=True),
        SchemaColumn(name="currency", polars_type="Utf8", nullable=True),
    ]


def _contract(
    name, batch_rules=None, schema=None, parser=None,
    schema_compatibility=None, behavior=QualityBehavior.DROP_ROW,
):
    return DataContract(
        metadata={"name": name, "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path="in.parquet",
                    format="parquet",
                    schema=schema or _schema(),
                    **({"parser": parser} if parser else {}),
                    **({"schema_compatibility": schema_compatibility} if schema_compatibility else {}),
                )
            ],
            "quality": QualitySpec(
                behavior=behavior,
                rules=[
                    QualityRule(rule_id="amount-positive", column="amount",
                                check=RuleCheckType.GREATER_THAN, value=0.0)
                ],
                batch_rules=batch_rules or [],
            ),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path="/tmp/feed_control_out.parquet")]),
        },
    )


# --------------------------------------------------------------------------
# Uniqueness / composite keys
# --------------------------------------------------------------------------

def test_single_column_unique_rejects_duplicate_keys(tmp_path, monkeypatch):
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    contract = _contract(
        "unique-contract",
        batch_rules=[
            BatchRule(rule_id="id-unique", check=BatchRuleCheckType.UNIQUE, column="id"),
        ],
    )
    dup = [
        {"id": 1, "account": "a", "amount": 10.0, "currency": "USD"},
        {"id": 1, "account": "b", "amount": 20.0, "currency": "USD"},  # duplicate key
        {"id": 2, "account": "c", "amount": 30.0, "currency": "USD"},
    ]
    result = DataShellPipeline(contract).run_records(dup, target_path_override=str(tmp_path / "out.parquet"))
    uniq = [b for b in result.audit_certificate["batch_evaluations"] if b["rule_id"] == "id-unique"]
    assert uniq, result.audit_certificate["batch_evaluations"]
    assert uniq[0]["passed"] is False
    assert uniq[0]["actual"] == "1"
    assert result.status == TerminalStatus.REJECTED


def test_composite_unique_allows_repeated_single_column(tmp_path, monkeypatch):
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    contract = _contract(
        "composite-contract",
        batch_rules=[
            BatchRule(rule_id="account-day-unique",
                      check=BatchRuleCheckType.COMPOSITE_UNIQUE,
                      columns=["account", "currency"]),
        ],
    )
    records = [
        {"id": 1, "account": "a", "amount": 10.0, "currency": "USD"},
        {"id": 2, "account": "a", "amount": 20.0, "currency": "USD"},  # (a, USD) repeats
        {"id": 3, "account": "b", "amount": 30.0, "currency": "USD"},
    ]
    result = DataShellPipeline(contract).run_records(records, target_path_override=str(tmp_path / "out.parquet"))
    uniq = [b for b in result.audit_certificate["batch_evaluations"] if b["rule_id"] == "account-day-unique"]
    assert uniq[0]["passed"] is False
    assert result.status == TerminalStatus.REJECTED

    # Distinct (account, currency) pairs pass.
    ok = [
        {"id": 1, "account": "a", "amount": 10.0, "currency": "USD"},
        {"id": 2, "account": "a", "amount": 20.0, "currency": "EUR"},
        {"id": 3, "account": "b", "amount": 30.0, "currency": "USD"},
    ]
    pass_result = DataShellPipeline(contract).run_records(ok, target_path_override=str(tmp_path / "out2.parquet"))
    uniq2 = [b for b in pass_result.audit_certificate["batch_evaluations"] if b["rule_id"] == "account-day-unique"]
    assert uniq2[0]["passed"] is True
    assert pass_result.status == TerminalStatus.LOADED


def test_batch_rule_rejects_bad_uniqueness_declarations():
    with pytest.raises(ValidationError):
        BatchRule(rule_id="bad-unique", check=BatchRuleCheckType.UNIQUE)  # missing column
    with pytest.raises(ValidationError):
        BatchRule(rule_id="bad-composite", check=BatchRuleCheckType.COMPOSITE_UNIQUE, columns=["only-one"])
    with pytest.raises(ValidationError):
        BatchRule(rule_id="both", check=BatchRuleCheckType.UNIQUE, column="id", columns=["id", "account"])


# --------------------------------------------------------------------------
# Batch control totals
# --------------------------------------------------------------------------

def test_sum_equals_control_total_passes_and_fails(tmp_path, monkeypatch):
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    contract = _contract(
        "control-total-contract",
        batch_rules=[
            BatchRule(rule_id="amount-total", check=BatchRuleCheckType.SUM_EQUALS, column="amount", value=60.0),
        ],
    )
    exact = [
        {"id": 1, "account": "a", "amount": 10.0, "currency": "USD"},
        {"id": 2, "account": "b", "amount": 50.0, "currency": "USD"},
    ]
    ok = DataShellPipeline(contract).run_records(exact, target_path_override=str(tmp_path / "out.parquet"))
    total = [b for b in ok.audit_certificate["batch_evaluations"] if b["rule_id"] == "amount-total"][0]
    assert total["passed"] is True
    assert total["actual"] == "60.0"
    assert ok.status == TerminalStatus.LOADED

    off = [
        {"id": 1, "account": "a", "amount": 10.0, "currency": "USD"},
        {"id": 2, "account": "b", "amount": 40.0, "currency": "USD"},
    ]
    bad = DataShellPipeline(contract).run_records(off, target_path_override=str(tmp_path / "out2.parquet"))
    total2 = [b for b in bad.audit_certificate["batch_evaluations"] if b["rule_id"] == "amount-total"][0]
    assert total2["passed"] is False
    assert bad.status == TerminalStatus.REJECTED


def test_sum_between_control_total(tmp_path, monkeypatch):
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    contract = _contract(
        "sum-between-contract",
        batch_rules=[
            BatchRule(rule_id="amount-band", check=BatchRuleCheckType.SUM_BETWEEN, column="amount", value=[50.0, 70.0]),
        ],
    )
    records = [
        {"id": 1, "account": "a", "amount": 30.0, "currency": "USD"},
        {"id": 2, "account": "b", "amount": 25.0, "currency": "USD"},
    ]
    result = DataShellPipeline(contract).run_records(records, target_path_override=str(tmp_path / "out.parquet"))
    band = [b for b in result.audit_certificate["batch_evaluations"] if b["rule_id"] == "amount-band"][0]
    assert band["passed"] is True
    assert result.status == TerminalStatus.LOADED


def test_sum_rule_declarations_validated():
    with pytest.raises(ValidationError):
        BatchRule(rule_id="bad", check=BatchRuleCheckType.SUM_EQUALS, column="amount", value="total")
    with pytest.raises(ValidationError):
        BatchRule(rule_id="bad-tol", check=BatchRuleCheckType.SUM_EQUALS, column="amount", value=1.0, tolerance=-1)
    with pytest.raises(ValidationError):
        BatchRule(rule_id="bad-band", check=BatchRuleCheckType.SUM_BETWEEN, column="amount", value=[10.0, 1.0])


# --------------------------------------------------------------------------
# Parser options
# --------------------------------------------------------------------------

def test_custom_delimiter_and_headerless_csv(tmp_path, monkeypatch):
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
    ]
    sep_csv = tmp_path / "sep.csv"
    sep_csv.write_text("id;amount;currency\n1;10.5;USD\n2;20.5;EUR\n", encoding="utf-8")

    contract = DataContract(
        metadata={"name": "delimiter-contract", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(sep_csv), format="csv", schema=schema,
                    parser=SourceParserOptions(delimiter=";"),
                )
            ],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW, rules=[]),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(tmp_path / "out.parquet"))]),
        },
    )
    res = DataShellPipeline(contract).run_file(str(sep_csv))
    assert res.status == TerminalStatus.LOADED
    assert res.ledger.rows_read == 2
    delivered = pl.read_parquet(tmp_path / "out.parquet")
    assert delivered["amount"].to_list() == [10.5, 20.5]

    # Headerless CSV uses declared schema column order.
    no_header = tmp_path / "noheader.csv"
    no_header.write_text("1,10.5,USD\n2,20.5,EUR\n", encoding="utf-8")
    hdr_contract = DataContract(
        metadata={"name": "headerless-contract", "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(no_header), format="csv", schema=schema,
                    parser=SourceParserOptions(has_header=False),
                )
            ],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW, rules=[]),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(tmp_path / "out2.parquet"))]),
        },
    )
    res2 = DataShellPipeline(hdr_contract).run_file(str(no_header))
    assert res2.status == TerminalStatus.LOADED
    assert res2.ledger.rows_read == 2


def test_configured_null_token_is_coerced_not_refused(tmp_path):
    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=True),
        SchemaColumn(name="amount", polars_type="Float64", nullable=True),
    ]
    csv = tmp_path / "nulls.csv"
    csv.write_text("id,amount\n1,10.5\n2,NA\n", encoding="utf-8")

    # Without a configured null token, "NA" is a structural refusal.
    plain = StructuralIngestionEngine(
        DataContract(metadata={"name": "n1", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(path=str(csv), format="csv", schema=schema)],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    r_plain = plain.ingest_from_file(csv)
    assert r_plain.rows_read == 2
    assert r_plain.refused_count == 1

    # With a configured null token, "NA" becomes null and the row is admitted.
    aware = StructuralIngestionEngine(
        DataContract(metadata={"name": "n2", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(
                path=str(csv), format="csv", schema=schema,
                parser=SourceParserOptions(null_values=["NA"]),
            )],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    r_aware = aware.ingest_from_file(csv)
    assert r_aware.rows_read == 2
    assert r_aware.refused_count == 0
    assert r_aware.conforming_df.filter(pl.col("amount").is_null()).height == 1


def test_csv_encoding_option(tmp_path):
    schema = [SchemaColumn(name="name", polars_type="Utf8", nullable=True)]
    csv = tmp_path / "latin1.csv"
    csv.write_bytes("name\ncaf\xe9\n".encode("latin-1"))

    engine = StructuralIngestionEngine(
        DataContract(metadata={"name": "enc", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(
                path=str(csv), format="csv", schema=schema,
                parser=SourceParserOptions(encoding="latin-1"),
            )],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    res = engine.ingest_from_file(csv)
    assert res.refused_count == 0
    assert res.conforming_df["name"].to_list() == ["café"]


def test_streaming_csv_honors_custom_delimiter(tmp_path):
    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
    ]
    csv = tmp_path / "sep.csv"
    csv.write_text("id|amount\n1|10.5\n2|20.5\n3|30.5\n", encoding="utf-8")
    engine = StructuralIngestionEngine(
        DataContract(metadata={"name": "stream-sep", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(
                path=str(csv), format="csv", schema=schema,
                parser=SourceParserOptions(delimiter="|"),
            )],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    total = 0
    for batch in engine.iter_batches(csv, batch_size=2):
        assert batch["amount"].dtype in (pl.Float64, pl.String)
        total += batch.height
    assert total == 3


def test_streaming_csv_headerless_uses_declared_schema(tmp_path):
    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
    ]
    csv = tmp_path / "noheader.csv"
    csv.write_text("1,10.5\n2,20.5\n3,30.5\n", encoding="utf-8")
    engine = StructuralIngestionEngine(
        DataContract(metadata={"name": "stream-noheader", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(
                path=str(csv), format="csv", schema=schema,
                parser=SourceParserOptions(has_header=False),
            )],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    total = 0
    for batch in engine.iter_batches(csv, batch_size=2):
        assert list(batch.columns) == ["id", "amount"]
        total += batch.height
    assert total == 3


def test_streaming_csv_null_token_is_nullified(tmp_path):
    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=True),
    ]
    csv = tmp_path / "nulls.csv"
    csv.write_text("id,amount\n1,10.5\n2,NA\n3,30.5\n", encoding="utf-8")
    engine = StructuralIngestionEngine(
        DataContract(metadata={"name": "stream-nulltok", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(
                path=str(csv), format="csv", schema=schema,
                parser=SourceParserOptions(null_values=["NA"]),
            )],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    # Raw parsed chunks are all-string; strict Tier-1 casting (and null-token
    # coercion) happens when each chunk is ingested.
    total = 0
    null_count = 0
    for batch in engine.iter_batches(csv, batch_size=2):
        res = engine.ingest_dataframe(batch)
        assert res.refused_count == 0
        total += res.conforming_df.height
        null_count += res.conforming_df["amount"].null_count()
    assert total == 3
    assert null_count == 1


# --------------------------------------------------------------------------
# Schema compatibility
# --------------------------------------------------------------------------

def test_strict_schema_compatibility_refuses_missing_column(tmp_path):
    # File is missing the declared (nullable) "currency" column.
    src = tmp_path / "drift.parquet"
    pl.DataFrame({"id": [1, 2], "amount": [10.0, 20.0]}).write_parquet(src)
    schema = [
        SchemaColumn(name="id", polars_type="Int64", nullable=True),
        SchemaColumn(name="amount", polars_type="Float64", nullable=True),
        SchemaColumn(name="currency", polars_type="Utf8", nullable=True),
    ]

    strict = StructuralIngestionEngine(
        DataContract(metadata={"name": "s1", "version": "1.0.0"}, spec={
            "sources": [
                SourceDefinition(
                    path=str(src), schema=schema,
                    schema_compatibility=SchemaCompatibilityPolicy.STRICT,
                )
            ],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    res_strict = strict.ingest_from_file(src)
    assert res_strict.rows_read == 2
    assert res_strict.refused_count == 2  # both rows refused for missing column

    # Default (EVOLVING) back-fills the nullable missing column with null.
    evolving = StructuralIngestionEngine(
        DataContract(metadata={"name": "s2", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(path=str(src), schema=schema)],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })
    )
    res_evolving = evolving.ingest_from_file(src)
    assert res_evolving.refused_count == 0
    assert res_evolving.conforming_df.height == 2
    assert res_evolving.conforming_df["currency"].null_count() == 2


def test_schema_compatibility_invalid_policy_rejected():
    with pytest.raises(ValidationError):
        DataContract(metadata={"name": "bad", "version": "1.0.0"}, spec={
            "sources": [SourceDefinition(path="x", schema=_schema(), schema_compatibility="NOPE")],
            "quality": QualitySpec(), "delivery": DeliverySpec(),
        })


# --------------------------------------------------------------------------
# Batch controls target admitted (deliverable) rows only
# --------------------------------------------------------------------------

def test_batch_controls_exclude_held_and_quarantined_rows(tmp_path, monkeypatch):
    """Uniqueness and control totals run over delivered rows, never the held set.

    A row quarantined by a row rule must not shift a control total or mask a
    duplicate key, and it must not silently count toward satisfying the batch.
    """
    from datashell.core.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")

    contract = _contract(
        "delivered-target-contract",
        batch_rules=[
            # Control total over admitted rows only (10 + 50), not all rows (40).
            BatchRule(rule_id="amount-total", check=BatchRuleCheckType.SUM_EQUALS, column="amount", value=60.0),
            # Duplicate id exists ONLY among the held row, so admitted ids are unique.
            BatchRule(rule_id="id-unique", check=BatchRuleCheckType.UNIQUE, column="id"),
        ],
    )
    records = [
        {"id": 1, "account": "a", "amount": -20.0, "currency": "USD"},  # held: amount > 0 fails
        {"id": 1, "account": "b", "amount": 10.0, "currency": "USD"},   # admitted
        {"id": 2, "account": "c", "amount": 50.0, "currency": "USD"},   # admitted
    ]
    result = DataShellPipeline(contract).run_records(records, target_path_override=str(tmp_path / "out.parquet"))

    assert result.status == TerminalStatus.LOADED
    assert result.ledger.held_quality == 1  # the quarantined row is held, not delivered

    batch_by_id = {b["rule_id"]: b for b in result.audit_certificate["batch_evaluations"]}
    # Held row did NOT drag the control total to 40, and did NOT create a duplicate.
    assert batch_by_id["amount-total"]["passed"] is True
    assert batch_by_id["amount-total"]["actual"] == "60.0"
    assert batch_by_id["id-unique"]["passed"] is True

    # The delivered file contains only the two admitted rows.
    delivered = pl.read_parquet(tmp_path / "out.parquet")
    assert delivered.height == 2
    assert sorted(delivered["id"].to_list()) == [1, 2]

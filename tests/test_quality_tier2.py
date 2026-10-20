import polars as pl

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    QualityRule,
    RowRuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.engines.quality import SemanticQualityEngine


def make_quality_contract():
    return DataContract(
        metadata={"name": "test-quality"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[
                        SchemaColumn(name="account", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
                    ]
                )
            ],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [
                    QualityRule(
                        rule_id="amt-positive",
                        check=RowRuleCheckType.GREATER_THAN,
                        column="amount",
                        value=0
                    ),
                    QualityRule(
                        rule_id="curr-valid",
                        check=RowRuleCheckType.ALLOWED_VALUES,
                        column="currency",
                        value=["USD", "EUR"]
                    ),
                ],
                "batch_rules": [
                    BatchRule(
                        rule_id="min-records",
                        check=BatchRuleCheckType.MIN_ROW_COUNT,
                        value=2
                    )
                ]
            }
        }
    )


def test_quality_rules_evaluation():
    contract = make_quality_contract()
    engine = SemanticQualityEngine(contract)

    df = pl.DataFrame({
        "account": ["A1", "A2", "A3", "A4"],
        "amount": [100.0, -50.0, 200.0, 50.0],  # A2 fails amt-positive
        "currency": ["USD", "USD", "JPY", "EUR"], # A3 fails curr-valid
    })

    res = engine.evaluate(df)
    assert res.total_candidate_rows == 4
    assert res.admitted_count == 2
    assert res.held_count == 2
    assert res.admitted_df["account"].to_list() == ["A1", "A4"]

    # Check stable rule attribution
    held_records = res.held_df.to_dicts()
    assert held_records[0]["account"] == "A2"
    assert held_records[0]["__dm_violated_rule_id"] == "amt-positive"

    assert held_records[1]["account"] == "A3"
    assert held_records[1]["__dm_violated_rule_id"] == "curr-valid"

    # Batch rule: min-records check
    assert res.all_batch_rules_passed is True
    assert len(res.batch_results) == 1
    assert res.batch_results[0].passed is True

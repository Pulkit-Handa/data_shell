"""Exhaustive regression test suite verifying remediation of all audit findings."""


import polars as pl
import pytest

from datashell.contract.models import (
    BatchRule,
    BatchRuleCheckType,
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    ExtraColumnsPolicy,
    QualityBehavior,
    QualityRule,
    QualitySpec,
    RowRuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.contract.registry import ContractRegistry
from datashell.core.sandbox import FilesystemSandbox, PathSecurityViolation
from datashell.engines.ingestion import StructuralIngestionEngine
from datashell.engines.quality import SemanticQualityEngine
from datashell.pipeline.runner import DataShellPipeline


def test_empty_input_does_not_bypass_batch_validation():
    """Audit Finding: Empty inputs must run batch checks (e.g. MIN_ROW_COUNT must fail on 0 rows)."""
    contract = DataContract(
        metadata={"name": "test-empty-batch"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[SchemaColumn(name="val", polars_type="Float64")]
                )
            ],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [],
                "batch_rules": [
                    BatchRule(
                        rule_id="min-volume",
                        check=BatchRuleCheckType.MIN_ROW_COUNT,
                        value=50000
                    )
                ]
            }
        }
    )

    empty_df = pl.DataFrame({"val": []}, schema={"val": pl.Float64})
    engine = SemanticQualityEngine(contract)
    res = engine.evaluate(empty_df)

    # Must FAIL minimum volume
    assert res.all_batch_rules_passed is False
    assert res.batch_results[0].passed is False
    assert res.batch_results[0].actual_value == "0"
    assert res.should_reject_run is True


def test_quality_policy_warn_does_not_drop_rows():
    """Audit Finding: WARN policy must record violations but NOT drop rows from delivery."""
    contract = DataContract(
        metadata={"name": "test-warn-policy"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[
                        SchemaColumn(name="id", polars_type="Utf8"),
                        SchemaColumn(name="score", polars_type="Float64")
                    ]
                )
            ],
            "quality": {
                "behavior": "WARN",
                "rules": [
                    QualityRule(
                        rule_id="score-positive",
                        check=RowRuleCheckType.GREATER_THAN,
                        column="score",
                        value=0.0
                    )
                ]
            }
        }
    )

    df = pl.DataFrame({
        "id": ["row-1", "row-2"],
        "score": [10.0, -5.0]  # row-2 violates score-positive
    })
    engine = SemanticQualityEngine(contract)
    res = engine.evaluate(df)

    # In WARN mode: all rows admitted, held count is 0, but violation is tracked
    assert res.admitted_count == 2
    assert res.held_count == 0
    assert res.rule_violation_counts["score-positive"] == 1
    assert res.should_reject_run is False


def test_reject_run_on_row_failures_stops_delivery_and_records_zero_delivered(tmp_path):
    """Audit Finding: REJECT_RUN with row failures must reject the whole run,
    record 0 delivered, and preserve quarantine."""
    target_out = tmp_path / "out.parquet"
    contract = DataContract(
        metadata={"name": "test-reject-row"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[
                        SchemaColumn(name="account", polars_type="Utf8"),
                        SchemaColumn(name="amount", polars_type="Float64")
                    ]
                )
            ],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [
                    QualityRule(
                        rule_id="amount-positive",
                        check=RowRuleCheckType.GREATER_THAN,
                        column="amount",
                        value=0.0
                    )
                ]
            },
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path=str(target_out), mode="overwrite")],
                quarantinePath=str(tmp_path / "quarantine")
            )
        }
    )

    records = [
        {"account": "ACC-1", "amount": 100.0},
        {"account": "ACC-2", "amount": -50.0}, # Violates amount-positive
    ]

    pipeline = DataShellPipeline(contract)
    res = pipeline.run_records(records)

    # Run must be REJECTED
    assert res.status.value == "REJECTED"
    assert res.outcome.rows_delivered == 0
    assert res.ledger.actual_delivered == 0
    assert res.ledger.rows_to_deliver == 1  # 1 was candidate, but rejected
    assert not target_out.exists()          # Target must NOT be written

    # Quarantine evidence MUST still be preserved!
    quarantine_files = list((tmp_path / "quarantine").glob("*.parquet"))
    assert len(quarantine_files) > 0


def test_filesystem_sandbox_rejects_arbitrary_and_system_paths():
    """Audit Finding: FilesystemSandbox must prevent paths pointing outside approved storage or into system dirs."""
    with pytest.raises(PathSecurityViolation):
        FilesystemSandbox.sanitize_and_validate_path("/etc/passwd")

    with pytest.raises(PathSecurityViolation):
        FilesystemSandbox.sanitize_and_validate_path("../../contracts_registry/corrupt.json")


def test_strict_non_lossy_scalar_ingestion():
    """Audit Finding: Ingestion must reject lossy float-to-int conversions and integer overflows."""
    contract = DataContract(
        metadata={"name": "test-strict-types"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[
                        SchemaColumn(name="small_int", polars_type="Int8", nullable=False),
                        SchemaColumn(name="strict_int", polars_type="Int64", nullable=False),
                    ],
                    extra_columns_policy=ExtraColumnsPolicy.REJECT
                )
            ]
        }
    )

    engine = StructuralIngestionEngine(contract)

    records = [
        # Valid
        {"small_int": 50, "strict_int": 1000},
        # Lossy float (3.8 cannot be integer)
        {"small_int": 10, "strict_int": 3.8},
        # Int8 overflow (200 > 127)
        {"small_int": 200, "strict_int": 1000},
        # Extra unexpected column rejected
        {"small_int": 10, "strict_int": 20, "unauthorized_extra": "hack"},
    ]

    res = engine.ingest_records(records)
    assert res.rows_read == 4
    assert res.refused_count == 3
    assert res.conforming_df.height == 1


def test_contract_registry_immutability_and_atomic_versioning(tmp_path):
    """Audit Finding: Registry must enforce version immutability and prevent modifying published versions."""
    registry = ContractRegistry(tmp_path)

    contract_v1 = DataContract(
        metadata={"name": "immutable-contract", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="id", polars_type="Utf8")])],
            "quality": QualitySpec(behavior=QualityBehavior.DROP_ROW)
        }
    )
    registry.save_contract(contract_v1)

    # Attempting to re-save version 1.0.0 with altered rules must be rejected
    mutated_v1 = DataContract(
        metadata={"name": "immutable-contract", "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="id", polars_type="Utf8")])],
            "quality": QualitySpec(behavior=QualityBehavior.REJECT_RUN) # Altered!
        }
    )
    with pytest.raises(ValueError) as exc:
        registry.save_contract(mutated_v1)
    assert "already exists and is immutable" in str(exc.value)


def test_cross_field_contract_validation_rejects_missing_columns():
    """Audit Finding: Rules referencing columns not in declared schema must fail contract validation."""
    with pytest.raises(ValueError) as exc:
        DataContract(
            metadata={"name": "bad-ref"},
            spec={
                "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="real_col", polars_type="Utf8")])],
                "quality": {
                    "rules": [
                        QualityRule(
                            rule_id="r1",
                            check=RowRuleCheckType.GREATER_THAN,
                            column="non_existent_column",
                            value=10
                        )
                    ]
                }
            }
        )
    assert "non_existent_column" in str(exc.value)


def test_atomic_delivery_leaves_no_staged_artifacts(tmp_path):
    """Audit Finding: Atomic delivery must stage then rename, leaving zero lingering .staged_ files."""
    from datashell.engines.delivery import DeliveryEngine, TerminalStatus
    from datashell.engines.reconciler import Reconciler

    target_path = tmp_path / "final_data.parquet"
    contract = DataContract(
        metadata={"name": "atomic-test"},
        spec={
            "sources": [SourceDefinition(path="dummy", schema=[SchemaColumn(name="val", polars_type="Int64")])],
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path=str(target_path), mode="overwrite")]
            )
        }
    )
    delivery = DeliveryEngine(contract)
    df_admitted = pl.DataFrame({"val": [1, 2, 3]})
    df_held = pl.DataFrame({"val": [4], "__dm_violated_rule_id": ["r"]})
    ledger = Reconciler.balance("run-atomic", 4, 0, 1, 3)

    outcome = delivery.deliver(df_admitted, df_held, [], ledger, True)
    assert outcome.status == TerminalStatus.LOADED
    assert target_path.exists()

    # Verify no .staged_* files remain
    staged_files = list(tmp_path.glob(".staged_*"))
    assert len(staged_files) == 0


def test_cli_exit_code_on_rejection(tmp_path, monkeypatch):
    """Audit Finding: CLI must exit with code 2 on REJECTED status, not 0."""
    import json

    from datashell.cli.main import main

    # Create contract JSON that rejects run on violations
    contract_path = tmp_path / "contract.json"
    data_path = tmp_path / "data.csv"
    output_path = tmp_path / "output.parquet"

    contract_data = {
        "apiVersion": "daas/v1",
        "kind": "DataProduct",
        "metadata": {"name": "cli-test", "version": "1.0.0"},
        "spec": {
            "sources": [{
                "path": str(data_path),
                "schema": [
                    {"name": "val", "polars_type": "Float64", "nullable": False}
                ]
            }],
            "quality": {
                "behavior": "REJECT_RUN",
                "rules": [
                    {"rule_id": "positive", "check": "GREATER_THAN", "column": "val", "value": 0.0}
                ]
            },
            "delivery": {
                "targets": [{"path": str(output_path), "mode": "overwrite"}]
            }
        }
    }
    with open(contract_path, "w") as f:
        json.dump(contract_data, f)

    # Negative value will cause REJECT_RUN
    with open(data_path, "w") as f:
        f.write("val\n-10.0\n")

    monkeypatch.setattr("sys.argv", [
        "datashell", "run",
        "--contract", str(contract_path),
        "--source", str(data_path)
    ])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2  # Exit code 2 for REJECTED!


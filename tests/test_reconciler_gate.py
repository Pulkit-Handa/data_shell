import pytest
import polars as pl
from datashell.contract.models import (
    DataContract, SourceDefinition, SchemaColumn, DeliverySpec, DeliveryTarget
)
from datashell.engines.reconciler import Reconciler, ReconciliationFootingError
from datashell.engines.delivery import DeliveryEngine, TerminalStatus


def test_reconciler_balances_perfectly():
    ledger = Reconciler.balance(
        run_id="run-1",
        rows_read=1000,
        refused_structural=50,
        held_quality=150,
        rows_to_deliver=800
    )
    assert ledger.footing_matched is True
    assert ledger.unaccounted_for == 0
    assert ledger.pct_delivered == 80.0
    assert ledger.pct_refused_or_held == 20.0


def test_reconciler_detects_deficit():
    # 1000 read != 50 + 150 + 799 (missing 1 row!)
    ledger = Reconciler.balance(
        run_id="run-deficit",
        rows_read=1000,
        refused_structural=50,
        held_quality=150,
        rows_to_deliver=799
    )
    assert ledger.footing_matched is False
    assert ledger.unaccounted_for == 1


def test_delivery_engine_refuses_write_when_arithmetic_does_not_foot(tmp_path):
    contract = DataContract(
        metadata={"name": "test-delivery"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[SchemaColumn(name="x", polars_type="Int64")]
                )
            ],
            "delivery": DeliverySpec(
                targets=[DeliveryTarget(path=str(tmp_path / "out.parquet"))]
            )
        }
    )
    delivery = DeliveryEngine(contract)

    # Balanced ledger passes
    balanced_ledger = Reconciler.balance("r1", 10, 2, 3, 5)
    df_admitted = pl.DataFrame({"x": [1, 2, 3, 4, 5]})
    df_held = pl.DataFrame({"x": [6, 7, 8], "__dm_violated_rule_id": ["r", "r", "r"]})
    
    outcome = delivery.deliver(df_admitted, df_held, [], balanced_ledger, True)
    assert outcome.status == TerminalStatus.LOADED
    assert (tmp_path / "out.parquet").exists()

    # Unbalanced ledger REFUSES TO WRITE
    unbalanced_ledger = Reconciler.balance("r2", 10, 2, 3, 4) # 1 row unaccounted!
    with pytest.raises(ReconciliationFootingError) as exc:
        delivery.deliver(df_admitted, df_held, [], unbalanced_ledger, True)
    assert "DELIVERY REFUSED TO WRITE" in str(exc.value)

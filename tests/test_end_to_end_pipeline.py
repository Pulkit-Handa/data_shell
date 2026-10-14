import pytest
import json
from pathlib import Path
import polars as pl
from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline
from datashell.engines.delivery import TerminalStatus


def test_end_to_end_boundary_enforcement(tmp_path):
    # 1. Load contract
    contract_path = Path(__file__).parent.parent / "examples" / "vendor_settlements_contract.json"
    with open(contract_path, "r") as f:
        contract_data = json.load(f)
    contract = DataContract(**contract_data)

    # 2. Seed data with known defect counts:
    # 10 records total:
    # - 2 structural rejections (corrupt float, unparseable amount)
    # - 3 quality rule rejections (1 negative amount, 2 unallowed currency)
    # - 5 valid rows delivered
    records = [
        # Conforming & Admitted (5 rows)
        {"settlement_id": "S-101", "amount": 1500.0, "currency": "USD"},
        {"settlement_id": "S-102", "amount": 250.75, "currency": "EUR"},
        {"settlement_id": "S-103", "amount": 99.0, "currency": "GBP"},
        {"settlement_id": "S-104", "amount": 4200.0, "currency": "USD"},
        {"settlement_id": "S-105", "amount": 18.5, "currency": "EUR"},
        # Semantic Violations (3 rows)
        {"settlement_id": "S-201", "amount": -50.0, "currency": "USD"},      # Rule: amount-positive
        {"settlement_id": "S-202", "amount": 300.0, "currency": "INR"},      # Rule: currency-settled
        {"settlement_id": "S-203", "amount": 80.0, "currency": "CAD"},       # Rule: currency-settled
        # Structural Violations (2 rows)
        {"settlement_id": "S-301", "amount": "invalid_num", "currency": "USD"}, # Uncastable float
        {"settlement_id": None, "amount": 100.0, "currency": "USD"},            # Missing non-nullable ID
    ]

    input_file = tmp_path / "incoming_feed.jsonl"
    with open(input_file, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    out_target = tmp_path / "delivered_settlements.parquet"
    out_audit = tmp_path / "run_audit.json"

    # 3. Execute DataShell Pipeline
    pipeline = DataShellPipeline(contract)
    result = pipeline.run_file(
        source_path=input_file,
        target_path_override=str(out_target),
        audit_output_path=str(out_audit)
    )

    # 4. Verify Mathematical Reconciliation Axiom:
    # Rows Read (10) = Structural (2) + Semantic Held (3) + Delivered (5)
    ledger = result.ledger
    assert ledger.rows_read == 10
    assert ledger.refused_structural == 2
    assert ledger.held_quality == 3
    assert ledger.rows_to_deliver == 5
    assert ledger.unaccounted_for == 0
    assert ledger.footing_matched is True
    assert result.status == TerminalStatus.LOADED

    # 5. Verify Delivered Parquet
    delivered_df = pl.read_parquet(out_target)
    assert delivered_df.height == 5
    assert "S-101" in delivered_df["settlement_id"].to_list()
    assert "S-105" in delivered_df["settlement_id"].to_list()

    # 6. Verify Audit Artifact
    assert out_audit.exists()
    with open(out_audit) as f:
        audit = json.load(f)
    assert audit["reconciliation"]["unaccounted_for"] == 0
    assert audit["reconciliation"]["footing_matched"] is True
    assert audit["rule_breakdown"]["amount-positive"] == 1
    assert audit["rule_breakdown"]["currency-settled"] == 2

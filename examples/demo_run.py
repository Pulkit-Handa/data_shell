#!/usr/bin/env python3
"""
Data Shell Demonstration Run.
Simulates a vendor feed arriving at the boundary:
- Rejects unparseable/malformed records at the door (Tier 1 Structural).
- Evaluates business rules via Polars and batch gates via DuckDB (Tier 2 Semantic).
- Asserts strict zero-deficit reconciliation before writing.
"""

import json
from pathlib import Path

from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline


def run_demo():
    print("=" * 65)
    print("      DATA SHELL — BOUNDARY ENFORCEMENT DEMO")
    print("         'Bad data never lands, and never leaves'")
    print("=" * 65)

    base_dir = Path(__file__).parent.parent
    contract_file = base_dir / "examples" / "vendor_settlements_contract.json"
    
    with open(contract_file) as f:
        contract_data = json.load(f)
    contract = DataContract(**contract_data)

    print(f"\n[1] Loaded Contract: '{contract.metadata.name}' v{contract.metadata.version}")
    print(f"    Owner: {contract.metadata.owner}")
    print(f"    Row Rules: {[r.rule_id for r in contract.spec.quality.rules]}")
    print(f"    Batch Rules: {[b.rule_id for b in contract.spec.quality.batch_rules]}")

    # Generate 1,000 synthetic feed records with seeded defects
    print("\n[2] Generating 1,000 incoming settlement records with seeded defects...")
    records = []
    
    # 900 perfectly valid records
    for i in range(1, 901):
        records.append({
            "settlement_id": f"SETTLE-{1000 + i}",
            "amount": round(10.0 + (i * 1.5), 2),
            "currency": "USD" if i % 2 == 0 else "EUR"
        })

    # 40 structural defects (unrealizable types / missing IDs)
    for i in range(1, 21):
        records.append({
            "settlement_id": f"SETTLE-BAD-AMT-{i}",
            "amount": "NOT_A_FLOAT",
            "currency": "USD"
        })
    for _ in range(21, 41):
        records.append({
            "settlement_id": None, # Non-nullable field violation
            "amount": 250.0,
            "currency": "GBP"
        })

    # 60 semantic quality defects
    # 35 negative amounts
    for i in range(1, 36):
        records.append({
            "settlement_id": f"SETTLE-NEG-{i}",
            "amount": -50.0,
            "currency": "USD"
        })
    # 25 unallowed currencies
    for i in range(36, 61):
        records.append({
            "settlement_id": f"SETTLE-CURR-{i}",
            "amount": 120.0,
            "currency": "JPY" # Not in allowed USD, EUR, GBP
        })

    total_records = len(records)
    print(f"    Total Feed Records: {total_records:,}")
    print("    - Seeded Structural Defects: 40")
    print("    - Seeded Semantic Defects:   60")
    print("    - Valid Conforming Records: 900")

    # Run boundary pipeline
    print("\n[3] Executing Data Shell Boundary Pipeline...")
    pipeline = DataShellPipeline(contract)
    
    result = pipeline.run_records(
        records=records,
        target_path_override="./output/curated_settlements.parquet",
        audit_output_path="./output/run_audit.json"
    )

    print("\n" + result.ledger.summary_table())

    print("\n[4] Execution Summary:")
    print(f"    Terminal Status:   {result.status.value}")
    print(f"    Delivered Records: {result.outcome.rows_delivered:,} rows -> {result.outcome.target_path}")
    print(f"    Quarantine Folder: {result.outcome.quarantine_path}")
    print(f"    Audit Artifact:    {result.audit_artifact_path}")
    print(f"    Execution Time:    {result.duration_seconds:.4f}s")

    print("\n[5] Detailed Rule Breakdown of Held Records:")
    for rule_id, count in result.ledger.rule_breakdown.items():
        print(f"    - {rule_id:<20}: {count:>4} held")

    print("\n[6] Detailed Structural Rejections at Ingestion:")
    for reason, count in result.ledger.structural_reasons.items():
        print(f"    - {reason:<20}: {count:>4} refused")

    print("\n✓ SUCCESS: Arithmetic matched to 0 unaccounted records. "
          "Target and lineage artifacts published with crash recovery.")


if __name__ == "__main__":
    run_demo()

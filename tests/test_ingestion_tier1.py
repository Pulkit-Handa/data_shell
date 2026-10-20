from datashell.contract.models import DataContract, SchemaColumn, SourceDefinition
from datashell.engines.ingestion import StructuralIngestionEngine


def make_dummy_contract():
    return DataContract(
        metadata={"name": "test-ingest"},
        spec={
            "sources": [
                SourceDefinition(
                    path="dummy",
                    schema=[
                        SchemaColumn(name="id", polars_type="Utf8", nullable=False),
                        SchemaColumn(name="val", polars_type="Float64", nullable=True),
                        SchemaColumn(name="active", polars_type="Boolean", nullable=False),
                    ]
                )
            ]
        }
    )


def test_structural_conformance_success():
    contract = make_dummy_contract()
    engine = StructuralIngestionEngine(contract)

    records = [
        {"id": "rec-1", "val": 12.5, "active": True},
        {"id": "rec-2", "val": "45.2", "active": "false"},
        {"id": "rec-3", "val": None, "active": 1},
    ]

    result = engine.ingest_records(records)
    assert result.rows_read == 3
    assert result.refused_count == 0
    assert result.conforming_df.height == 3
    assert result.conforming_df["val"].to_list() == [12.5, 45.2, None]


def test_structural_rejection_unrealizable_type():
    contract = make_dummy_contract()
    engine = StructuralIngestionEngine(contract)

    records = [
        {"id": "rec-valid", "val": 10.0, "active": True},
        {"id": "rec-bad-float", "val": "not-a-number", "active": True},
        {"id": None, "val": 5.0, "active": True},  # Non-nullable null
        {"id": "rec-bad-bool", "val": 20.0, "active": "unparseable_bool"},
    ]

    result = engine.ingest_records(records)
    assert result.rows_read == 4
    assert result.refused_count == 3
    assert result.conforming_df.height == 1
    assert result.conforming_df["id"][0] == "rec-valid"
    assert len(result.rejections) == 3

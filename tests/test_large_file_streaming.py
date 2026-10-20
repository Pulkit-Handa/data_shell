"""Large-file handling: bounded-memory streaming ingestion across all formats.

These tests exercise the out-of-core paths that keep the gateway memory-bounded
for arbitrarily large CSV, JSONL, and Parquet inputs, and the hard-linked
lineage publication that avoids duplicating delivered bytes on disk.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest

from datashell.contract.models import (
    DataContract,
    DeliverySpec,
    DeliveryTarget,
    QualityBehavior,
    QualityRule,
    QualitySpec,
    RuleCheckType,
    SchemaColumn,
    SourceDefinition,
)
from datashell.core.config import settings
from datashell.core.fileops import materialize_artifact_copy
from datashell.engines.delivery import TerminalStatus
from datashell.engines.ingestion import StructuralIngestionEngine
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.pipeline.runner import DataShellPipeline


def _schema():
    return [
        SchemaColumn(name="id", polars_type="Int64", nullable=False),
        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
        SchemaColumn(name="currency", polars_type="Utf8", nullable=False),
    ]


def _write_csv(path: Path, rows: int, *, bad_amount_at: int | None = None, negative_at: int | None = None):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "amount", "currency"])
        for i in range(rows):
            amount = 10.0 + (i % 25)
            if bad_amount_at is not None and i == bad_amount_at:
                amount = "not-a-number"
            elif negative_at is not None and i == negative_at:
                amount = -1.0
            writer.writerow([i, amount, "USD"])


def _write_jsonl(path: Path, rows: int, *, bad_amount_at: int | None = None, negative_at: int | None = None):
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(rows):
            amount = 10.0 + (i % 25)
            if bad_amount_at is not None and i == bad_amount_at:
                amount = "invalid"
            elif negative_at is not None and i == negative_at:
                amount = -1.0
            handle.write(json.dumps({"id": i, "amount": amount, "currency": "EUR"}) + "\n")


def _contract(name: str, source: Path, target: Path) -> DataContract:
    return DataContract(
        metadata={"name": name, "version": "1.0.0"},
        spec={
            "sources": [SourceDefinition(path=str(source), schema=_schema())],
            "quality": QualitySpec(
                behavior=QualityBehavior.DROP_ROW,
                rules=[
                    QualityRule(
                        rule_id="amount-positive",
                        column="amount",
                        check=RuleCheckType.GREATER_THAN,
                        value=0.0,
                    )
                ],
            ),
            "delivery": DeliverySpec(targets=[DeliveryTarget(path=str(target))]),
        },
    )


def _isolate_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")


def test_iter_batches_streams_all_formats_in_bounded_chunks(tmp_path):
    rows = 5_000
    engine = StructuralIngestionEngine(
        _contract("iter", tmp_path / "unused", tmp_path / "unused_out.parquet")
    )

    parquet_path = tmp_path / "data.parquet"
    pl.DataFrame(
        {
            "id": list(range(rows)),
            "amount": [float(i) for i in range(rows)],
            "currency": ["USD"] * rows,
        }
    ).write_parquet(parquet_path)
    _write_csv(tmp_path / "data.csv", rows)
    _write_jsonl(tmp_path / "data.jsonl", rows)

    for name in ("data.parquet", "data.csv", "data.jsonl"):
        batches = list(engine.iter_batches(tmp_path / name, batch_size=512))
        assert sum(batch.height for batch in batches) == rows
        assert all(batch.height <= 512 for batch in batches)
        assert all("id" in batch.columns for batch in batches)
        # CSV is read as strings so strict Tier-1 casting owns type validation.
        if name.endswith(".csv"):
            assert batches[0].schema["amount"] == pl.String


def test_ingest_dataframe_fast_path_matches_declared_schema():
    engine = StructuralIngestionEngine(
        _contract("fast", Path("unused"), Path("unused_out.parquet"))
    )
    matching = pl.DataFrame(
        {"id": [1, 2, 3], "amount": [1.0, 2.0, 3.0], "currency": ["USD", "EUR", "GBP"]}
    )
    result = engine.ingest_dataframe(matching)
    assert result.refused_count == 0
    assert result.rows_read == 3
    assert result.conforming_df.height == 3
    assert result.conforming_df.schema["id"] == pl.Int64

    mismatched = pl.DataFrame(
        {"id": ["1", "bad"], "amount": ["1.0", "2.0"], "currency": ["USD", "EUR"]}
    )
    fallback = engine.ingest_dataframe(mismatched)
    assert fallback.refused_count == 1
    assert fallback.conforming_df.height == 1


def test_ingest_dataframe_falls_back_on_non_finite_floats():
    engine = StructuralIngestionEngine(
        _contract("nonfinite", Path("unused"), Path("unused_out.parquet"))
    )
    df = pl.DataFrame(
        {"id": [1, 2], "amount": [1.0, float("nan")], "currency": ["USD", "USD"]}
    )
    result = engine.ingest_dataframe(df)
    # NaN is not null, so the fast path must detect it and defer to the strict
    # per-row validator, which ejects the non-finite value.
    assert result.refused_count == 1
    assert result.conforming_df.height == 1


def test_streaming_flushes_bounded_row_groups(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "source.parquet"
    pl.DataFrame(
        {"id": list(range(1_000)), "amount": [1.0] * 1_000, "currency": ["USD"] * 1_000}
    ).write_parquet(source)
    target = tmp_path / "delivered.parquet"
    pipeline = DataShellPipeline(
        _contract("rowgroups", source, target), tenant_id="tenant-rowgroups"
    )
    result = pipeline.run_streaming(source_path=source, chunk_size=100)
    assert result.status == TerminalStatus.LOADED
    assert pl.read_parquet(target).height == 1_000
    # Each micro-batch is flushed as its own row group so staging memory stays
    # bounded by one batch instead of pyarrow's default megabyte-scale group.
    assert pq.ParquetFile(target).num_row_groups >= 5


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_run_streaming_handles_csv_and_jsonl(tmp_path, monkeypatch, fmt):
    _isolate_storage(monkeypatch, tmp_path)
    rows = 20_000
    source = tmp_path / f"source.{fmt}"
    if fmt == "csv":
        _write_csv(source, rows, bad_amount_at=111, negative_at=222)
    else:
        _write_jsonl(source, rows, bad_amount_at=111, negative_at=222)

    target = tmp_path / f"delivered.{fmt}.parquet"
    contract = _contract(f"stream-{fmt}", source, target)
    pipeline = DataShellPipeline(contract, tenant_id=f"tenant-{fmt}")
    result = pipeline.run_streaming(source_path=source, chunk_size=1024)

    assert result.status == TerminalStatus.LOADED
    assert result.ledger.rows_read == rows
    assert result.ledger.refused_structural == 1
    assert result.ledger.held_quality == 1
    assert result.ledger.footing_matched is True
    assert result.outcome.rows_delivered == rows - 2
    assert pl.read_parquet(target).height == rows - 2


def test_lineage_artifact_prefers_hardlink_without_losing_verification(tmp_path, monkeypatch):
    _isolate_storage(monkeypatch, tmp_path)
    source = tmp_path / "source.parquet"
    pl.DataFrame({"id": list(range(500)), "amount": [1.0] * 500, "currency": ["USD"] * 500}).write_parquet(source)

    target = tmp_path / "delivered.parquet"
    contract = _contract("lineage-hardlink", source, target)
    pipeline = DataShellPipeline(contract, tenant_id="tenant-lineage")
    result = pipeline.run_streaming(source_path=source, chunk_size=128)
    assert result.status == TerminalStatus.LOADED

    lineage = Path(result.audit_certificate["cryptographic_manifest"]["run_artifact_path"])
    assert target.read_bytes() == lineage.read_bytes()
    # On a filesystem that supports links the two artifacts share one inode.
    if os.stat(target).st_nlink > 1:
        assert os.stat(target).st_ino == os.stat(lineage).st_ino

    verification = ArtifactGenerator.verify_certificate_and_artifacts(result.audit_certificate)
    assert verification["status"] == "AUTHENTIC_AND_UNMODIFIED"

    lineage.unlink()
    after = ArtifactGenerator.verify_certificate_and_artifacts(result.audit_certificate)
    assert after["artifact_details"]["run_lineage_artifact"]["status"] == "MISSING"


def test_materialize_artifact_copy_content_addressed(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload-bytes" * 4096)
    destination = tmp_path / "copy.bin"
    materialize_artifact_copy(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    # Overwriting an existing destination must still yield matching content.
    destination.write_bytes(b"stale")
    materialize_artifact_copy(source, destination)
    assert destination.read_bytes() == source.read_bytes()


def test_streaming_threshold_setting_is_positive():
    assert settings.stream_auto_threshold_bytes >= 0
    assert settings.stream_chunk_rows >= 1

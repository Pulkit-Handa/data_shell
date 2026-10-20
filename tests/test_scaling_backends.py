"""Scaling-backend selection and delivery-compression configuration.

The pipeline has exactly two ingest backends — in-memory vectorization and
out-of-core micro-batch streaming — plus one delivered-artifact codec. Which
one serves a run must be a deployment decision expressed through environment
variables (``DATASHELL_INGEST_MODE``, ``DATASHELL_DELIVERY_COMPRESSION``,
``DATASHELL_DUCKDB_THREADS``), never hidden in caller code. These tests pin
that decision surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from datashell.core.config import EnterpriseSettings, settings
from datashell.core.fileops import parquet_compression_codec
from datashell.pipeline.runner import (
    EXECUTION_IN_MEMORY,
    EXECUTION_STREAMING,
    choose_execution_backend,
)

THRESHOLD = 64 * 1024 * 1024


def test_choose_execution_backend_explicit_caller_decision_wins() -> None:
    """An explicit streaming/memory request beats both env policy and size."""
    assert choose_execution_backend(
        1, force_streaming=True, ingest_mode="memory",
        auto_threshold_bytes=THRESHOLD) == EXECUTION_STREAMING
    assert choose_execution_backend(
        THRESHOLD + 1, force_streaming=False, ingest_mode="stream",
        auto_threshold_bytes=THRESHOLD) == EXECUTION_IN_MEMORY


def test_choose_execution_backend_ingest_mode_policy() -> None:
    """DATASHELL_INGEST_MODE pins the backend when the caller has no opinion."""
    assert choose_execution_backend(
        1, ingest_mode="memory", auto_threshold_bytes=THRESHOLD) == EXECUTION_IN_MEMORY
    assert choose_execution_backend(
        1, ingest_mode="stream", auto_threshold_bytes=THRESHOLD) == EXECUTION_STREAMING


def test_choose_execution_backend_auto_promotes_by_size() -> None:
    """Without any other signal, only sources at/above the threshold stream."""
    assert choose_execution_backend(
        0, ingest_mode="auto", auto_threshold_bytes=THRESHOLD) == EXECUTION_IN_MEMORY
    assert choose_execution_backend(
        THRESHOLD - 1, ingest_mode="auto", auto_threshold_bytes=THRESHOLD) == EXECUTION_IN_MEMORY
    assert choose_execution_backend(
        THRESHOLD, ingest_mode="auto", auto_threshold_bytes=THRESHOLD) == EXECUTION_STREAMING


def test_settings_reject_invalid_ingest_mode() -> None:
    with pytest.raises(RuntimeError, match="DATASHELL_INGEST_MODE"):
        EnterpriseSettings(ingest_mode="turbo")


def test_settings_reject_invalid_delivery_compression() -> None:
    with pytest.raises(RuntimeError, match="DATASHELL_DELIVERY_COMPRESSION"):
        EnterpriseSettings(delivery_compression="lzma")


def test_settings_reject_invalid_duckdb_threads() -> None:
    with pytest.raises(RuntimeError, match="DATASHELL_DUCKDB_THREADS"):
        EnterpriseSettings(duckdb_threads=0)
    assert EnterpriseSettings(duckdb_threads=8).duckdb_threads == 8


def test_parquet_compression_codec_normalization() -> None:
    """Case-insensitive mapping with an explicit uncompressed spelling."""
    assert parquet_compression_codec("ZSTD") == "zstd"
    assert parquet_compression_codec(" Snappy ") == "snappy"
    assert parquet_compression_codec("uncompressed") is None
    with pytest.raises(ValueError, match="DATASHELL_DELIVERY_COMPRESSION"):
        parquet_compression_codec("lzma")


def test_parquet_compression_codec_reads_live_settings() -> None:
    """No argument resolves through settings so reconfiguration sticks."""
    original = settings.delivery_compression
    try:
        settings.delivery_compression = "snappy"
        assert parquet_compression_codec() == "snappy"
        settings.delivery_compression = "uncompressed"
        assert parquet_compression_codec() is None
    finally:
        settings.delivery_compression = original


def _mini_contract() -> dict:
    """A smallest-end-to-end contract derived from the vendor example."""
    contract_path = Path(__file__).parent.parent / "examples" / "vendor_settlements_contract.json"
    return json.loads(contract_path.read_text(encoding="utf-8"))


def _run_mini_feed(input_file: Path, target: Path, audit: Path) -> None:
    from datashell.contract.models import DataContract
    from datashell.pipeline.runner import DataShellPipeline

    records = [
        {"settlement_id": f"S-{i}", "amount": 100.0 + i, "currency": "USD"}
        for i in range(1, 7)
    ]
    with open(input_file, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    contract = DataContract(**_mini_contract())
    pipeline = DataShellPipeline(contract)
    result = pipeline.run_file(
        source_path=input_file,
        target_path_override=str(target),
        audit_output_path=str(audit),
    )
    assert result.status.value == "LOADED"


def test_delivery_compression_is_applied_to_delivered_target(tmp_path, monkeypatch) -> None:
    """The configured codec is actually what lands in the delivered Parquet."""
    input_file = tmp_path / "feed.jsonl"
    expected = {
        "snappy": "SNAPPY",
        "zstd": "ZSTD",
        "uncompressed": "UNCOMPRESSED",
    }
    for configured, artifact_codec in expected.items():
        monkeypatch.setattr(settings, "delivery_compression", configured)
        target = tmp_path / f"delivered_{configured}.parquet"
        audit = tmp_path / f"audit_{configured}.json"
        _run_mini_feed(input_file, target, audit)
        parquet_metadata = pq.ParquetFile(target).metadata
        codecs = {
            parquet_metadata.row_group(rg).column(col).compression
            for rg in range(parquet_metadata.num_row_groups)
            for col in range(parquet_metadata.num_columns)
        }
        assert codecs == {artifact_codec}, configured


def test_default_delivery_codec_is_zstd() -> None:
    """Runs previously mixed zstd (in-memory) and snappy (streaming) output.

    The unified default keeps in-memory deliveries byte-compatible with the
    1.0 baseline and shrinks streaming deliveries that used to be snappy.
    """
    assert EnterpriseSettings().delivery_compression == "zstd"

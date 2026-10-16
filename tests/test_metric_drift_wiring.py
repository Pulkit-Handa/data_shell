"""Regression tests for P0 correctness fixes.

1. Persisted per-feed metrics are actually compared run-over-run by the runner
   (metric drift), not silently passed as empty dicts.
2. Every execution mode binds the audit certificate to a real source-payload
   digest; the ``IN_MEMORY_PAYLOAD`` placeholder is never emitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
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
from datashell.engines.delivery import TerminalStatus
from datashell.pipeline.runner import DataShellPipeline


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")


def _contract(name: str, source: Path, target: Path) -> DataContract:
    return DataContract(
        metadata={"name": name, "version": "1.0.0"},
        spec={
            "sources": [
                SourceDefinition(
                    path=str(source),
                    schema=[
                        SchemaColumn(name="id", polars_type="Int64", nullable=False),
                        SchemaColumn(name="amount", polars_type="Float64", nullable=False),
                    ],
                )
            ],
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


def _records(amount: float, rows: int = 50):
    return [{"id": i, "amount": amount} for i in range(rows)]


def test_runner_persists_feed_metrics_and_fires_metric_drift(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("drift-metric-contract", source, target)

    # Run 1 establishes the baseline with amount = 10.0 (avg_amount == 10.0).
    first = DataShellPipeline(contract).run_records(_records(10.0))
    assert first.status == TerminalStatus.LOADED

    # The persisted baseline must actually carry our computed metrics.
    baseline = DataShellPipeline(contract).history_tracker.get_last_run(
        "drift-metric-contract", "default-tenant"
    )
    assert baseline is not None
    assert baseline["metrics"]["avg_amount"] == pytest.approx(10.0)
    assert baseline["metrics"]["sum_amount"] == pytest.approx(500.0)

    # Run 2 keeps the same row count (volume stable) but shifts avg_amount 10x,
    # far beyond the 50% metric threshold, so metric drift must trip and reject.
    second = DataShellPipeline(contract).run_records(_records(100.0))
    assert second.status == TerminalStatus.REJECTED
    metric_checks = [d for d in second.audit_certificate["drift_evaluations"] if "avg_amount" in d["rule_id"]]
    assert metric_checks, second.audit_certificate["drift_evaluations"]
    assert metric_checks[0]["passed"] is False
    assert metric_checks[0]["current_value"] == pytest.approx(100.0)
    assert metric_checks[0]["baseline_value"] == pytest.approx(10.0)


def test_metric_drift_dormant_until_minimum_baseline_samples(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "drift_metric_min_baseline_samples", 2)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("drift-dormant-contract", source, target)

    first = DataShellPipeline(contract).run_records(_records(10.0))
    assert first.status == TerminalStatus.LOADED

    # Only one delivered baseline exists, so metric comparisons stay dormant and
    # the shifted feed is still delivered (warn-first during learning).
    second = DataShellPipeline(contract).run_records(_records(100.0))
    assert second.status == TerminalStatus.LOADED
    assert not [d for d in second.audit_certificate["drift_evaluations"] if "avg_amount" in d["rule_id"]]


def test_run_records_binds_real_source_digest(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("digest-contract", source, target)

    records = _records(10.0)
    result = DataShellPipeline(contract).run_records(records)
    manifest = result.audit_certificate["cryptographic_manifest"]
    digest = manifest["source_payload_sha256"]

    assert digest != "IN_MEMORY_PAYLOAD"
    assert len(digest) == 64
    assert digest == CryptoEngine.sha256_json(records)


def test_file_execution_binds_file_digest(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    import polars as pl

    source = tmp_path / "in.parquet"
    pl.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}).write_parquet(source)
    target = tmp_path / "out.parquet"
    contract = _contract("digest-file-contract", source, target)

    result = DataShellPipeline(contract).run_file(str(source))
    manifest = result.audit_certificate["cryptographic_manifest"]
    assert manifest["source_payload_sha256"] == CryptoEngine.sha256_file(source)


def test_streaming_execution_persists_metrics_and_fires_metric_drift(tmp_path, monkeypatch):
    """The out-of-core path must persist and compare metrics too, not just in-memory."""
    _isolate(monkeypatch, tmp_path)
    import polars as pl

    baseline_src = tmp_path / "base.parquet"
    pl.DataFrame({"id": list(range(50)), "amount": [10.0] * 50}).write_parquet(baseline_src)
    target = tmp_path / "out.parquet"
    contract = _contract("stream-drift-contract", baseline_src, target)

    first = DataShellPipeline(contract).run_streaming(str(baseline_src), chunk_size=16)
    assert first.status == TerminalStatus.LOADED
    baseline = DataShellPipeline(contract).history_tracker.get_last_run(
        "stream-drift-contract", "default-tenant"
    )
    assert baseline["metrics"]["avg_amount"] == pytest.approx(10.0)
    assert baseline["metrics"]["sum_amount"] == pytest.approx(500.0)

    # Same row count (volume stable) but a 10x value shift; streaming drift must trip.
    shifted_src = tmp_path / "shifted.parquet"
    pl.DataFrame({"id": list(range(50)), "amount": [100.0] * 50}).write_parquet(shifted_src)
    shifted_contract = _contract("stream-drift-contract", shifted_src, tmp_path / "out2.parquet")
    second = DataShellPipeline(shifted_contract).run_streaming(str(shifted_src), chunk_size=16)
    assert second.status == TerminalStatus.REJECTED
    metric_checks = [
        d for d in second.audit_certificate["drift_evaluations"] if "avg_amount" in d["rule_id"]
    ]
    assert metric_checks and metric_checks[0]["passed"] is False

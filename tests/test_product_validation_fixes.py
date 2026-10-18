"""Regression tests for the product-validation P0/P1 fixes.

Covers:
1. Historical drift is opt-in (DRIFT_ENABLED default false) and disabling it
   never rejects a valid delivery because of a small prior test run.
2. DRIFT_BASELINE_RESET clears the stored baseline for a contract/tenant.
3. CLI ``--no-drift`` disables drift for one run.
4. Rejection output names the failing rule, counts, and expected values.
5. ``validate-contract`` reports a clear summary, warnings, and actionable
   errors at authoring time.
6. Delivery status indicators distinguish published vs withheld data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datashell.cli import main as cli
from datashell.core.config import settings
from datashell.core.ledger import BoundaryLedger
from datashell.engines.delivery import TerminalStatus
from datashell.pipeline.runner import DataShellPipeline


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(settings, "audit_dir", tmp_path / "output" / "audits")
    monkeypatch.setattr(settings, "quarantine_dir", tmp_path / "output" / "quarantine")
    monkeypatch.setattr(settings, "drift_enabled", False)
    monkeypatch.setattr(settings, "drift_baseline_reset", False)


def _contract(name: str, source: Path, target: Path, *, batch_rules=None):
    payload = {
        "apiVersion": "daas/v1",
        "kind": "DataProduct",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "sources": [
                {
                    "type": "LOCAL",
                    "path": str(source),
                    "format": "jsonl",
                    "schema": [
                        {"name": "id", "polars_type": "Int64", "nullable": False},
                        {"name": "amount", "polars_type": "Float64", "nullable": False},
                    ],
                }
            ],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [
                    {"rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount", "value": 0}
                ],
                "batch_rules": batch_rules or [],
            },
            "delivery": {"targets": [{"type": "PARQUET", "path": str(target), "mode": "overwrite"}]},
        },
    }
    return payload


def _records(amount: float, rows: int = 50):
    return [{"id": i, "amount": amount} for i in range(rows)]


def _run(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code


# ------------------------------------------------------------------ drift gate


def test_drift_disabled_by_default_ignores_prior_test_baseline(tmp_path, monkeypatch):
    """A tiny first run must not make a normal delivery fail on drift."""
    _isolate(monkeypatch, tmp_path)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("drift-default-off", source, target)

    from datashell.contract.models import DataContract

    small = DataShellPipeline(DataContract(**contract)).run_records(_records(10.0, rows=5))
    assert small.status == TerminalStatus.LOADED

    large = DataShellPipeline(DataContract(**contract)).run_records(_records(100.0, rows=900))
    assert large.status == TerminalStatus.LOADED
    assert large.outcome.rows_delivered == 900
    assert large.audit_certificate["drift_evaluations"] == []


def test_drift_enabled_rejects_with_specific_metric_reason(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "drift_enabled", True)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("drift-on", source, target)

    from datashell.contract.models import DataContract

    first = DataShellPipeline(DataContract(**contract)).run_records(_records(10.0))
    assert first.status == TerminalStatus.LOADED

    second = DataShellPipeline(DataContract(**contract)).run_records(_records(100.0))
    assert second.status == TerminalStatus.REJECTED
    assert "avg_amount" in (second.ledger.rejection_reason or "")
    assert "drift" in (second.ledger.rejection_reason or "").lower()


def test_baseline_reset_watermark_clears_comparisons_without_deleting_history(tmp_path):
    tracker = BoundaryLedger(db_path=tmp_path / "history.db")
    tracker.record_run("run-1", "contract-a", 100, 100, {"sum_x": 1.0}, tenant_id="tenant-a", status="LOADED")
    assert tracker.get_baseline("contract-a", "tenant-a") is not None

    tracker.reset_baseline("contract-a", "tenant-a")
    assert tracker.get_baseline("contract-a", "tenant-a") is None
    # Forensics stay intact: the original run is still queryable.
    assert tracker.get_run("run-1", "tenant-a")["rows_delivered"] == 100

    tracker.record_run("run-2", "contract-a", 90, 90, {"sum_x": 2.0}, tenant_id="tenant-a", status="LOADED")
    baseline = tracker.get_baseline("contract-a", "tenant-a")
    assert baseline is not None
    assert baseline["metrics"]["sum_x"] == pytest.approx(2.0)


def test_pipeline_drift_baseline_reset_env_flow(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "drift_enabled", True)
    source = tmp_path / "in.parquet"
    source.write_bytes(b"placeholder")
    target = tmp_path / "out.parquet"
    contract = _contract("drift-reset", source, target)

    from datashell.contract.models import DataContract

    first = DataShellPipeline(DataContract(**contract)).run_records(_records(10.0))
    assert first.status == TerminalStatus.LOADED
    would_drift = DataShellPipeline(DataContract(**contract)).run_records(_records(100.0))
    assert would_drift.status == TerminalStatus.REJECTED

    # With the reset flag set, the old baseline is ignored and this run
    # re-establishes a fresh comparison point instead of being rejected.
    monkeypatch.setattr(settings, "drift_baseline_reset", True)
    reestablished = DataShellPipeline(DataContract(**contract)).run_records(_records(100.0))
    assert reestablished.status == TerminalStatus.LOADED
    rule_ids = [d["rule_id"] for d in reestablished.audit_certificate["drift_evaluations"]]
    assert "drift-volume-baseline-established" in rule_ids


# --------------------------------------------------------------------- CLI run


def test_cli_no_drift_flag_overrides_environment_default(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "drift_enabled", True)
    source = tmp_path / "feed.jsonl"
    source.write_text(
        "\n".join(json.dumps({"id": i, "amount": 10.0}) for i in range(50)) + "\n",
        encoding="utf-8",
    )
    target = tmp_path / "out.parquet"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract("cli-nodrift", source, target)), encoding="utf-8")

    # Seed a baseline that would otherwise trigger drift on a value shift.
    seed_source = tmp_path / "seed.jsonl"
    seed_source.write_text(
        "\n".join(json.dumps({"id": i, "amount": 10.0}) for i in range(50)) + "\n",
        encoding="utf-8",
    )
    assert _run([
        "run", "--contract", str(contract_path), "--source", str(seed_source),
        "--target", str(target), "--audit", str(tmp_path / "seed_audit.json"),
    ]) == 0

    shifted = tmp_path / "shifted.jsonl"
    shifted.write_text(
        "\n".join(json.dumps({"id": i, "amount": 100.0}) for i in range(50)) + "\n",
        encoding="utf-8",
    )
    code = _run([
        "run", "--contract", str(contract_path), "--source", str(shifted),
        "--target", str(target), "--audit", str(tmp_path / "audit.json"),
        "--no-drift",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Drift: disabled by --no-drift for this run" in out
    assert "OK Data was published to:" in out


def test_cli_rejection_output_is_actionable(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    source = tmp_path / "two.jsonl"
    source.write_text(
        json.dumps({"id": 1, "amount": 10.0}) + "\n" + json.dumps({"id": 2, "amount": 20.0}) + "\n",
        encoding="utf-8",
    )
    target = tmp_path / "out.parquet"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(_contract(
            "cli-actionable",
            source,
            target,
            batch_rules=[{"rule_id": "not-partial", "check": "MIN_ROW_COUNT", "value": 3}],
        )),
        encoding="utf-8",
    )

    code = _run(["run", "--contract", str(contract_path), "--source", str(source), "--target", str(target)])
    captured = capsys.readouterr()
    assert code == 2
    assert "FEED REJECTED" in captured.err
    assert "not-partial batch rule failed" in captured.err
    assert "actual: 2 rows" in captured.err
    assert "expected: >= 3" in captured.err
    assert "Batch rules:" in captured.err
    assert "Run with --json for full details." in captured.err
    assert "audit certificate" in captured.err
    assert "X No data was published" in captured.out


# -------------------------------------------------------- validate-contract


def test_validate_contract_summary_and_warnings(tmp_path, capsys):
    source = tmp_path / "feed.jsonl"
    source.write_text("", encoding="utf-8")
    target = tmp_path / "out.parquet"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract("validate-ok", source, target)), encoding="utf-8")

    assert _run(["validate-contract", "-c", str(contract_path)]) == 0
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "1 row rule" in out
    assert "0 batch rules" in out
    assert "1 delivery target" in out
    assert "WARNING: No batch rules defined" in out
    assert "MIN_ROW_COUNT" in out


def test_validate_contract_reports_clear_errors(tmp_path, capsys):
    source = tmp_path / "feed.jsonl"
    source.write_text("", encoding="utf-8")
    target = tmp_path / "out.parquet"
    payload = _contract("validate-bad", source, target)
    payload["spec"]["quality"]["rules"].append(
        {"rule_id": "ghost", "check": "GREATER_THAN", "column": "does_not_exist", "value": 0}
    )
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _run(["validate-contract", "-c", str(contract_path)]) == 1
    err = capsys.readouterr().err
    assert "ghost" in err
    assert "does_not_exist" in err
    assert "FATAL" not in err

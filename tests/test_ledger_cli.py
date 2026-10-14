"""CLI tests for the boundary-ledger query surface and certificate verification."""

from __future__ import annotations

import json

import pytest

from datashell.cli import main as cli
from datashell.core.ledger import BoundaryLedger
from datashell.contract.models import DataContract
from datashell.pipeline.runner import DataShellPipeline


def _seed(tmp_path):
    db = tmp_path / "history.db"
    ledger = BoundaryLedger(db)
    ledger.record_run(
        "run-1", "contract-a", 100, 90, {"avg_amount": 12.0}, tenant_id="alpha",
        status="LOADED", rows_refused=4, rows_held=6, source_bytes=2048,
        target_path=str(tmp_path / "out.parquet"), execution_mode="batch",
        rule_breakdown={"amount-positive": 6}, structural_reasons={"amount:Float64": 4},
    )
    ledger.record_run(
        "run-2", "contract-a", 50, 0, {}, tenant_id="alpha", status="REJECTED",
        is_run_rejected=True, rows_held=50, rejection_reason="min row count",
    )
    ledger.record_quarantine("run-1", "alpha", "contract-a", "held_quality", str(tmp_path / "q.parquet"), 6, {"amount-positive": 6})
    ledger.record_usage("run-1", "alpha", "contract-a", "LOADED", "batch", 100, 90, bytes_processed=2048)
    return db


@pytest.fixture
def cli_ledger(tmp_path, monkeypatch):
    db = _seed(tmp_path)
    monkeypatch.setattr(cli, "BoundaryLedger", lambda: BoundaryLedger(db))
    return db


def _run(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code


def test_ledger_summary_json(cli_ledger, capsys):
    assert _run(["ledger", "summary", "--tenant", "alpha", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 2
    assert payload["delivered_runs"] == 1
    assert payload["rejected_runs"] == 1
    assert payload["rows_read"] == 150


def test_ledger_summary_human_readable(cli_ledger, capsys):
    assert _run(["ledger", "summary", "--tenant", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "Boundary ledger summary" in out
    assert "Delivery rate" in out


def test_ledger_runs_and_count(cli_ledger, capsys):
    assert _run(["ledger", "runs", "--tenant", "alpha", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2
    assert {r["run_id"] for r in payload["runs"]} == {"run-1", "run-2"}


def test_ledger_runs_status_filter(cli_ledger, capsys):
    assert _run(["ledger", "runs", "--tenant", "alpha", "--status", "REJECTED", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in payload["runs"]] == ["run-2"]


def test_ledger_show_detail_and_breakdown(cli_ledger, capsys):
    assert _run(["ledger", "show", "run-1", "--tenant", "alpha", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["contract_name"] == "contract-a"
    assert record["rule_breakdown"] == {"amount-positive": 6}
    assert record["structural_reasons"] == {"amount:Float64": 4}


def test_ledger_show_missing_run_exits_nonzero(cli_ledger, capsys):
    assert _run(["ledger", "show", "nope", "--tenant", "alpha"]) == 1
    assert "not found" in capsys.readouterr().err


def test_ledger_quarantine_search(cli_ledger, capsys):
    assert _run(["ledger", "quarantine", "--tenant", "alpha", "--reason", "amount", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantine"][0]["run_id"] == "run-1"
    assert payload["quarantine"][0]["dominant_reason"] == "amount-positive"


def test_ledger_usage_metering(cli_ledger, capsys):
    assert _run(["ledger", "usage", "--tenant", "alpha", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total_units"] == 100
    assert summary["bytes_processed"] == 2048


def test_ledger_trend(cli_ledger, capsys):
    assert _run(["ledger", "trend", "--tenant", "alpha", "--json"]) == 0
    trend = json.loads(capsys.readouterr().out)["trend"]
    assert len(trend) == 2


def test_ledger_prune_reports(tmp_path, monkeypatch, capsys):
    db = tmp_path / "history.db"
    BoundaryLedger(db).record_run("r", "c", 1, 1, {}, tenant_id="alpha", status="LOADED")
    monkeypatch.setattr(cli, "BoundaryLedger", lambda: BoundaryLedger(db))
    assert _run(["ledger", "prune", "--json"]) == 0
    assert "run_history" in json.loads(capsys.readouterr().out)


def test_verify_missing_certificate_exits_nonzero(tmp_path, capsys):
    assert _run(["verify", str(tmp_path / "missing.json")]) == 1
    assert "not found" in capsys.readouterr().err


def test_verify_real_certificate_is_authentic(tmp_path, capsys):
    source = tmp_path / "settlements.jsonl"
    source.write_text("")
    contract = DataContract(
        metadata={"name": "verify-cli", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(source),
                "schema": [{"name": "amount", "polars_type": "Float64", "nullable": False}],
            }],
            "quality": {
                "behavior": "DROP_ROW",
                "rules": [{"rule_id": "amount-positive", "check": "GREATER_THAN", "column": "amount", "value": 0}],
                "batch_rules": [{"rule_id": "min-3", "check": "MIN_ROW_COUNT", "value": 3}],
            },
            "delivery": {
                "targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(tmp_path / "out.parquet"), "mode": "overwrite"}],
                "quarantinePath": str(tmp_path / "quarantine"),
            },
        },
    )
    records = [{"amount": float(v)} for v in (10, 20, 30, 40, 50)] + [{"amount": -1.0}]
    audit = tmp_path / "audit.json"
    result = DataShellPipeline(contract).run_records(
        records=records,
        target_path_override=str(tmp_path / "out.parquet"),
        audit_output_path=str(audit),
    )
    assert result.status.value == "LOADED"

    assert _run(["verify", str(audit)]) == 0
    out = capsys.readouterr().out
    assert "AUTHENTIC_AND_UNMODIFIED" in out


def test_verify_detects_a_tampered_artifact(tmp_path, capsys):
    source = tmp_path / "settlements.jsonl"
    source.write_text("")
    contract = DataContract(
        metadata={"name": "verify-tamper", "version": "1.0.0"},
        spec={
            "sources": [{
                "type": "LOCAL",
                "connectionRef": "local",
                "path": str(source),
                "schema": [{"name": "amount", "polars_type": "Float64", "nullable": False}],
            }],
            "quality": {"behavior": "DROP_ROW", "rules": []},
            "delivery": {
                "targets": [{"type": "PARQUET", "connectionRef": "local", "path": str(tmp_path / "out.parquet"), "mode": "overwrite"}],
                "quarantinePath": str(tmp_path / "quarantine"),
            },
        },
    )
    audit = tmp_path / "audit.json"
    target = tmp_path / "out.parquet"
    DataShellPipeline(contract).run_records(
        records=[{"amount": 1.0}, {"amount": 2.0}],
        target_path_override=str(target),
        audit_output_path=str(audit),
    )
    original = target.read_bytes()
    tampered = bytearray(original)
    tampered[0] ^= 0xFF
    target.write_bytes(bytes(tampered))

    assert _run(["verify", str(audit)]) == 3
    assert "TAMPERED_UNDERLYING_ARTIFACTS" in capsys.readouterr().out

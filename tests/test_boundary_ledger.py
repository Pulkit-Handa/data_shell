"""Unit tests for the durable boundary ledger (moat #10).

The ledger is the forensic system of record: every terminal run, every
quarantine artifact, and every metering row. These tests pin tenant isolation,
filtering, aggregation, rolling baselines, retention, and integrity checking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from datashell.core.ledger import BoundaryLedger


def _ledger(tmp_path) -> BoundaryLedger:
    return BoundaryLedger(tmp_path / "history.db")


def test_record_and_get_run_is_tenant_scoped(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("run-1", "contract-a", 10, 8, {"avg_amount": 12.5}, tenant_id="alpha", status="LOADED")

    assert ledger.get_run("run-1", "alpha")["contract_name"] == "contract-a"
    # A different tenant cannot see the run at all.
    assert ledger.get_run("run-1", "beta") is None


def test_record_run_is_idempotent_on_run_id(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("run-1", "c", 10, 8, {}, tenant_id="alpha")
    ledger.record_run("run-1", "c", 99, 99, {}, tenant_id="alpha")
    runs = ledger.list_runs("alpha")
    assert len(runs) == 1
    assert runs[0]["rows_read"] == 99


def test_list_and_count_runs_filter_by_contract_and_status(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("r1", "c-a", 5, 5, {}, tenant_id="alpha", status="LOADED")
    ledger.record_run("r2", "c-a", 5, 0, {}, tenant_id="alpha", status="REJECTED", is_run_rejected=True)
    ledger.record_run("r3", "c-b", 5, 5, {}, tenant_id="alpha", status="LOADED")
    ledger.record_run("r4", "c-a", 5, 5, {}, tenant_id="beta", status="LOADED")

    assert ledger.count_runs("alpha") == 3
    assert ledger.count_runs("alpha", contract_name="c-a") == 2
    assert ledger.count_runs("alpha", status="REJECTED") == 1
    assert ledger.count_runs("beta") == 1

    rejected = ledger.list_runs("alpha", status="REJECTED")
    assert [r["run_id"] for r in rejected] == ["r2"]


def test_history_summary_aggregates(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("r1", "c", 100, 90, {}, tenant_id="alpha", status="LOADED", rows_refused=5, rows_held=5)
    ledger.record_run("r2", "c", 100, 0, {}, tenant_id="alpha", status="REJECTED", is_run_rejected=True, rows_held=40)

    summary = ledger.history_summary("alpha")
    assert summary["total_runs"] == 2
    assert summary["delivered_runs"] == 1
    assert ledger.history_summary("beta")["total_runs"] == 0


def test_quarantine_index_and_filters(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_quarantine("r1", "alpha", "c", "held_quality", "/q/held.parquet", 7, {"amount-positive": 5, "currency": 2})
    ledger.record_quarantine("r2", "alpha", "c", "structural_refused", "/q/refused.parquet", 3, {"amount:Float64": 3})
    ledger.record_quarantine("r3", "beta", "c", "held_quality", None, 1, {"x": 1})

    assert len(ledger.list_quarantine("alpha")) == 2
    assert ledger.list_quarantine("alpha", kind="held_quality")[0]["kind"] == "held_quality"
    assert ledger.list_quarantine("alpha", run_id="r2")[0]["row_count"] == 3
    assert ledger.list_quarantine("alpha", reason="amount-positive")[0]["dominant_reason"] == "amount-positive"
    assert [q["run_id"] for q in ledger.list_quarantine("beta")] == ["r3"]


def test_usage_summary_and_since_filter(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_usage("r1", "alpha", "c", "LOADED", "batch", 100, 90, bytes_processed=2048)
    ledger.record_usage("r2", "alpha", "c", "REJECTED", "streaming", 50, 0, bytes_processed=1024, billable=False)
    ledger.record_usage("r3", "beta", "c", "LOADED", "batch", 10, 10)

    summary = ledger.usage_summary("alpha")
    assert summary["total_runs"] == 2
    assert summary["rows_read"] == 150
    assert summary["total_units"] == 150
    assert summary["billable_units"] == 100
    assert summary["billable_runs"] == 1
    assert summary["bytes_processed"] == 3072

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert ledger.usage_summary("alpha", since=future)["total_runs"] == 0


def test_volume_trend_is_chronological(tmp_path):
    ledger = _ledger(tmp_path)
    for rid, ts in (("old", "2026-01-01T00:00:00Z"), ("new", "2026-02-01T00:00:00Z")):
        ledger.record_run(rid, "c", 1, 1, {}, tenant_id="alpha", status="LOADED")
        with ledger._connect() as conn:
            conn.execute("UPDATE run_history SET timestamp = ? WHERE run_id = ?", (ts, rid))
    trend = ledger.volume_trend("alpha")
    assert [r["run_id"] for r in trend] == ["old", "new"]


def test_rolling_baseline_averages_last_window(tmp_path):
    ledger = _ledger(tmp_path)
    for rid, delivered, avg in (("r1", 10, 1.0), ("r2", 20, 3.0), ("r3", 30, 5.0)):
        ledger.record_run(rid, "c", 100, delivered, {"avg_amount": avg}, tenant_id="alpha", status="LOADED")
    baseline = ledger.get_baseline("c", "alpha", window=2)
    assert baseline["sample_count"] == 2
    assert baseline["rows_delivered"] == 25.0  # (20 + 30) / 2, newest two runs
    assert baseline["metrics"]["avg_amount"] == 4.0


def test_rejected_runs_are_excluded_from_baseline(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("good", "c", 10, 10, {"avg_amount": 2.0}, tenant_id="alpha", status="LOADED")
    ledger.record_run("bad", "c", 10, 0, {"avg_amount": 999.0}, tenant_id="alpha", status="REJECTED", is_run_rejected=True)
    baseline = ledger.get_baseline("c", "alpha")
    assert baseline["sample_count"] == 1
    assert baseline["rows_delivered"] == 10.0


def test_prune_respects_retention_window(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("recent", "c", 1, 1, {}, tenant_id="alpha", status="LOADED")
    ledger.record_run("ancient", "c", 1, 1, {}, tenant_id="alpha", status="LOADED")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with ledger._connect() as conn:
        conn.execute("UPDATE run_history SET timestamp = ? WHERE run_id = 'ancient'", (old_ts,))

    # A non-positive window keeps everything.
    assert ledger.prune(retention_days=0) == {"run_history": 0, "quarantine_index": 0, "usage_records": 0}

    removed = ledger.prune(retention_days=30)
    assert removed["run_history"] == 1
    assert [r["run_id"] for r in ledger.list_runs("alpha")] == ["recent"]


def test_check_integrity_true_for_healthy_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_run("r1", "c", 1, 1, {}, tenant_id="alpha", status="LOADED")
    assert ledger.check_integrity() is True

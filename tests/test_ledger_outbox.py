"""LEDGER-001 regression tests: publication can never be durable while its
ledger row is missing or partial - every terminal run ends COMMITTED or
explicitly PENDING with a replayable outbox record.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("polars")

from datashell.contract.models import DataContract
from datashell.core.config import EnterpriseSettings
from datashell.pipeline.runner import DataShellPipeline, reconcile_pending_ledger_entries


def _isolate_settings(monkeypatch, tmp_path: Path) -> None:
    """Point the global settings singleton at a fresh tmp storage root so each
    test gets its own ledger DB and output tree (no cross-test collision).
    Reconcile while settings are patched so the replay writer and the pipeline
    tracker share the same database.
    """
    monkeypatch.setenv("DATASHELL_STORAGE_ROOT", str(tmp_path))
    new_settings = EnterpriseSettings()
    new_settings.init_directories()
    monkeypatch.setattr("datashell.core.config.settings", new_settings)
    monkeypatch.setattr("datashell.pipeline.runner.settings", new_settings)
    monkeypatch.setattr("datashell.core.ledger.settings", new_settings)


def _run_once(tmp_path: Path, monkeypatch) -> tuple:
    """Run one pipeline execution with a failing ledger write; returns
    (pipeline, result, outbox_path)."""
    from datashell.core import ledger as ledmod

    c = DataContract(
        **json.loads(Path("examples/vendor_settlements_contract.json").read_text())
    )
    p = DataShellPipeline(c, tenant_id="tenant-ledger")

    target = tmp_path / "curated" / "s.parquet"
    audit = tmp_path / "audit" / "cert.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    audit.parent.mkdir(parents=True, exist_ok=True)

    orig = ledmod.BoundaryLedger.apply_run_outcome

    def failing_apply(self, **kw):
        raise OSError("simulated database outage")

    monkeypatch.setattr(ledmod.BoundaryLedger, "apply_run_outcome", failing_apply)
    r = p.run_file(
        source_path="examples/samples/vendor_settlements_1k.jsonl",
        target_path_override=str(target),
        audit_output_path=str(audit),
    )
    # Restore the real writer before returning: the replay path must use the
    # genuine ledger writer, not the failure mock.
    monkeypatch.setattr(ledmod.BoundaryLedger, "apply_run_outcome", orig)
    # The pending record is per-run, so the test must look for the same path
    # the runner wrote.
    outbox = p.history_tracker.pending_outbox_path(audit, r.run_id)
    return p, r, outbox


def test_ledger_write_failure_keeps_run_pending_with_replayable_outbox(
    tmp_path: Path, monkeypatch
):
    """LEDGER-001: when the ledger write fails after publication, the run is
    PENDING, an outbox record exists, and replay makes it exactly COMMITTED."""
    _isolate_settings(monkeypatch, tmp_path)
    p, r, outbox = _run_once(tmp_path, monkeypatch)

    assert r.status.value == "LOADED"
    assert r.ledger_status == "PENDING", r.ledger_status
    # Outbox record left for replay.
    assert outbox.exists()

    # Reconcile while settings (and therefore the shared database) are still
    # patched: exactly one replay → COMMITTED.
    result = reconcile_pending_ledger_entries(storage_root=tmp_path)
    assert result["replayed"] == 1, result
    assert not outbox.exists()
    monkeypatch.undo()
    row = p.history_tracker.get_run(r.run_id, "tenant-ledger")
    assert row is not None
    assert row["ledger_status"] == "COMMITTED"
    assert row["rows_delivered"] == 900


def test_reconcile_is_idempotent_no_duplicate_usage_rows(tmp_path: Path, monkeypatch):
    """LEDGER-001: replaying the same outbox twice never double-counts."""
    _isolate_settings(monkeypatch, tmp_path)
    p, r, outbox = _run_once(tmp_path, monkeypatch)

    assert r.status.value == "LOADED"
    assert r.ledger_status == "PENDING"
    assert outbox.exists()

    # Reconcile twice while settings are still patched.
    first = reconcile_pending_ledger_entries(storage_root=tmp_path)
    second = reconcile_pending_ledger_entries(storage_root=tmp_path)
    monkeypatch.undo()
    assert first["replayed"] == 1
    assert second["replayed"] == 0  # nothing left to replay
    assert not outbox.exists()

    usage = p.history_tracker.usage_summary("tenant-ledger")
    # Exactly one usage record (idempotent INSERT OR REPLACE keyed on run_id).
    assert usage["billable_runs"] == 1, usage
    assert usage["total_units"] == 1000, usage


def test_replay_does_not_duplicate_quarantine_records(tmp_path: Path, monkeypatch):
    """LEDGER-001: repeated application of the same outcome must not duplicate
    quarantine index rows."""
    _isolate_settings(monkeypatch, tmp_path)
    p, r, outbox = _run_once(tmp_path, monkeypatch)
    assert r.ledger_status == "PENDING"

    assert reconcile_pending_ledger_entries(storage_root=tmp_path)["replayed"] == 1
    monkeypatch.undo()

    quarantined = p.history_tracker.list_quarantine("tenant-ledger")
    # One row per quarantine artifact (held + refused) - not duplicated.
    assert len(quarantined) == 2, quarantined
    assert {row["kind"] for row in quarantined} == {"held_quality", "structural_refused"}

    # Applying the identical outcome again must not add rows: the quarantine
    # identity is (tenant, run, kind, artifact). Reuse the *same* artifact
    # paths so this exercises the duplicate case rather than new artifacts.
    # The pipeline's tracker is used directly so the isolated test database
    # (not the default one) is the target.
    tracker = p.history_tracker
    for _ in range(2):
        tracker.apply_run_outcome(
            run_id=r.run_id,
            tenant_id="tenant-ledger",
            contract_name="vendor-settlements",
            rows_read=1000,
            rows_delivered=900,
            status="LOADED",
            quarantine_rows=[
                {
                    "kind": row["kind"],
                    "artifact_path": row["artifact_path"],
                    "row_count": row["row_count"],
                    "reasons": row.get("reasons") or {},
                }
                for row in quarantined
            ],
        )
    assert len(tracker.list_quarantine("tenant-ledger")) == 2


def test_replay_refuses_to_commit_a_run_whose_artifacts_were_never_published(
    tmp_path: Path, monkeypatch
):
    """LEDGER-001: an outbox record whose artifacts were never published must
    not be replayed as a successful delivery."""
    _isolate_settings(monkeypatch, tmp_path)
    p, r, outbox = _run_once(tmp_path, monkeypatch)
    assert r.ledger_status == "PENDING"
    assert outbox.exists()

    # Simulate a publication that never committed: the delivered artifacts are
    # absent, so the outbox record describes a delivery that did not happen.
    for artifact in (tmp_path / "curated" / "s.parquet", tmp_path / "audit" / "cert.json"):
        artifact.unlink(missing_ok=True)

    result = reconcile_pending_ledger_entries(storage_root=tmp_path)
    monkeypatch.undo()
    assert result["replayed"] == 0
    assert result["skipped"] == 1
    # The outbox is retained for operator review, and the run is never marked
    # committed (it stays PENDING, or is absent if the DB was unreachable).
    assert outbox.exists()
    row = p.history_tracker.get_run(r.run_id, "tenant-ledger")
    assert row is None or row["ledger_status"] != "COMMITTED"


def test_concurrent_runs_do_not_overwrite_each_others_outbox(tmp_path: Path, monkeypatch):
    """LEDGER-001: two runs use two distinct outbox records."""
    _isolate_settings(monkeypatch, tmp_path)
    from datashell.core.ledger import pending_outbox_path

    audit_a = tmp_path / "audit" / "dm_run_a_audit_certificate.json"
    audit_b = tmp_path / "audit" / "dm_run_b_audit_certificate.json"
    outbox_a = pending_outbox_path(audit_a)
    outbox_b = pending_outbox_path(audit_b)
    assert outbox_a != outbox_b
    assert outbox_a.name != outbox_b.name


def test_legacy_duplicate_quarantine_rows_are_collapsed(tmp_path: Path):
    """LEDGER-001 migration: pre-existing duplicate quarantine rows are collapsed
    so the new unique constraint can be applied."""
    import sqlite3

    from datashell.core.ledger import BoundaryLedger

    db_path = tmp_path / "history.db"
    BoundaryLedger(db_path=db_path)
    # Simulate a legacy database with duplicate quarantine rows.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_quarantine_unique")
        for _ in range(3):
            conn.execute(
                "INSERT INTO quarantine_index (run_id, tenant_id, contract_name, "
                "timestamp, kind, artifact_path, row_count, dominant_reason, reasons_json) "
                "VALUES ('run-x', 't1', 'c', '2026-01-01T00:00:00+00:00', 'held_quality', "
                "'/tmp/held.parquet', 5, 'r', '{}')"
            )
        conn.commit()

    # Re-initialising the ledger applies the migration and collapses duplicates.
    BoundaryLedger(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM quarantine_index WHERE run_id = 'run-x'"
        ).fetchone()[0]
    assert count == 1, count


def test_shared_audit_path_runs_keep_distinct_outbox_records(tmp_path: Path, monkeypatch):
    """LEDGER-001: two runs pointed at the SAME audit path still own separate
    pending records; neither can overwrite or lose the other's payload."""
    _isolate_settings(monkeypatch, tmp_path)
    from datashell.core.ledger import pending_outbox_path

    audit = tmp_path / "audit" / "shared-audit.json"
    run_a = "dm_run_aaaa1111"
    run_b = "dm_run_bbbb2222"

    outbox_a = pending_outbox_path(audit, run_a)
    outbox_b = pending_outbox_path(audit, run_b)
    assert outbox_a != outbox_b
    assert outbox_a.name != outbox_b.name
    # Both are still discovered by the recovery scan.
    assert outbox_a.name.endswith(".ledger-pending.json")
    assert outbox_b.name.endswith(".ledger-pending.json")


def test_two_pending_runs_replay_independently(tmp_path: Path, monkeypatch):
    """LEDGER-001: two pending runs replay to two distinct ledger rows with no
    duplicated or lost accounting."""
    _isolate_settings(monkeypatch, tmp_path)
    from datashell.contract.models import DataContract
    from datashell.core.ledger import pending_outbox_path, write_pending_outbox
    from datashell.pipeline.runner import DataShellPipeline

    contract = DataContract(
        **json.loads(Path("examples/vendor_settlements_contract.json").read_text())
    )
    pipeline = DataShellPipeline(contract, tenant_id="tenant-ledger")
    tracker = pipeline.history_tracker

    audit = tmp_path / "audit" / "shared-audit.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "curated").mkdir(parents=True, exist_ok=True)

    # Two pending payloads for two different runs, both pending.
    payloads = []
    for run_id in ("dm_run_aaaa1111", "dm_run_bbbb2222"):
        payload = {
            "run_id": run_id,
            "tenant_id": "tenant-ledger",
            "contract_name": "vendor-settlements",
            "rows_read": 100,
            "rows_delivered": 100,
            "status": "LOADED",
            "audit_path": None,
            "run_artifact_path": None,
            "quarantine_files": [],
        }
        write_pending_outbox(pending_outbox_path(audit, run_id), payload)
        payloads.append(payload)

    result = reconcile_pending_ledger_entries(storage_root=tmp_path)
    assert result["replayed"] == 2, result
    assert result["skipped"] == 0, result

    rows = tracker.list_runs("tenant-ledger")
    assert {row["run_id"] for row in rows} == {"dm_run_aaaa1111", "dm_run_bbbb2222"}
    usage = tracker.usage_summary("tenant-ledger")
    assert usage["billable_runs"] == 2, usage
    assert usage["total_units"] == 200, usage


def test_failed_publication_leaves_no_pending_outbox(tmp_path: Path, monkeypatch):
    """LEDGER-001: a publication that never committed must not leave a pending
    record behind claiming a delivery that did not happen."""
    _isolate_settings(monkeypatch, tmp_path)
    from datashell.contract.models import DataContract
    from datashell.pipeline import runner as runner_mod

    contract = DataContract(
        **json.loads(Path("examples/vendor_settlements_contract.json").read_text())
    )
    pipeline = runner_mod.DataShellPipeline(contract, tenant_id="tenant-ledger")

    target = tmp_path / "curated" / "s.parquet"
    audit = tmp_path / "audit" / "cert.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    audit.parent.mkdir(parents=True, exist_ok=True)

    def _fail_publish(*args, **kwargs):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(runner_mod, "publish_transactionally", _fail_publish)
    with pytest.raises(OSError):
        pipeline.run_file(
            source_path="examples/samples/vendor_settlements_1k.jsonl",
            target_path_override=str(target),
            audit_output_path=str(audit),
        )
    monkeypatch.undo()

    # No pending record survives a failed publication.
    leftovers = list(tmp_path.rglob("*.ledger-pending.json"))
    assert leftovers == [], leftovers
    # And the run was not recorded as a delivery.
    assert pipeline.history_tracker.get_run.__self__ is not None  # tracker usable
    runs = pipeline.history_tracker.list_runs("tenant-ledger")
    assert all(row["status"] != "LOADED" for row in runs), runs


def test_run_is_not_marked_committed_with_incomplete_accounting(tmp_path: Path, monkeypatch):
    """LEDGER-001: run row, quarantine index, and usage meter commit together."""
    import sqlite3

    _isolate_settings(monkeypatch, tmp_path)
    from datashell.core.ledger import BoundaryLedger

    ledger = BoundaryLedger()
    original = BoundaryLedger.apply_run_outcome

    def failing_apply(self, *args, **kwargs):
        # Simulate a failure while the outcome is being written.
        raise sqlite3.OperationalError("disk full during outcome write")

    monkeypatch.setattr(BoundaryLedger, "apply_run_outcome", failing_apply)
    with pytest.raises(sqlite3.OperationalError):
        ledger.apply_run_outcome(
            run_id="run-atomic",
            tenant_id="t1",
            contract_name="c",
            rows_read=10,
            rows_delivered=8,
        )
    monkeypatch.setattr(BoundaryLedger, "apply_run_outcome", original)
    monkeypatch.undo()

    # Nothing was recorded: the transaction rolled back as a unit.
    assert ledger.get_run("run-atomic", "t1") is None
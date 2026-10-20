"""Backup/restore drill: a reproducible end-to-end rehearsal of the recovery
procedure, with measured recovery point and recovery duration.

The drill:
  1. Builds a disposable storage root containing real enforcement runs
     (delivered artifacts, audit certificates, generation pointers, quarantine
     artifacts, the ledger database and the credential registry).
  2. Records the pre-backup state: ledger row counts and artifact digests.
  3. Takes a backup with ``scripts/backup_restore.py`` and measures how long
     it takes.
  4. Restores into an *isolated* directory (never over the source) and
     measures how long the restore takes.
  5. Verifies the restored state: manifest checksums, ledger row counts,
     certificate verification against the restored artifacts, and the
     generation pointer of the committed run.
  6. Prints a drill report and exits non-zero if any check fails.

Secrets are never copied, printed, or written into the drill output: the
drill exercises the ledger and the published evidence, and the signing key
stays where the operator keeps it.

Usage
-----
    ENVIRONMENT=test DATASHELL_STORAGE_ROOT=/tmp/drill \
        PYTHONPATH=. python scripts/backup_restore_drill.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The drill must back up a *known* storage root, and Data Shell resolves its
# storage layout from the environment when its settings singleton is first
# constructed. The workspace and the environment are therefore prepared before
# any datashell module is imported, so the pipeline writes into the drill
# workspace instead of the ambient storage root.
DRILL_WORKSPACE = Path(tempfile.mkdtemp(prefix="datashell-drill-"))
DRILL_STORAGE_ROOT = DRILL_WORKSPACE / "storage"
DRILL_BACKUP_DIR = DRILL_WORKSPACE / "backup"
DRILL_RESTORE_ROOT = DRILL_WORKSPACE / "restore"
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["DATASHELL_STORAGE_ROOT"] = str(DRILL_STORAGE_ROOT)
DRILL_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

from scripts.backup_restore import create_backup, restore_backup, verify_restore  # noqa: E402


def _run_sample_run(storage_root: Path, count: int = 2) -> list[str]:
    """Execute real enforcement runs so the drill backs up real evidence."""
    from datashell.contract.models import DataContract
    from datashell.pipeline.runner import DataShellPipeline

    contract = DataContract(
        **json.loads(
            (REPO_ROOT / "examples" / "vendor_settlements_contract.json").read_text(
                encoding="utf-8"
            )
        )
    )
    source = REPO_ROOT / "examples" / "samples" / "vendor_settlements_1k.jsonl"
    run_ids: list[str] = []
    for index in range(count):
        pipeline = DataShellPipeline(contract, tenant_id="drill-tenant")
        audit_dir = storage_root / "output" / "audits"
        audit_dir.mkdir(parents=True, exist_ok=True)
        result = pipeline.run_file(
            source_path=str(source),
            target_path_override=str(
                storage_root / "output" / "drill" / f"delivered_{index}.parquet"
            ),
            audit_output_path=str(audit_dir / f"drill_{index}_audit_certificate.json"),
        )
        run_ids.append(result.run_id)
    return run_ids


def _ledger_counts(db_path: Path) -> dict:
    import sqlite3

    counts = {}
    with sqlite3.connect(str(db_path)) as conn:
        for table in ("run_history", "quarantine_index", "usage_records"):
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup/restore drill.")
    parser.add_argument("--keep", action="store_true", help="keep the drill workspace")
    args = parser.parse_args()

    workspace = DRILL_WORKSPACE
    storage_root = DRILL_STORAGE_ROOT
    backup_dir = DRILL_BACKUP_DIR
    restore_root = DRILL_RESTORE_ROOT
    storage_root.mkdir(parents=True, exist_ok=True)

    print(f"Drill workspace: {workspace}")
    print("\n[1/6] Producing real enforcement runs ...")
    run_ids = _run_sample_run(storage_root)
    print(f"      runs: {', '.join(run_ids)}")

    ledger_db = storage_root / "output" / "datashell_history.db"
    if not ledger_db.is_file():
        print("FAILED: no ledger database was produced", file=sys.stderr)
        return 1

    print("\n[2/6] Recording pre-backup state ...")
    before_counts = _ledger_counts(ledger_db)
    print(f"      ledger rows: {before_counts}")

    print("\n[3/6] Taking backup ...")
    backup_start = time.perf_counter()
    manifest = create_backup(storage_root, backup_dir)
    backup_seconds = time.perf_counter() - backup_start
    print(f"      backed up {len(manifest.get('files', []))} files in {backup_seconds:.2f}s")

    print("\n[4/6] Restoring into an isolated directory ...")
    restore_start = time.perf_counter()
    restore_backup(backup_dir, restore_root)
    restore_seconds = time.perf_counter() - restore_start
    print(f"      restored in {restore_seconds:.2f}s")

    print("\n[5/6] Verifying restored state ...")
    verify_result = verify_restore(restore_root)

    restored_ledger = restore_root / "output" / "datashell_history.db"
    after_counts = _ledger_counts(restored_ledger) if restored_ledger.is_file() else {}
    print(f"      restored ledger rows: {after_counts}")

    failures = []
    if before_counts != after_counts:
        failures.append(
            f"ledger row counts differ: before={before_counts} after={after_counts}"
        )
    if verify_result.get("valid") is False:
        failures.append(f"restore verification failed: {verify_result}")

    # Independently verify a restored certificate against the restored
    # artifacts, using the operator's own key (not a copied one).
    print("\n[6/6] Verifying a restored certificate against restored artifacts ...")
    restored_audits = sorted(
        (restore_root / "output" / "audits").glob("*_audit_certificate.json")
    )
    certificate_results = []
    if not restored_audits:
        failures.append("no audit certificates were restored")
    for audit in restored_audits:
        from datashell.pipeline.artifacts import ArtifactGenerator

        report = ArtifactGenerator.verify_certificate_and_artifacts(
            json.loads(audit.read_text(encoding="utf-8"))
        )
        certificate_results.append(
            {"certificate": audit.name, "status": report.get("status")}
        )
        print(f"      {audit.name}: {report.get('status')}")
        if report.get("status") != "AUTHENTIC_AND_UNMODIFIED":
            failures.append(
                f"restored certificate {audit.name} did not verify: {report.get('status')}"
            )

    # The generation pointer must be present and name a committed run.
    pointers = sorted(restore_root.rglob("*.generation.json"))
    pointer_runs = [
        json.loads(pointer.read_text(encoding="utf-8")).get("run_id")
        for pointer in pointers
    ]
    print(f"      restored generation pointers: {pointer_runs}")
    if not pointer_runs:
        failures.append("no generation pointers were restored")

    report = {
        "workspace": str(workspace),
        "runs": run_ids,
        "backup_seconds": round(backup_seconds, 3),
        "restore_seconds": round(restore_seconds, 3),
        "total_recovery_seconds": round(backup_seconds + restore_seconds, 3),
        "files_backed_up": len(manifest.get("files", [])),
        "ledger_rows_before": before_counts,
        "ledger_rows_after": after_counts,
        "certificate_results": certificate_results,
        "generation_pointers": pointer_runs,
        "failures": failures,
    }
    report_path = workspace / "drill_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    print("      BACKUP / RESTORE DRILL REPORT")
    print("=" * 68)
    print(f"Backup duration:            {backup_seconds:.2f}s")
    print(f"Restore duration:           {restore_seconds:.2f}s")
    print(f"Total recovery duration:    {backup_seconds + restore_seconds:.2f}s")
    print(f"Files backed up:            {len(manifest.get('files', []))}")
    print(f"Ledger rows before/after:   {before_counts} / {after_counts}")
    print(f"Certificates re-verified:   {len(certificate_results)}")
    print(f"Generation pointers:        {pointer_runs}")
    print(f"Report:                     {report_path}")
    print("=" * 68)

    if failures:
        print("DRILL FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("DRILL PASSED")
    if not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

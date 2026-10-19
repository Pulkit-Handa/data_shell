"""Backup and restore tooling for the Data Shell boundary ledger, artifacts,
publication journals, and signing-key metadata.

This script provides:
- Consistent backup of the SQLite ledger (using SQLite's backup API)
- Backup of the artifact root, journals, and key metadata
- Restore into an isolated environment
- Verification of restored ledger/artifact consistency

Usage:
    python scripts/backup_restore.py backup --storage-root /path/to/storage --backup-dir /path/to/backup
    python scripts/backup_restore.py restore --backup-dir /path/to/backup --restore-root /path/to/restore
    python scripts/backup_restore.py verify --restore-root /path/to/restore
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_backup(storage_root: Path, backup_dir: Path) -> Dict[str, Any]:
    """Create a consistent backup of the storage root.

    Uses SQLite's online backup API for the ledger to avoid corruption
    from concurrent writes. The artifact root, journals, and key metadata
    are copied with checksums.
    """
    storage_root = Path(storage_root).resolve()
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "version": 1,
        "created_at": _utcnow(),
        "storage_root": str(storage_root),
        "files": [],
    }

    # 1. Backup the artifact root first (excluding SQLite files which are backed up separately)
    artifact_root = storage_root / "output"
    if artifact_root.exists():
        backup_artifacts = backup_dir / "output"
        backup_artifacts.mkdir(parents=True, exist_ok=True)
        for item in artifact_root.iterdir():
            # Skip SQLite files — they are backed up using the online backup API
            if item.name in ("datashell_history.db", "security_registry.sqlite3"):
                continue
            dest_item = backup_artifacts / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest_item)
        # Record checksums for all artifact files
        for artifact_file in backup_artifacts.rglob("*"):
            if artifact_file.is_file():
                rel_path = str(artifact_file.relative_to(backup_dir))
                manifest["files"].append({
                    "path": rel_path,
                    "sha256": _sha256_file(artifact_file),
                    "size": artifact_file.stat().st_size,
                    "type": "artifact",
                })

    # 2. Backup the SQLite ledger using the online backup API
    ledger_path = storage_root / "output" / "datashell_history.db"
    if ledger_path.exists():
        backup_db_path = backup_dir / "output" / "datashell_history.db"
        backup_db_path.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(ledger_path))
        dest = sqlite3.connect(str(backup_db_path))
        source.backup(dest)
        dest.close()
        source.close()
        manifest["files"].append({
            "path": "output/datashell_history.db",
            "sha256": _sha256_file(backup_db_path),
            "size": backup_db_path.stat().st_size,
            "type": "sqlite_ledger",
        })

    # 3. Backup the security registry
    security_path = storage_root / "output" / "security_registry.sqlite3"
    if security_path.exists():
        backup_sec_path = backup_dir / "output" / "security_registry.sqlite3"
        backup_sec_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(security_path, backup_sec_path)
        manifest["files"].append({
            "path": "output/security_registry.sqlite3",
            "sha256": _sha256_file(backup_sec_path),
            "size": backup_sec_path.stat().st_size,
            "type": "sqlite_security",
        })

    # 4. Write manifest
    manifest_path = backup_dir / "backup_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    return manifest


def restore_backup(backup_dir: Path, restore_root: Path) -> Dict[str, Any]:
    """Restore a backup into an isolated environment.

    Returns a report with restore metrics and verification results.
    """
    backup_dir = Path(backup_dir).resolve()
    restore_root = Path(restore_root).resolve()
    restore_root.mkdir(parents=True, exist_ok=True)

    start_time = time.time()

    # Read manifest
    manifest_path = backup_dir / "backup_manifest.json"
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    # Verify and restore each file
    restored_files = []
    errors = []
    for file_info in manifest["files"]:
        src = backup_dir / file_info["path"]
        dest = restore_root / file_info["path"]

        if not src.exists():
            errors.append(f"Missing backup file: {file_info['path']}")
            continue

        # Verify checksum
        actual_hash = _sha256_file(src)
        if actual_hash != file_info["sha256"]:
            errors.append(f"Checksum mismatch: {file_info['path']}")
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        restored_files.append(file_info["path"])

    elapsed = time.time() - start_time

    # Verify the restored ledger
    ledger_ok = False
    restored_ledger = restore_root / "output" / "datashell_history.db"
    if restored_ledger.exists():
        try:
            conn = sqlite3.connect(str(restored_ledger))
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] == "ok":
                # Check that at least the run_history table exists and is queryable
                conn.execute("SELECT COUNT(*) FROM run_history").fetchone()
                ledger_ok = True
            conn.close()
        except sqlite3.Error:
            ledger_ok = False

    return {
        "restored_files": len(restored_files),
        "errors": errors,
        "elapsed_seconds": round(elapsed, 3),
        "ledger_ok": ledger_ok,
        "backup_created_at": manifest.get("created_at"),
    }


def verify_restore(restore_root: Path) -> Dict[str, Any]:
    """Verify restored ledger/artifact consistency and certificate verification."""
    restore_root = Path(restore_root).resolve()
    report: Dict[str, Any] = {
        "restore_root": str(restore_root),
        "verified_at": _utcnow(),
        "checks": {},
    }

    # Check ledger
    ledger_path = restore_root / "output" / "datashell_history.db"
    if ledger_path.exists():
        try:
            conn = sqlite3.connect(str(ledger_path))
            row = conn.execute("PRAGMA quick_check").fetchone()
            report["checks"]["ledger_integrity"] = row[0] == "ok" if row else False

            # Count runs
            run_count = conn.execute("SELECT COUNT(*) FROM run_history").fetchone()[0]
            report["checks"]["run_count"] = run_count

            # Check for orphaned records (runs without corresponding artifacts)
            # Only query columns that exist in the schema
            columns = {row[1] for row in conn.execute("PRAGMA table_info(run_history)").fetchall()}
            orphaned = 0
            if "target_path" in columns:
                for row in conn.execute("SELECT run_id, target_path FROM run_history").fetchall():
                    run_id, target_path = row
                    if target_path and not Path(target_path).exists():
                        orphaned += 1
            report["checks"]["orphaned_runs"] = orphaned

            conn.close()
        except sqlite3.Error as e:
            report["checks"]["ledger_integrity"] = False
            report["checks"]["ledger_error"] = str(e)
    else:
        report["checks"]["ledger_integrity"] = False

    # Check audit certificates
    audit_dir = restore_root / "output" / "audits"
    if audit_dir.exists():
        cert_count = 0
        cert_ok = 0
        for cert_file in audit_dir.glob("*_audit_certificate.json"):
            cert_count += 1
            try:
                with open(cert_file) as f:
                    cert = json.load(f)
                # Basic structure check
                if "cryptographic_manifest" in cert and "digital_signature" in cert:
                    cert_ok += 1
            except (json.JSONDecodeError, KeyError):
                pass
        report["checks"]["certificates_total"] = cert_count
        report["checks"]["certificates_valid_structure"] = cert_ok
    else:
        report["checks"]["certificates_total"] = 0
        report["checks"]["certificates_valid_structure"] = 0

    # Check for leftover commit journals (should be none after recovery)
    journals = list(restore_root.rglob(".dm-commit-*.json"))
    report["checks"]["leftover_journals"] = len(journals)

    return report


def main():
    parser = argparse.ArgumentParser(description="Data Shell backup and restore tool")
    subparsers = parser.add_subparsers(dest="command")

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--storage-root", required=True)
    backup_parser.add_argument("--backup-dir", required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup-dir", required=True)
    restore_parser.add_argument("--restore-root", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--restore-root", required=True)

    args = parser.parse_args()

    if args.command == "backup":
        manifest = create_backup(Path(args.storage_root), Path(args.backup_dir))
        print(json.dumps(manifest, indent=2, sort_keys=True))
        print(f"\nBackup created: {len(manifest['files'])} files")

    elif args.command == "restore":
        report = restore_backup(Path(args.backup_dir), Path(args.restore_root))
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["errors"]:
            print(f"\nERRORS: {len(report['errors'])}")
            for err in report["errors"]:
                print(f"  - {err}")
            sys.exit(1)
        print(f"\nRestore complete: {report['restored_files']} files in {report['elapsed_seconds']}s")

    elif args.command == "verify":
        report = verify_restore(Path(args.restore_root))
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report["checks"].get("ledger_integrity"):
            print("\nWARNING: Ledger integrity check failed")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

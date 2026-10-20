"""Tier 3: Outside — Delivery Engine with Zero-Deficit Footing Verification and Atomic Staged Commits."""

from __future__ import annotations

import contextlib
import datetime
import decimal
import json
import os
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from datashell.contract.models import DataContract
from datashell.core.commit import publish_transactionally, recover_publication
from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
from datashell.core.fileops import materialize_artifact_copy
from datashell.core.sandbox import FilesystemSandbox
from datashell.engines.ingestion import StructuralRejection
from datashell.engines.reconciler import ReconciliationFootingError, ReconciliationLedger


class TerminalStatus(StrEnum):
    LOADED = "LOADED"          # Statement of fact that surviving records were safely committed
    REJECTED = "REJECTED"      # Whole run rejected by declared stop condition or policy
    FAILED = "FAILED"          # Reserved strictly for engine or infrastructure errors


class DeliveryGateError(Exception):
    """Raised when declared contract gate prevents delivery."""
    pass



def safe_json_default(obj: Any) -> Any:
    """Encodes non-JSON primitive types (datetime, Decimal, UUID, bytes, Enum) gracefully."""
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "value"):
        return obj.value
    if hasattr(obj, "__str__"):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def safe_json_dumps(obj: Any) -> str:
    """Serializes objects to JSON using safe encoder for dates/timestamps and primitives."""
    return json.dumps(obj, default=safe_json_default)


@dataclass
class DeliveryOutcome:
    status: TerminalStatus
    rows_delivered: int
    target_path: str | None
    quarantine_path: str | None
    ledger: ReconciliationLedger
    quarantine_files: list[str] = field(default_factory=list)
    details: str | None = None
    delivered_sha256: str | None = None
    run_artifact_path: str | None = None
    staged_target_path: str | None = None
    staged_run_artifact_path: str | None = None


class DeliveryEngine:
    """
    Final boundary gate. Writes data to the target if and only if
    arithmetic reconciliation foots exactly, contract gates pass,
    and paths are inside approved storage roots.
    """

    def __init__(self, contract: DataContract, tenant_id: str = "default-tenant"):
        self.contract = contract
        self.delivery_spec = contract.spec.delivery
        self.quality_spec = contract.spec.quality
        self.tenant_id = tenant_id

    def deliver(
        self,
        admitted_df: pl.DataFrame,
        held_df: pl.DataFrame,
        structural_rejections: list[StructuralRejection],
        ledger: ReconciliationLedger,
        all_batch_rules_passed: bool,
        should_reject_run: bool = False,
        defer_publish: bool = False,
        rejection_reason: str | None = None,
    ) -> DeliveryOutcome:
        """
        Executes atomic staged delivery, sandboxing checks, and quarantine artifact generation.
        """
        # GATE 1: Zero-Deficit Footing Check
        if not ledger.footing_matched:
            raise ReconciliationFootingError(
                f"DELIVERY REFUSED TO WRITE: Reconciliation arithmetic failed to foot! "
                f"Unaccounted for: {ledger.unaccounted_for} rows."
            )

        # Resolve and sandbox quarantine directory under tenant namespace
        quarantine_path_str = self.delivery_spec.quarantinePath
        if quarantine_path_str:
            quarantine_dir = FilesystemSandbox.sanitize_and_validate_path(
                quarantine_path_str, tenant_id=self.tenant_id
            )
        else:
            quarantine_dir = settings.get_tenant_quarantine_dir(self.tenant_id)
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        # Track staged files for atomic cleanup in case of failure
        staged_files: list[Path] = []
        created_quarantine_files: list[Path] = []

        try:
            # 1. ALWAYS persist quarantine artifacts (even if the run is later rejected!)
            published_quarantine_path: Path | None = None
            if held_df.height > 0:
                staged_held = quarantine_dir / f".staged_{ledger.run_id}_held_records.parquet"
                final_held = quarantine_dir / f"{ledger.run_id}_held_records.parquet"
                held_df.write_parquet(staged_held)
                staged_files.append(staged_held)
                os.replace(staged_held, final_held)
                created_quarantine_files.append(final_held)
                published_quarantine_path = quarantine_dir

            if structural_rejections:
                staged_rejections = quarantine_dir / f".staged_{ledger.run_id}_structural_rejections.jsonl"
                final_rejections = quarantine_dir / f"{ledger.run_id}_structural_rejections.jsonl"
                with open(staged_rejections, "w", encoding="utf-8") as f:
                    for r in structural_rejections:
                        f.write(safe_json_dumps({
                            "row_index": r.row_index,
                            "failed_column": r.failed_column,
                            "expected_type": r.expected_type,
                            "error_message": r.error_message,
                            "raw_record": r.raw_record
                        }) + "\n")
                staged_files.append(staged_rejections)
                os.replace(staged_rejections, final_rejections)
                created_quarantine_files.append(final_rejections)
                published_quarantine_path = quarantine_dir

            # GATE 2: Contract Gate (Stop conditions / batch rule pass / behavior check)
            # Rejection is determined by quality engine according to declared contract behavior
            if should_reject_run:
                reason = rejection_reason or "Violations or batch assertions exceeded policy gate"
                ledger.mark_rejected(reason)
                return DeliveryOutcome(
                    status=TerminalStatus.REJECTED,
                    rows_delivered=0,
                    target_path=None,
                    quarantine_path=str(published_quarantine_path) if published_quarantine_path else None,
                    quarantine_files=[str(p) for p in created_quarantine_files],
                    ledger=ledger,
                    details=f"Run rejected by contract policy: {reason}. Zero rows delivered."
                )

            # GATE 3: Atomic Staged Target Delivery
            target = self.delivery_spec.targets[0] if self.delivery_spec.targets else None
            target_path_str = target.path if target else "./output/delivered.parquet"
            # Strict delivery sandbox: enforces tenant isolation and forbids operational namespaces
            target_path = FilesystemSandbox.sanitize_and_validate_path(
                target_path_str, is_delivery_target=True, tenant_id=self.tenant_id
            )

            if not defer_publish:
                recover_publication(target_path)
            staged_target = target_path.with_name(f".staged_{ledger.run_id}_{target_path.name}")
            staged_files.append(staged_target)

            # Write to staging path
            admitted_df.write_parquet(staged_target)

            # Compute cryptographic hash of the exact staged file BEFORE publication
            # Guarantees concurrent runs cannot tamper with or replace the hash binding!
            delivered_sha256 = CryptoEngine.sha256_file(staged_target)

            # Stage immutable run-specific output artifact for permanent lineage: <run_id>_<target_name>
            run_artifact_path = target_path.with_name(f"{ledger.run_id}_{target_path.name}")
            staged_run_artifact = target_path.with_name(f".staged_{ledger.run_id}_lineage_{target_path.name}")
            staged_files.append(staged_run_artifact)
            materialize_artifact_copy(staged_target, staged_run_artifact)

            if defer_publish:
                # The pipeline runner will generate the audit certificate and
                # commit certificate + active target + immutable lineage together.
                staged_target_path = str(staged_target)
                staged_run_artifact_path = str(staged_run_artifact)
            else:
                # Direct engine callers retain the legacy two-artifact transaction.
                publish_transactionally(
                    [(staged_run_artifact, run_artifact_path), (staged_target, target_path)],
                    run_id=ledger.run_id,
                )
                staged_target_path = None
                staged_run_artifact_path = None
            delivered_count = admitted_df.height

            return DeliveryOutcome(
                status=TerminalStatus.LOADED,
                rows_delivered=delivered_count,
                target_path=str(target_path),
                quarantine_path=str(published_quarantine_path) if published_quarantine_path else None,
                quarantine_files=[str(p) for p in created_quarantine_files],
                ledger=ledger,
                details=f"Delivered {delivered_count:,} conforming rows with recoverable lineage publication.",
                delivered_sha256=delivered_sha256,
                run_artifact_path=str(run_artifact_path),
                staged_target_path=staged_target_path,
                staged_run_artifact_path=staged_run_artifact_path,
            )


        except Exception:
            # Clean up any uncommitted staged files
            for sf in staged_files:
                if sf.exists():
                    with contextlib.suppress(OSError):
                        sf.unlink()
            raise
